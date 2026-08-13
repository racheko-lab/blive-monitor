"""KuaishouAdapter：快手直播 + 新作（阶段三 T02）。

直播：主路径 **SSR 解析** ``live.kuaishou.com/u/<id>`` 的 ``window.__INITIAL_STATE__``。
      真实结构（已实测）：直播态在 ``liveroom.playList[0].isLiving``，详情在
      ``liveroom.playList[0].liveStream.{caption,coverUrl,watcherCount}``；
      旧结构（``liveroom.living/caption``）作为兼容回退保留。
      不再调用已废弃的 ``liveroomDetail`` 接口（实测 HTTP 404）。
      SSR 失败时用 ``dynamicIcon`` 兜底 —— 该接口**免 Cookie 免验证码**直出
      ``isLiving``/``liveStreamId``（见 :meth:`KuaishouAdapter._live_via_dynamic_icon`）。

新作：走 ``live_api/profile/public``（浏览器拦截，免登录 Cookie），
      实现见 :mod:`backend.adapters.kuaishou_feed`。

      **此前的 ``visionProfilePhotoList`` graphql 实现已删除**：该端点被快手前端
      弃用（打开 profile 页，页面自身发出的 graphql 请求数为 0），裸请求恒返回
      ``result=2``。它「不报错、只返回空」的失败方式极具欺骗性 —— 监控看起来在跑，
      实际上永远报「没有新作品」。同类被验证挡死的通道见 kuaishou_feed 模块文档。

匿名 scraping 尽力而为：失败一律优雅降级为 offline / raise AdapterGated
（绝不抛未捕获异常中断整轮，也绝不把「被挡」伪装成「没有新作」）。
"""

import json
import logging
import re
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from backend.adapters import kuaishou_feed as ks_feed
from backend.adapters.base import (
    AdapterGated,
    AdapterSkip,
    PlatformAdapter,
    PostModel,
    RoomModel,
)
from common import bjnow, epoch_to_beijing
from backend.adapters.identity import (
    CredentialLevel,
    IdentityCache,
    PrincipalIdentity,
)
from backend.adapters.kuaishou_identity import (
    KuaishouIdentityResolver,
    looks_like_principal_id as _looks_like_principal_id,
)

logger = logging.getLogger(__name__)

#: 进程内共享的身份缓存 —— 同一轮监控里多个账号/多次调用不重复解析。
#: 跨轮持久化交给 tracking 的 ``principal_id`` 字段（见 apply_identity_to_tracking）。
_identity_cache = IdentityCache()

#: 从 config 条目里认得的身份提示字段（用户填了就用，没填 resolver 自己找）
_HINT_KEYS = ("principal_id", "nickname", "unique_name", "share_user_id",
              "room_id", "live_id", "home_url", "share_url", "seed_url", "photo_id")


def build_identity_hints(entry: Any, tracking: Any = None) -> Dict[str, Any]:
    """把 config 条目 + 已有 tracking 合成 resolver 的 hints。

    tracking 里的 ``principal_id`` 是上一轮解析并校验过的结果，直接复用可以让稳态
    运行时的身份解析降到零次网络请求 —— 这也是跨轮次的持久化缓存。
    config 优先级高于 tracking（用户改配置能立刻生效）。
    """
    hints: Dict[str, Any] = {}
    if isinstance(tracking, dict):
        for key in ("principal_id", "nickname", "unique_name"):
            if tracking.get(key):
                hints[key] = str(tracking[key])
    if isinstance(entry, dict):
        for key in _HINT_KEYS:
            if entry.get(key):
                hints[key] = str(entry[key])
        # config 里的 name 当昵称用（不覆盖更明确的 nickname 字段）
        if entry.get("name") and not hints.get("nickname"):
            hints["nickname"] = str(entry["name"])
    return hints


#: 已校验身份的信任期：这段时间内不再重复交叉校验（秒）。
#:
#: 为什么需要它：CI 每 5 分钟一轮，而一次完整校验要打 2 次 live.kuaishou.com
#: （输入侧 + principalId 侧）。每轮都验 = 每账号每天约 864 次请求，
#: 实测十几次连打就会进入 **11 分钟以上** 的 IP 级限流惩罚期 —— 那样反而
#: 什么都监控不到。principalId 是账号级稳定标识（originUserId 更是终生不变），
#: 「首次严格校验 + 24 小时内信任 + 到期重验」在准确性上的损失可以忽略，
#: 请求量却降到 1/288。这不是放宽标准，是把校验预算花在刀刃上。
IDENTITY_TRUST_SEC = 24 * 3600

#: 校验**没做成**（VerifyOutcome.UNKNOWN，典型是被限流）之后的重验冷却期（秒）。
#:
#: 为什么必须有它 —— 这是实测出来的死锁，光看代码想不到：
#:
#: 1. 被限流时 ``verify()`` 只能返回 UNKNOWN，写下 ``identity_verified=False``；
#: 2. 信任期只认 ``identity_verified=True``，于是下一轮不享受信任期；
#: 3. 下一轮走完整解析，又打 live 请求（限流下还会退避重试，**实测每轮 6 次**）；
#: 4. 而惩罚期内的请求会给惩罚**续期** → 永远出不来。
#:
#: 实测：限流环境下连打 5 轮，每轮 6 次，换算 **1728 次/账号/天**。
#: 信任期本是为了省请求，却恰恰在最需要它的时候完全失效。
#:
#: 解法不是「把没验过的当成验过」（那是降低标准），而是**降低重试频率**：
#: principal_id 照常复用并**如实标记为未校验**，只是不再以 5 分钟一次的
#: 频率去撞一堵已知的墙。取值必须大于实测惩罚期（>50min），故设 2 小时。
IDENTITY_REVERIFY_COOLDOWN_SEC = 2 * 3600


def identity_from_tracking(tracking: Any, rid: str = "") -> Optional[PrincipalIdentity]:
    """从 tracking 恢复上一轮的身份（跨进程缓存）。

    CI 每轮都是全新进程，内存缓存必然落空，但 tracking 会随仓库提交回来，
    天然就是持久层。

    「未校验」有两种成因，必须区别对待，混为一谈就会踩死锁
    （见 :data:`IDENTITY_REVERIFY_COOLDOWN_SEC`）：

    * ``identity_verified=True`` —— 验过了，享受 24h **信任期**，返回已校验身份；
    * ``identity_verified=False`` —— 校验没做成（多半是被限流）。principal_id
      本身可能完全正确，只是没机会证实。此时在 **重验冷却期**内照常复用它，
      但 ``extra["verified"]`` **如实为 False**，冷却期一过立刻重新严格校验。

    第二种情况绝不是「把猜测当结论」：身份状态在 tracking 和日志里始终显示为
    未校验，只是不再每 5 分钟重试一次注定失败的校验。
    """
    if not isinstance(tracking, dict):
        return None
    pid = str(tracking.get("principal_id") or "")
    if not pid:
        return None

    verified = bool(tracking.get("identity_verified"))
    if verified:
        window = IDENTITY_TRUST_SEC
        stamp = tracking.get("last_identity_refresh")
    else:
        # 未校验：按「上次尝试时间」算冷却，避免限流期间每轮重撞
        window = IDENTITY_REVERIFY_COOLDOWN_SEC
        stamp = tracking.get("last_identity_attempt") or \
            tracking.get("last_identity_refresh")

    refreshed = _parse_bj(stamp)
    if refreshed is None:
        return None
    age = (bjnow() - refreshed).total_seconds()
    if age < 0 or age > window:
        return None

    ident = PrincipalIdentity(
        platform="kuaishou",
        principal_id=pid,
        nickname=str(tracking.get("nickname") or ""),
        unique_name=str(tracking.get("unique_name") or ""),
        identity_source=str(tracking.get("identity_source") or "tracking"),
        confidence=1.0 if verified else 0.5,
        last_updated=str(tracking.get("last_identity_refresh") or ""),
    )
    ident.trace = ["tracking_trusted" if verified else "tracking_reverify_cooldown"]
    # 如实标记：没验过就是没验过，绝不因为复用而伪装成已校验
    ident.extra["verified"] = verified
    ident.extra["trusted_from_tracking"] = True
    if not verified:
        ident.extra["reverify_deferred"] = True
    origin = tracking.get("origin_user_id")
    if origin:
        ident.extra["origin_user_id"] = str(origin)
    return ident


def _parse_bj(s: Any) -> Optional[datetime]:
    """解析 ``YYYY-MM-DD HH:MM:SS`` 北京时间；失败返回 None。"""
    try:
        return datetime.strptime(str(s), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def resolve_kuaishou_identity(entry: Any, rid: str, tracking: Any = None,
                              resolver: Optional[KuaishouIdentityResolver] = None,
                              ) -> Optional[PrincipalIdentity]:
    """解析快手账号身份（Fail Soft，解不出返回 None）。

    Identity Framework 的统一入口：不再手写「有 principal_id 就用、否则扒 seed_url」
    那套散装逻辑，交给 resolver 的策略流水线 + originUserId 交叉校验。

    稳态快路径：上一轮已校验且在信任期内 → 直接复用，零请求（见
    :data:`IDENTITY_TRUST_SEC`）。但用户在 config 里改了 principal_id 时立即失效，
    否则「改了配置不生效」会让人抓狂。
    """
    trusted = identity_from_tracking(tracking, rid)
    if trusted is not None:
        cfg_pid = str((entry or {}).get("principal_id") or "") if isinstance(entry, dict) else ""
        if not cfg_pid or cfg_pid == trusted.principal_id:
            return trusted
        logger.info("[kuaishou] config 的 principal_id 已变更（%s → %s），放弃信任期重新解析",
                    trusted.principal_id, cfg_pid)
    r = resolver or KuaishouIdentityResolver(cache=_identity_cache)
    return r.resolve(rid, hints=build_identity_hints(entry, tracking))


def apply_identity_to_tracking(ident: Optional[PrincipalIdentity],
                               tracking: Dict[str, Any]) -> None:
    """把解析到的身份写回 tracking，供下一轮零成本复用。"""
    if ident is None or not isinstance(tracking, dict):
        return
    extra = ident.extra or {}
    tracking["principal_id"] = ident.principal_id
    tracking["identity_source"] = ident.identity_source
    tracking["last_identity_refresh"] = ident.last_updated
    tracking["identity_verified"] = bool(extra.get("verified"))
    if not extra.get("trusted_from_tracking"):
        # 只有真正打了请求去解析/校验才算一次「尝试」。复用 tracking 时若也刷新，
        # 冷却期会被无限推后，等于永远不再重验 —— 和「不刷新信任期起点」同一个坑。
        tracking["last_identity_attempt"] = _now_bj()
    if ident.nickname and not tracking.get("nickname"):
        tracking["nickname"] = ident.nickname
    if ident.unique_name:
        tracking["unique_name"] = ident.unique_name
    origin = (ident.extra or {}).get("origin_user_id")
    if origin:
        tracking["origin_user_id"] = str(origin)


def apply_identity_to_config(ident: Optional[PrincipalIdentity],
                             entry: Dict[str, Any]) -> bool:
    """把解析到的身份补进 config 条目（任务八：用户不填则自动补齐）。

    **只填空位**：用户手填的值永远优先，解析结果只补用户没说的部分。
    发现两者冲突时不静默覆盖，而是明确告警 —— 冲突要么是用户配错了人，
    要么是我们解错了人，两种都必须被看见，绝不能悄悄和稀泥。

    Returns:
        是否写入了新字段（供调用方决定要不要落盘）。
    """
    if ident is None or not isinstance(entry, dict):
        return False
    extra = ident.extra or {}
    candidates = {
        "principal_id": ident.principal_id,
        "origin_user_id": str(extra.get("origin_user_id") or ""),
        "nickname": ident.nickname,
        "unique_name": ident.unique_name,
        "home_url": ident.home_url,
        "share_url": ident.share_url,
        "room_id": ident.room_id,
        "identity_source": ident.identity_source,
    }
    verified = bool(extra.get("verified"))
    changed = False
    for field, val in candidates.items():
        if not val:
            continue
        cur = entry.get(field)
        if not cur:
            entry[field] = val
            changed = True
        elif str(cur) != str(val) and field in ("principal_id", "origin_user_id"):
            # 主键级冲突：以用户配置为准，但必须留下痕迹
            logger.warning(
                "[kuaishou] config 里的 %s=%s 与解析结果 %s 不一致（校验=%s）—— "
                "以 config 为准；若通知里的人不对，请核对该字段",
                field, cur, val, "已通过" if verified else "未做",
            )
    return changed


def resolve_kuaishou_principal_id(entry: Any, rid: str,
                                  http_get: Optional[Callable[[str], bytes]] = None,
                                  tracking: Any = None) -> Optional[str]:
    """解析 principalId（兼容旧签名的薄封装，内部走 Identity Framework）。

    ``http_get`` 仅为不破坏既有调用方而保留：resolver 需要拿到重定向**终链**
    （分享链接的 userId 就藏在那），旧的 ``bytes`` 返回值表达不了这个信息。
    """
    ident = resolve_kuaishou_identity(entry, rid, tracking=tracking)
    return ident.principal_id if ident else None


_KUAISHOU_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
#: dynamicIcon 是 H5 小程序接口，必须用移动端 UA
_KUAISHOU_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)
_DEFAULT_CLIENT_KEY = "3c7cd4d734b53483"


def _to_ts(v: Any) -> Optional[int]:
    """尽力把时间值转成 epoch 秒（兼容 int / 字符串）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ts_to_bj(ts: Optional[int]) -> str:
    """epoch 秒 -> 北京时间字符串；失败返回空串。

    必须显式指定 +8 时区，不能用裸 ``datetime.fromtimestamp()``：
    GitHub Actions runner 默认 UTC 且 workflow 未设 ``TZ``，
    裸调用会把 UTC 时间当成北京时间写进 tracking，整体偏早 8 小时。
    """
    return epoch_to_beijing(ts)


def _now_bj() -> str:
    """当前北京时间字符串（显式 +8，理由同 :func:`_ts_to_bj`）。"""
    return bjnow().strftime("%Y-%m-%d %H:%M:%S")


class KuaishouAdapter(PlatformAdapter):
    platform = "kuaishou"
    supports_live = True
    supports_posts = True
    poll_interval = 300
    rate_limit = {"max_requests": 20, "window_sec": 60, "backoff_sec": 30}
    #: 新作必须走浏览器：``live_api/profile/public`` 只认**页面自身**发出的请求
    #: （带 JS 现算的 __NS_hxfalcon 签名 + 新鲜风控 cookie）。实测在页面上下文里
    #: 手动 fetch 同一 URL 连打 12 次全部 result=2，裸 HTTP 更是恒被挡。
    #: 直播仍以 SSR / dynamicIcon 为主，不依赖浏览器。
    needs_context = True

    def __init__(self, credentials: Optional[Dict[str, Any]] = None,
                 poll_interval: Optional[int] = None,
                 rate_limit: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(credentials or {}, poll_interval, rate_limit)
        self.did = str(self.credentials.get("did") or "")
        self.client_key = str(self.credentials.get("client_key") or _DEFAULT_CLIENT_KEY)
        self.cookie = str(self.credentials.get("cookie") or "")
        #: 最近一次取新作使用的凭证等级（编排层据此写 tracking）
        self.last_ladder = None
        #: 本适配器实例累计命中风控的次数
        self.gated_count = 0
        #: 跨账号复用的作品流会话（预热一次，全轮共享）
        self._feed_session: Optional[ks_feed.KuaishouFeedSession] = None

    # ---- 网络（可被测试 monkeypatch）----
    def _http_get(self, url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: int = 10) -> bytes:
        hdr = {"User-Agent": _KUAISHOU_UA, "Referer": "https://live.kuaishou.com/"}
        if self.cookie:
            hdr["Cookie"] = self.cookie
        elif self.did:
            hdr["Cookie"] = f"did={self.did}"
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    # ---------- 直播 ----------
    def fetch_room_status(self, room_id: str) -> RoomModel:
        """取快手直播间状态（SSR 解析为主，失败优雅降级 offline）。"""
        room_id = str(room_id)
        room: Optional[RoomModel] = None
        try:
            html = self._http_get(
                f"https://live.kuaishou.com/u/{room_id}", timeout=10
            ).decode("utf-8", "replace")
            room = self._room_from_html(room_id, html)
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 直播 SSR 解析失败，转 dynamicIcon 兜底: %s", e)

        # SSR 说没在播时也兜一次底：SSR 会被风控喂空页面，那种「静默 offline」
        # 与真的没开播长得一模一样，正是开播提醒漏报的典型成因。
        if room is None or not room.live_status:
            icon = self._live_via_dynamic_icon(room_id)
            if icon is not None:
                if icon["living"]:
                    logger.info("[kuaishou] %s SSR 判 offline，dynamicIcon 判在播，以后者为准",
                                room_id)
                return RoomModel(
                    platform="kuaishou", room_id=room_id,
                    name=room.name if room else "",
                    title=room.title if room else "",
                    live_status=icon["living"],
                    online=room.online if room else 0,
                    cover=room.cover if room else "",
                    avatar=room.avatar if room else "",
                    extra={"living": icon["living"], "source": "dynamic_icon",
                           "live_stream_id": icon["live_stream_id"],
                           "ssr_living": bool(room.live_status) if room else None},
                )

        if room is not None:
            return room
        return RoomModel(
            platform="kuaishou", room_id=room_id, live_status=False,
            extra={"degraded": True},
        )

    @staticmethod
    def _room_from_html(room_id: str, html: str) -> RoomModel:
        """SSR 解析 window.__INITIAL_STATE__（纯函数，便于单测）。

        兼容两种结构：
          - 真实结构：``liveroom.playList[0].isLiving`` + ``.liveStream.*``
          - 旧/兼容结构：``liveroom.living/caption/watcherCount/coverUrl``
        """
        state = _extract_initial_state(html)
        if not state:
            return RoomModel(platform="kuaishou", room_id=room_id, live_status=False)

        liveroom = state.get("liveroom") or {}
        play_list = liveroom.get("playList") or []

        living = False
        title = ""
        online = 0
        cover = ""

        if play_list:
            # 真实快手 SSR 结构（实测确认）
            pl0 = play_list[0] or {}
            living = bool(pl0.get("isLiving"))
            ls = pl0.get("liveStream") or {}
            title = ls.get("caption") or pl0.get("caption") or ""
            online = _as_int(
                ls.get("watcherCount") or ls.get("viewerCount")
                or pl0.get("watcherCount") or liveroom.get("watcherCount")
            )
            cover = (
                ls.get("coverUrl") or ls.get("poster")
                or pl0.get("coverUrl") or liveroom.get("coverUrl") or ""
            )
        else:
            # 兼容旧结构
            living = bool(liveroom.get("living"))
            if living:
                title = liveroom.get("caption") or ""
                online = _as_int(liveroom.get("watcherCount"))
                cover = liveroom.get("coverUrl") or ""

        user_name = KuaishouAdapter._extract_kuaishou_nickname(html, room_id)
        user_avatar = KuaishouAdapter._extract_kuaishou_avatar(html, room_id)

        return RoomModel(
            platform="kuaishou",
            room_id=room_id,
            name=user_name,
            title=title,
            live_status=living,
            online=online,
            cover=cover,
            avatar=user_avatar,
            extra={"living": living, "source": "ssr"},
        )

    @staticmethod
    def _extract_kuaishou_nickname(html: Any, room_id: str) -> str:
        """从直播页 SSR/HTML 提取主播昵称（优先匹配 room_id 对应的 author）。

        实测结构：``liveroom.playList[0].author`` 含 ``{"id": <room_id>, "name": <昵称>}``
        （如 Sandy88888 → {"id":"Sandy88888","name":"肥阿肥"}）。
        直接吃原始 HTML 做正则，规避 ``__INITIAL_STATE__`` 大对象解析不稳定与字段顺序差异。
        """
        if isinstance(html, (bytes, bytearray)):
            html = html.decode("utf-8", "replace")
        if not html:
            return ""
        # 主：author.id == room_id 的 name（精准命中本主播，避开推荐流其它 author）
        m = re.search(
            r'"author"\s*:\s*\{\s*"id"\s*:\s*"'
            + re.escape(room_id)
            + r'"\s*,\s*"name"\s*:\s*"([^"]*)"',
            html,
        )
        if m:
            return m.group(1)
        # 兜底：任一 author.name（无 id 匹配时的宽松回退）
        m = re.search(r'"author"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]*)"', html)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _extract_kuaishou_avatar(html: Any, room_id: str) -> str:
        """从直播页 SSR/HTML 提取主播头像 URL（与昵称同源，优先匹配 room_id 对应 author）。

        实测结构：``liveroom.playList[0].author`` 含 ``{"id": <room_id>, "name": <昵称>,
        "avatar": "https:\\u002F\\u002Fp2-pro.a.yximgs.com\\u002Fuhead\\u002F..._s.jpg"}``。
        快手 SSR 把 ``/`` 转义为 ``\\u002F``（也可能 ``\\/``），需还原为真实分隔符。
        直接吃原始 HTML 做正则，规避 ``__INITIAL_STATE__`` 大对象解析不稳定与字段顺序差异。
        """
        if isinstance(html, (bytes, bytearray)):
            html = html.decode("utf-8", "replace")
        if not html:
            return ""
        # 主：author.id == room_id 的 avatar（精准命中本主播，避开推荐流其它 author）
        m = re.search(
            r'"author"\s*:\s*\{\s*"id"\s*:\s*"'
            + re.escape(room_id)
            + r'"[^}]{0,400}?"avatar"\s*:\s*"([^"]*)"',
            html,
        )
        if m:
            return m.group(1).replace("\\u002F", "/").replace("\\/", "/")
        # 兜底：任一 author.avatar（无 id 匹配时的宽松回退）
        m = re.search(r'"author"\s*:\s*\{[^}]{0,400}?"avatar"\s*:\s*"([^"]*)"', html)
        if m:
            return m.group(1).replace("\\u002F", "/").replace("\\/", "/")
        return ""

    # ---------- 新作 ----------
    def _session(self, context: Any) -> ks_feed.KuaishouFeedSession:
        """取（或建）跨账号复用的作品流会话。

        复用是刚需不是优化：冷启动首个账号要重新导航 4~5 次才等到 ``result=1``，
        而同一 context 热起来后下一个账号第 1 次导航即命中。每账号各开一次
        浏览器等于每个都付一遍冷启动代价，还更容易撞风控。

        **默认匿名（不需要 cookie 的快手版本）**：不注入任何登录态/风控 cookie，
        完全依赖浏览器预热（访问 ``www.kuaishou.com``）自行种下 ``kwfv1``/
        ``kwssectoken``/``kwscode`` 等新鲜风控 token，再拦截页面自身发出的带签名
        ``live_api/profile/public`` 响应。这正是与 RSSHub 同思路的免登录路径——
        ``fetch_new_posts`` 也如实记录 ``cookie_used=False`` / ``credential_level=ANONYMOUS``。

        仅在 ``credentials.cookie`` 被显式提供时才注入（用于突破个别匿名仍被挡的账号，
        属可选增强而非必需项）；``credentials.cookie`` 为空时再回退到
        ``load_kuaishou_cookie()``（环境变量 / BLIVE_CONFIG 的可选覆盖）。
        """
        if self._feed_session is None:
            # 默认空 → 匿名；显式提供才注入。绝不默认加载已提交的仓库 cookie 文件。
            # load_kuaishou_cookie 走 check_new_posts（懒加载以避免循环导入）。
            from check_new_posts import load_kuaishou_cookie
            cookie = self.cookie or load_kuaishou_cookie()
            self._feed_session = ks_feed.KuaishouFeedSession(
                context, user_agent=_KUAISHOU_UA, kuaishou_cookie=cookie
            )
        return self._feed_session

    def close(self) -> None:
        """释放浏览器会话（编排层结束一轮后可调用；不调用也不会泄漏到下轮进程）。"""
        if self._feed_session is not None:
            self._feed_session.close()
            self._feed_session = None

    def fetch_new_posts(self, author_or_room: str, since: Optional[datetime] = None,  # noqa: C901
                        baseline: Optional[Dict[str, Any]] = None,
                        context: Any = None) -> List[PostModel]:
        """取快手新作品（``live_api/profile/public`` 浏览器通道）。

        与旧 graphql 实现的关键差异，每一条都是实测踩出来的：

        1. **必须浏览器**：没有 context 直接 raise AdapterGated，而不是退回裸请求
           假装尝试 —— 裸请求 100% 返回空，只会把「被挡」伪装成「没有新作」。
        2. **必须按时间排序**：接口把置顶作品放在列表最前，取 ``list[0]`` 会让
           「最新作品」永远卡在那条置顶上，新作永远发现不了。
        3. **必须校验归属**：用作品 URL 里反解出的 userId 交叉校验，防止抓到
           他人作品就直接推给用户（本项目在抖音上吃过这个亏）。
        """
        rid = str(author_or_room)
        t = baseline if isinstance(baseline, dict) else {}

        if not _looks_like_principal_id(rid):
            # 不是 principalId（多半是用户名）：接口会返回空列表，看着像「没更新」，
            # 实则是身份没解对。显式点破，否则这类问题会一直伪装成正常。
            logger.warning(
                "[kuaishou] %s 不是 principalId 形态，作品接口大概率返回空。"
                "请在 post_rooms.json 补 principal_id / share_url / seed_url", rid,
            )

        if context is None:
            self.gated_count += 1
            self._write_run_tracking(t, rid, success=False)
            raise AdapterGated(
                detail="快手作品检测需要浏览器上下文（接口只认页面自身发出的带签名请求）"
            )

        try:
            parsed = self._session(context).fetch(rid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[kuaishou] 作品抓取异常（记为 gated）: %s", e)
            self.gated_count += 1
            self._write_run_tracking(t, rid, success=False)
            raise AdapterGated(detail=f"快手作品接口请求失败：{e}")

        self.last_ladder = None
        # 该通道不使用登录态，如实记录为匿名等级
        t["credential_level"] = CredentialLevel.ANONYMOUS
        t["cookie_used"] = False
        t["did_used"] = False

        if not parsed.get("ok"):
            self.gated_count += 1
            self._write_run_tracking(t, rid, success=False)
            code = parsed.get("result")
            hint = "（result=2：风控预热未通过，通常重试下一轮即可）" if code == 2 else ""
            raise AdapterGated(
                detail=f"快手作品接口未返回列表{hint}：{parsed.get('detail') or f'result={code}'}"
            )

        items = parsed.get("items") or []

        # ---- 归属校验：宁可这轮不报，也不能把别人的作品当成目标账号的新作 ----
        expect_uid = str(t.get("origin_user_id") or "")
        expect_aid = str(t.get("unique_name") or "")
        trusted, why = ks_feed.verify_ownership(
            items, expect_author_id=expect_aid, expect_user_id=expect_uid
        )
        if not trusted:
            logger.warning("[kuaishou] %s 作品归属校验未通过：%s", rid, why)
            self._write_run_tracking(t, rid, success=False)
            raise AdapterSkip("poisoned", detail=f"作品归属校验未通过：{why}")

        # 首轮把反解到的 userId / 用户名固化下来，之后即可用作强校验（自举）
        for it in items:
            if it.get("user_id") and not t.get("origin_user_id"):
                t["origin_user_id"] = it["user_id"]
            if it.get("author_id") and not t.get("unique_name"):
                t["unique_name"] = it["author_id"]
        if parsed.get("author_name") and not t.get("nickname"):
            t["nickname"] = parsed["author_name"]
        if parsed.get("living") is not None:
            # 同一响应顺带给出直播态，写入 tracking 供直播链路对账
            t["living_hint"] = bool(parsed["living"])

        # 观测：固化本轮接口实际返回的列表（仅 id+时间戳）。2026-08 实测快手对
        # 不同地域访问者返回不同列表（同一账号海外出口比大陆出口少最新一条），
        # 没有这个字段，「为什么这轮没看到那条」永远无法回答。
        t["last_fetch_items"] = [
            {"id": it["photo_id"], "ts": it.get("timestamp")} for it in items
        ]

        latest = ks_feed.pick_latest(items)
        if latest is None:
            # 一条时间都解不出：不猜、不报，如实记为被挡（避免用置顶冒充最新）
            self.gated_count += 1
            self._write_run_tracking(t, rid, success=False)
            raise AdapterGated(detail="快手作品均无法解析发布时间，无法判断新旧")

        prev_id = str(t.get("latest_post_id") or "")
        prev_ts = _to_ts(t.get("latest_timestamp"))
        new_id = latest["photo_id"]
        new_ts = latest["timestamp"]

        # 基线回退保护：本轮「最新」比已有基线还旧，说明本轮列表是退化视图
        # （2026-08 实测：快手对不同地域出口返回不同列表，海外出口会少最新一条）。
        # 此时保持基线不动：否则「最新作品」会被回写成旧帖，下一轮正常抓取又把
        # 同一条旧帖当新作重推，来回抖动。代价：作者删除最新作品后基线不会自动
        # 回落（需人工清 tracking 重置），两害相权取其轻。
        if prev_id and new_id != prev_id and prev_ts is not None \
                and new_ts is not None and new_ts < prev_ts:
            logger.warning(
                "[kuaishou] %s 本轮列表缺少已知更新的作品（基线 %s/%s，本轮最新 %s/%s），"
                "疑似退化列表（地域/风控差异），基线保持不变",
                rid, prev_id, _ts_to_bj(prev_ts), new_id, _ts_to_bj(new_ts),
            )
            # 标记本轮退化，供 check_new_posts 触发「静默漏检」告警（§4.4）。
            # 否则 handle_kuaishou_posts 只会看到「无新作」而静默放过。
            t["degraded_this_round"] = True
            self._write_run_tracking(t, rid, success=True)
            return []

        out: List[PostModel] = []
        is_new = bool(new_id) and new_id != prev_id and (prev_ts is None or new_ts > prev_ts)
        if is_new and prev_id:
            # 只在有基线时推送：首轮建基线不推历史作品，避免用户被一次性刷屏
            caption = ""
            try:
                caption = self._session(context).fetch_caption(new_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("[kuaishou] 补取文案失败: %s", e)
            out.append(PostModel(
                platform="kuaishou",
                post_id=new_id,
                author=t.get("nickname", "") or latest.get("author_name", ""),
                url=ks_feed.photo_url(new_id),
                cover=latest.get("cover", ""),
                published_at=_ts_to_bj(new_ts),
                title=caption or latest.get("music_name", ""),
                extra={
                    "conf": "api",
                    "type": "图文" if latest.get("is_image") else "视频",
                    "dedup_key": f"post:kuaishou:{new_id}",
                    "source": "profile_public",
                },
            ))
        elif is_new and not prev_id:
            logger.info("[kuaishou] %s 首轮建立基线（最新作品 %s），不推送历史作品",
                        rid, new_id)

        # 更新基线（始终以「按时间排序后的最新」为准，不是列表第一条）
        t["latest_post_id"] = new_id
        t["latest_timestamp"] = new_ts
        t["latest_published_at"] = _ts_to_bj(new_ts)
        if latest.get("cover"):
            t["latest_cover"] = latest["cover"]
        # 作品卡字段：每次成功抓取都回填（不只建基线），避免 handle_kuaishou_posts
        # 在「无新作」轮因 out 为空提前返回、导致作品卡缺文案/链接/类型/头像。
        t["latest_url"] = ks_feed.photo_url(new_id)
        t["latest_type"] = "图文" if latest.get("is_image") else "视频"
        _av = parsed.get("author_avatar") or latest.get("author_avatar") or ""
        if _av:
            t["avatar"] = _av
        # 文案：仅当尚未抓到时才补取（避免每轮都发浏览器请求）；
        # 建基线/历史首次抓取会走这一步，之后 latest_desc 非空即跳过。
        if not t.get("latest_desc"):
            _cap = ""
            try:
                _cap = self._session(context).fetch_caption(new_id)
            except Exception as _e:  # noqa: BLE001
                logger.debug("[kuaishou] 补取文案失败 %s: %s", new_id, _e)
            t["latest_desc"] = _cap or latest.get("music_name", "")
        self._write_run_tracking(t, rid, success=True)
        return out

    def _write_run_tracking(self, t: Dict[str, Any], rid: str, success: bool) -> None:
        """写入本轮运行观测字段（任务七）。

        这些字段不参与去重判断，纯粹是为了让「为什么没抓到」可回答：
        是身份没解对、还是被风控、还是要更高等级的凭证。
        """
        if not isinstance(t, dict):
            return
        # 入参即 principalId 时固化下来，避免下一轮重复解析
        if _looks_like_principal_id(rid):
            t.setdefault("principal_id", rid)
        if success:
            t["last_success"] = _now_bj()
        ladder = self.last_ladder
        if ladder is not None:
            t["cookie_used"] = ladder.level_used == CredentialLevel.COOKIE
            t["did_used"] = ladder.level_used == CredentialLevel.DEVICE
            t["credential_level"] = ladder.level_used or ""
        if self.gated_count:
            t["gated_count"] = self.gated_count

    # ---------- 直播兜底：dynamicIcon（免 Cookie 免验证码）----------
    def _live_via_dynamic_icon(self, principal_id: str) -> Optional[Dict[str, Any]]:
        """用 H5 小程序接口查直播态，SSR 解析失败时兜底。

        实测这是快手少见的**完全不设防**的接口：裸 POST、无 Cookie、无验证码、
        无签名，直接返回 ``{"result":1,"data":{"isLiving":bool,"liveStreamId":str}}``，
        两个账号双向验证过（一个在播一个没播，结果都准）。比作品接口简单得多，
        所以直播链路不必依赖浏览器。

        Returns:
            ``{"living": bool, "live_stream_id": str}``；请求失败或响应异常返回 None
            （调用方保持原判定，不因兜底失败而误判）。
        """
        try:
            body = json.dumps({"userId": str(principal_id)}).encode("utf-8")
            req = urllib.request.Request(
                "https://v.m.chenzhongtech.com/rest/wd/ugH5App/user/dynamicIcon",
                data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": _KUAISHOU_MOBILE_UA,
                    "Referer": "https://v.m.chenzhongtech.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                payload = json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            logger.debug("[kuaishou] dynamicIcon 兜底失败: %s", e)
            return None
        if not isinstance(payload, dict) or payload.get("result") != 1:
            return None
        data = payload.get("data") or {}
        if not isinstance(data, dict) or "isLiving" not in data:
            return None
        return {
            "living": bool(data.get("isLiving")),
            "live_stream_id": str(data.get("liveStreamId") or ""),
        }


#: 快手 SSR 里会出现的 JS 专有字面量（非合法 JSON），解析失败时替换为 null 重试。
#: 前后用 (?<!["\w]) / (?!["\w]) 守卫，避免误伤字符串内容里的同名单词。
_JS_LITERAL_RE = re.compile(
    r'(?<![\w"])(?:undefined|NaN|-?Infinity|void 0)(?![\w"])'
)


def _as_int(v: Any) -> int:
    """把可能是字符串/数字的值安全转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _extract_initial_state(html: Any) -> Optional[Dict[str, Any]]:
    """从 HTML 稳健提取 window.__INITIAL_STATE__ 的对象（括号配平，兼容嵌套）。

    两个必须处理的坑（2026-08 实测踩到）：

    1. **括号可能出现在字符串字面量里**（标题/描述常含 ``{``），朴素配平会提前收尾，
       所以扫描时要跳过引号内内容并正确处理反斜杠转义。
    2. **快手 SSR 是 JS 对象字面量，不是严格 JSON**：实测页面里有
       ``"authToken":undefined``，``json.loads`` 直接抛 JSONDecodeError。
       此前该异常被吞掉返回 None，导致 ``fetch_room_status`` 把**正在直播的房间
       静默判成 offline**（开播提醒漏报的真凶）。这里对 ``undefined`` / ``NaN`` /
       ``Infinity`` / ``void 0`` 做一次保守替换后重试。

    失败返回 None（调用方降级为 offline）。
    """
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", "replace")
    if not isinstance(html, str):
        return None
    marker = "window.__INITIAL_STATE__"
    idx = html.find(marker)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    end = -1
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    raw = html[start:end + 1]
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001 —— 大概率是 JS 字面量，降级重试
        pass
    try:
        return json.loads(_JS_LITERAL_RE.sub("null", raw))
    except Exception as e:  # noqa: BLE001
        logger.debug("[kuaishou] SSR 状态解析失败: %s", e)
        return None
