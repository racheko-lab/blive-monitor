#!/usr/bin/env python3
"""
抖音新作品检测（GitHub Actions 用，独立于直播监控）

设计说明：
- 本脚本与直播监控完全解耦：直播监控读 rooms.json、写 tracking.json/state.json；
  本脚本只读 post_rooms.json（独立的抖音号列表），写 post_tracking.json。
- 抖音的作品列表接口（aweme/v1/web/aweme/post/）现在强制要求 X-Bogus / a_bogus 签名 +
  WebID / 登录态，纯服务端 urllib 或无头浏览器裸调都会返回空列表（被风控）。
  因此本脚本采用「三层策略 + 优雅降级」：
    策略 0（首选，免 Cookie）：移动端老接口 m.douyin.com/web/api/v2/aweme/post/
            无需登录即返回真实作品列表（含 aweme_id/desc/视频或图文链接），
            所有账号通用，作为精确检测的首选路径。
    策略 1（需登录 Cookie）：在无头浏览器里打开用户主页，拦截页面【自身】发出的、
            已带签名的 aweme/post 请求响应（浏览器自动生成 a_bogus/msToken/webid，无需逆向）。
            配置 douyin_cookie 后该响应返回真实作品列表（含 create_time/desc），
            可精确推送「X 发布了新作品」并链接到具体作品。
    策略 2（退化，无需 Cookie）：解析 user/profile/other 的 aweme_count。
            经验证该接口在【未登录】时仍返回真实作品总数（status_code:0），
            作品数增加时推测「可能有新作品」，推送一条带主页链接的提示请用户自行确认。
- 为什么不再用「主页 DOM 提取作品链接」：经验证，无登录态时用户主页几乎全是被推荐流占据，
  所谓「干净链接」（不带 source=Baiduspider）每次加载都会变化，无法可靠区分用户自身作品
  与他者推荐视频，据此推送会造成大量误报，故已弃用该策略。
- 两层都拿不到（被风控/未登录且 profile 接口也异常）时，明确打印提示并保留基线，不静默、不刷屏。
- 每个账号的 sec_uid 在本脚本内自行解析（优先用已存值 / post_rooms.json 直存值；否则从直播页
  的【房间主人 anchor】结构化字段提取，绝不取推荐流），不依赖直播监控产物。
  解析后对「实际账号」做中毒防护：用已捕获 profile 的 unique_id 校验 sec_uid 是否真对应本 handle，
  若被推荐流污染则跳过并清除毒值，避免误监控陌生人。
- 通过多渠道推送（见 push_utils），启用开关：环境变量 ENABLE_POST_CHECK=true。
"""

import json
import os
import re
import time
import logging
from typing import Dict, List, Optional, Any, Tuple

# 公共工具（时间/JSON 读写），避免与 check_status.py 重复定义
from common import (
    bjnow,
    load_json_file,
    save_json_file,
    BEIJING_TZ,
    room_enabled,
    load_silence_cfg,
    should_skip_by_silence,
    content_hash,
    DEFAULT_USER_AGENT,
    DEFAULT_MOBILE_USER_AGENT,
)
import common  # A2/A4 统一路由：common.resolve_channel（dispatch_event 同源）
# 推送实现见 push_utils.py（直播监控与新作品监控共用）
# 统一路由入口直接复用 push_utils.dispatch_event（与 check_status/auto_summary 同范式，不再本地薄封装）
from push_utils import SendResult, load_push_cfg, channel_to_push_cfg, dispatch_event
# 通知去重账本：与 post_tracking.json 持久化解耦，同一作品永久不重复推送
from notify_dedup import should_notify as dedup_should_notify, record as dedup_record, prune as dedup_prune, sync_from_remote as dedup_sync_from_remote
# 横切模块：运行时日志（init_runtime_logging）+ 统一 history 读写/上限/节流
from log_utils import (
    init_runtime_logging, append_history, dedupe_by_throttle,
    HISTORY_MAX, EVENT_TYPES, level_from_type,
)
# 级联清理（post_tracking 孤儿 / post_rooms 字段合并，替代原内联应急补丁）
import state_prune
# 适配器异常（快手新作被风控 → AdapterGated，等价原 cookie_warn）
from backend.adapters.base import AdapterGated

# ==================== 常量配置 ====================

# 文件路径（与本脚本同目录）
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(REPO_DIR, "post_rooms.json")      # 作品监控的抖音号列表（独立）
TRACKING_FILE = os.path.join(REPO_DIR, "post_tracking.json")  # 作品监控状态（独立）
HISTORY_FILE = os.path.join(REPO_DIR, "history.json")         # 统一日志（与 check_status 同文件）

# 浏览器配置
BROWSER_TIMEOUT = 30000   # 页面加载超时（ms）
SETTLE_WAIT = 6000        # 主页加载后等待 SPA 渲染（ms）

# 移动端 UA / 视口：用于访问 m.douyin.com 的老接口 web/api/v2/aweme/post/，
# 该接口**无 Cookie 即返回真实作品列表**（含 aweme_id/desc/视频或图文链接），
# 是所有账号通用、无需登录的「精确检测」首选路径。
MOBILE_UA = DEFAULT_MOBILE_USER_AGENT
MOBILE_VIEWPORT = {"width": 390, "height": 844}

# ==================== 日志配置 ====================

# 统一走 log_utils 的运行时日志（控制台 + 文件轮转 + 结构化上下文）。
# 其余 logger.info/warning/error 调用零改动（account 缺省空串，兼容既有输出解析）。
init_runtime_logging()
logger = logging.getLogger(__name__)


# ==================== 新作品基线判定（纯函数，便于单测） ====================

def _post_is_newer(prev_id: str, prev_ct: int, new_id: str, new_ct: int) -> bool:
    """判断 new 是否比 prev 更新。

    优先用 create_time；若任一方缺 create_time（例如从 DOM 提取、或接口未返回），
    退化为按 aweme_id 数值比较——抖音作品 id 近似单调递增，新作品 id 更大。
    """
    if prev_ct and new_ct:
        return new_ct > prev_ct
    try:
        return int(new_id or 0) > int(prev_id or 0)
    except (ValueError, TypeError):
        return bool(new_id) and new_id != prev_id


def should_notify_new_post(
    prev_id: str, prev_ct: int, new_id: str, new_ct: int
) -> bool:
    """是否应就「新作品」推送。

    规则：
    - 首次（无基线 prev_id）仅建立基线，不推送（避免启用即轰炸）；
    - 同一作品（id 相同）不重复推送；
    - 仅当接口返回的作品「确实比基线更新」时才视为新作品并推送。
      否则（接口返回的反而更旧，即抖音接口尚未收录我们已知的更新作品，属 feed 延迟）
      只静默保留已有基线，不误推送、也不回退显示。
    """
    if not prev_id:
        return False
    if prev_id == new_id:
        return False
    return _post_is_newer(prev_id, prev_ct, new_id, new_ct)


def should_update_baseline(prev_id: str, prev_ct: int, new_id: str, new_ct: int) -> bool:
    """是否用本次结果覆盖基线。

    - 首次建立基线；
    - 同一作品（id 相同）：刷新 ct/desc；
    - 不同作品：仅当「更新」时覆盖基线（避免抖音接口延迟导致回退到旧作品，
      也避免 API→DOM 过渡时把更旧的 DOM 结果覆盖掉更优的 API 基线）。
    """
    if not prev_id:
        return True
    if prev_id == new_id:
        return True
    return _post_is_newer(prev_id, prev_ct, new_id, new_ct)


# ==================== 抖音 Cookie（可选，突破风控的关键） ====================

def load_douyin_cookie() -> str:
    """读取抖音登录 Cookie（可选）。

    优先环境变量 DOUYIN_COOKIE；其次 BLIVE_CONFIG 里的 douyin_cookie 字段。
    没有则返回空串——此时抖音接口会被风控，脚本会优雅降级（见 get_latest_aweme）。
    """
    env = os.environ.get("DOUYIN_COOKIE", "").strip()
    if env:
        return env
    raw = os.environ.get("BLIVE_CONFIG", "{}")
    try:
        cfg = json.loads(raw) if raw else {}
    except Exception:
        cfg = {}
    return (cfg.get("douyin_cookie") or "").strip()


def load_kuaishou_cookie() -> str:
    """读取快手 Cookie（可选覆盖，非必需）。

    快手新作监控**默认不需要任何 cookie**：走浏览器匿名通道
    （``live_api/profile/public`` + 浏览器预热种新鲜风控 token），由
    ``KuaishouAdapter._session`` 默认以空 cookie 启动。本函数仅在**显式需要**
    突破个别匿名仍被挡的账号时才提供 cookie，来源（从高到低）：

      1. 环境变量 KUAISHOU_COOKIE
      2. BLIVE_CONFIG 里的 kuaishou_cookie 字段

    没有则返回空串 —— 此时快手走免 Cookie 匿名通道（与抖音 DOUYIN_COOKIE 同理，
    但快手默认即为匿名，无需任何登录态）。

    注：历史上曾把登录态 cookie 作为 ``config/kuaishou_cookie.txt`` 提交进公开仓库，
    既暴露凭证又易 stale；现已废弃该文件通道，快手默认即为无 cookie 版本。
    """
    env = os.environ.get("KUAISHOU_COOKIE", "").strip()
    if env:
        return env
    raw = os.environ.get("BLIVE_CONFIG", "{}")
    try:
        cfg = json.loads(raw) if raw else {}
    except Exception:
        cfg = {}
    cookie = (cfg.get("kuaishou_cookie") or "").strip()
    if cookie:
        return cookie
    return ""


def load_browser_proxy() -> Optional[Dict[str, str]]:
    """读取浏览器出口代理（可选），把作品抓取流量走到指定地域出口。

    优先级：环境变量 BROWSER_PROXY > BLIVE_CONFIG.browser_proxy。
    格式：``http://host:port`` 或 ``http://user:pass@host:port``。

    为什么需要它（2026-08 实测，排查「Nizi981116 抓不到最新」时确认）：
    快手对不同地域的访问者返回**不同的作品列表** —— 同一账号，大陆出口 IP
    能拿到全部作品（含最新一条），GitHub Actions 的海外出口拿到的列表少
    最新一条（接口还返回 pcursor=no_more，伪装成「没有更多」）。监控跑在
    海外 runner 上就会永远漏掉这类作品。配置大陆出口代理后，Chromium 的
    全部抓取流量走代理即可拿到完整列表。未配置返回 None（直连，行为不变）。
    """
    raw = os.environ.get("BROWSER_PROXY", "").strip()
    if not raw:
        try:
            cfg = json.loads(os.environ.get("BLIVE_CONFIG", "{}") or "{}")
        except Exception:
            cfg = {}
        raw = str(cfg.get("browser_proxy") or "").strip()
    if not raw:
        return None
    from urllib.parse import urlparse
    try:
        u = urlparse(raw if "://" in raw else f"http://{raw}")
        if not u.hostname or not u.port:
            raise ValueError("缺 host 或端口")
        out: Dict[str, str] = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port}"}
        if u.username:
            out["username"] = u.username
        if u.password:
            out["password"] = u.password
        logger.info("浏览器抓取走代理: %s", out["server"])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("BROWSER_PROXY 格式不合法，忽略并直连: %r (%s)", raw[:80], e)
        return None


def apply_douyin_cookie(context, cookie_str: str) -> None:
    """把 Cookie 字符串拆成单条写入浏览器上下文（仅当配置了才调用）。

    cookie_str 形如 "sessionid=xxx; passport_csrf_token=yyy; sid_tt=zz"
    """
    if not cookie_str:
        return
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies.append({
            "name": k.strip(),
            "value": v.strip(),
            "domain": ".douyin.com",
            "path": "/",
        })
    if cookies:
        try:
            context.add_cookies(cookies)
            logger.info("已注入抖音登录 Cookie（%d 条），可突破作品接口风控", len(cookies))
        except Exception as e:
            logger.warning("注入抖音 Cookie 失败: %s", e)


# ==================== sec_uid 解析 ====================

# 房主 sec_uid 统一正则（供 extract_host_sec_uid / resolve_sec_uid 复用）
SEC_RE = re.compile(r"MS4wLjABAAAA[A-Za-z0-9_\-]+")


def is_sec_uid(s: str) -> bool:
    """判断字符串是否形如抖音 sec_uid（MS4w 开头）。"""
    return bool(s) and s.startswith("MS4w")


def looks_like_handle(s: str) -> bool:
    """判断字符串是否像抖音 handle（非纯数字、非 sec_uid）。

    用于「中毒防护」时决定是否用 profile 的 unique_id 反查校验：
    纯数字 id（如用户填的抖音数字号）无法与 unique_id 直接比对，此时信任直播页
    房主 anchor 解析出的 sec_uid，不做反查，避免误杀正确账号。
    """
    return bool(s) and not s.isdigit() and not is_sec_uid(s)


def extract_host_sec_uid(html: str) -> Optional[str]:
    """从直播页 HTML 提取【房主本人】的 sec_uid（纯函数，便于单测）。

    抖音直播页的房间主人信息嵌在结构化 JSON 中，形如::

        "anchor":{"id_str":"...","sec_uid":"MS4w...","nickname":"..."}

    该 ``anchor`` 字段始终位于推荐流之前，是房主本人。

    注意：**绝不可**对整页 HTML 用 ``re.search(r"MS4w...")`` 取「第一个 sec_uid」——
    离线页 / 推荐流里也充斥大量其他主播的 MS4w，会取到陌生人的 sec_uid，导致基线全错。
    旧版还曾用 ``a[href*="/user/"]`` 链接循环，但推荐流的 ``/user/`` 链接可能排在房主之前，
    同样会误取。这里只认房间主人的 ``anchor`` 结构化字段，确保拿到的是本人。

    Args:
        html: 直播页完整 HTML

    Returns:
        房主 sec_uid；取不到返回 None
    """
    if not html:
        return None
    # 房主结构化字段的 sec_uid 可能以两种形态出现：
    #   (A) 未转义 JSON：  "anchor":{"id_str":"...","sec_uid":"MS4w..."}
    #   (B) RENDER_DATA 转义形态（引号被转义，花括号不转义）：
    #                     \"anchor\":{\"id_str\":\"...\",\"sec_uid\":\"MS4w...\"}
    # 两种形态都只认「房间主人」字段（anchor / roomInfo / owner / or / anchorInfo），
    # 绝不对整页取「第一个 MS4w」——离线页/推荐流里充斥大量他者 sec_uid，会误取陌生人。
    # 注：用 [^{}]* 而非 [^}]*：anchor 对象内除 sec_uid 外无嵌套花括号，
    # 用 [^}]* 会贪婪吞掉 "sec_uid":"..." 导致匹配失败。
    SEC = r"(MS4wLjABAAAA[A-Za-z0-9_\-]+)"
    # 顺序：未转义优先，转义兜底；anchor 优先，其余结构化字段兜底。
    patterns = [
        # (A-1) 未转义 anchor（最精准，房主本人）
        r'"anchor"\s*:\s*\{[^{}]*?"sec_uid"\s*:\s*"' + SEC + r'"',
        # (A-2) 未转义 roomInfo/owner/or/anchorInfo/anchor
        r'"(?:roomInfo|owner|or|anchorInfo|anchor)"\s*:\s*\{[^{}]*?"sec_uid"\s*:\s*"' + SEC + r'"',
        # (B-1) 转义 anchor（RENDER_DATA 形态：\"anchor\":\{...\}）
        r'\\"anchor\\"\s*:\s*\{[^{}]*?\\"sec_uid\\"\s*:\s*\\"' + SEC + r'\\"',
        # (B-2) 转义 roomInfo/owner/or/anchorInfo/anchor
        r'\\"(?:roomInfo|owner|or|anchorInfo|anchor)\\"\s*:\s*\{[^{}]*?\\"sec_uid\\"\s*:\s*\\"' + SEC + r'\\"',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def resolve_sec_uid(context, entry_id: str) -> Optional[str]:
    """解析某抖音号的真实 sec_uid（房主本人，绝不取推荐流）。

    解析顺序：
      1) entry_id 本身已是 sec_uid（MS4w 开头）→ 直接用；
      2) 打开直播页，从「房间主人 anchor」结构化字段提取（开播 / 离线均可，房主本人）；
      3) 兜底：拦截直播页自动发出的 user/profile/other 响应，取出房主 sec_uid；
      4) 都拿不到 → 返回 None（本次跳过该账号，避免监控陌生人）。

    说明：直播页的房主 anchor 字段在开播 / 离线两种状态下都会随页面下发
    （经验证，离线页的 RENDER_DATA 转义形态里同样含房主 sec_uid），因此该路径对
    「纯发视频、不直播」的账号也有效——这是前端网页添加的账号能正确解析的关键。

    Args:
        context: Playwright BrowserContext
        entry_id: post_rooms.json 里的 id（可能是 sec_uid、抖音 handle 或数字号 web_rid）

    Returns:
        sec_uid 字符串，失败返回 None
    """
    if is_sec_uid(entry_id):
        return entry_id

    url = f"https://live.douyin.com/{entry_id}"
    page = context.new_page()
    captured: Dict[str, str] = {}

    def on_resp(resp):
        # 兜底：直播页自动签发的 user/profile/other 响应里同时含 sec_uid 与 unique_id。
        # 仅当 unique_id 与 entry_id 一致（或页面未给 unique_id）时才采用，避免取错账号。
        u = resp.url
        if "user/profile/other" in u:
            try:
                body = resp.body().decode("utf-8", "replace")
                uid = parse_profile_handle(body)
                m = SEC_RE.search(body)
                if m and (not uid or uid == entry_id):
                    captured["profile"] = m.group(1)
            except Exception:
                pass

    try:
        page.on("response", on_resp)
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        page.wait_for_timeout(3000)
        host = extract_host_sec_uid(page.content())
        if host:
            return host
        if captured.get("profile"):
            return captured["profile"]
        logger.warning("  [%s] 直播页未找到房主 sec_uid（可能页面未渲染，下次重试）", entry_id)
        return None
    except Exception as e:
        logger.warning("  [%s] 解析 sec_uid 失败: %s", entry_id, e)
        return None
    finally:
        page.close()


# ==================== 解析辅助（纯函数，便于单测） ====================

def _extract_cover(w: Dict[str, Any]) -> Optional[str]:
    """从 aweme 作品对象提取封面 URL。

    视频封面字段随接口/版本变化：桌面端 API 多在 ``video.origin_cover`` /
    ``video.animated_cover`` / ``video.dynamic_cover``（dict 含 ``url_list``），
    移动端 v2 接口当前直接在 ``video.cover``（dict 含 ``url_list``，偶为字符串直链）。
    图文取 ``images[0]``。无则返回 None。
    """
    v = w.get("video") or {}
    for key in ("origin_cover", "animated_cover", "dynamic_cover", "cover"):
        c = v.get(key)
        if isinstance(c, dict):
            urls = c.get("url_list") or []
            if urls:
                return urls[0]
        elif isinstance(c, str) and c.startswith("http"):
            return c
    imgs = w.get("images") or []
    if imgs:
        im = imgs[0] or {}
        if isinstance(im, dict):
            return (im.get("url_list") or [None])[0] or im.get("url")
    return None


def _extract_author_avatar(w: Dict[str, Any]) -> Optional[str]:
    """从 aweme 作品的 author 对象提取头像 URL。

    抖音 aweme_list[].author 与 user/profile/other 的 user 对象结构一致，
    均含 avatar_url（高清）/ avatar_medium / avatar_thumb（dict 含 url_list）。
    取首个可用 URL（url_list[0]）。无则返回 None。

    作为 parse_profile_avatar 的 fallback：当 user/profile/other 响应未被拦截
    或被风控时，仍可从作品列表的 author 拿到头像供前端展示。
    """
    author = w.get("author") or {}
    if not isinstance(author, dict):
        return None
    for key in ("avatar_url", "avatar_medium", "avatar_thumb"):
        v = author.get(key)
        if isinstance(v, dict):
            urls = v.get("url_list") or []
            if urls and isinstance(urls[0], str) and urls[0].startswith("http"):
                return urls[0]
        elif isinstance(v, str) and v.startswith("http"):
            return v
    return None


def parse_aweme_list(json_text: str) -> List[Dict[str, Any]]:
    """从 aweme/post 响应体解析作品列表，返回标准化 dict 列表。

    每个 dict: {aweme_id, desc, video_url, is_note, nickname, create_time, avatar}
    avatar 取自 author 对象，供前端横条视图显示真实头像（profile 拦截失败时的 fallback）。
    空/风控/异常返回 []。
    """
    if not json_text:
        return []
    try:
        data = json.loads(json_text)
    except Exception:
        return []
    # 风控/未登录：status_code 非 0 且无作品列表
    if data.get("status_code", 0) not in (0, None) and not data.get("aweme_list"):
        return []
    items = data.get("aweme_list") or []
    out: List[Dict[str, Any]] = []
    for w in items:
        aid = str(w.get("aweme_id", "") or "")
        if not aid:
            continue
        is_note = bool(w.get("images"))
        link = "note" if is_note else "video"
        out.append({
            "aweme_id": aid,
            "desc": w.get("desc", "") or "",
            "video_url": f"https://www.douyin.com/{link}/{aid}",
            "is_note": is_note,
            "nickname": (w.get("author") or {}).get("nickname", "") or "",
            "create_time": int(w.get("create_time", 0) or 0),
            "cover": _extract_cover(w),
            "avatar": _extract_author_avatar(w),
        })
    return out


def parse_aweme_count(profile_text: str) -> Optional[int]:
    """从 user/profile/other 响应体解析作品总数（aweme_count）。解析失败返回 None。"""
    if not profile_text:
        return None
    try:
        data = json.loads(profile_text)
    except Exception:
        return None
    user = data.get("user") or (data.get("data") or {}).get("user") or {}
    if not isinstance(user, dict):
        return None
    cnt = user.get("aweme_count")
    return int(cnt) if isinstance(cnt, int) else None


def parse_profile_handle(profile_text: str) -> Optional[str]:
    """从 user/profile/other 响应体解析账号唯一 handle（unique_id）。

    用于「中毒防护」：把拿到的 sec_uid 打开主页后，校验 profile 里的 unique_id 是否等于
    post_rooms.json 里期望的 handle。若不一致，说明该 sec_uid 来自推荐流陌生人，需清除重解。
    解析失败 / 无 unique_id 返回 None（交由上层决定是否跳过）。
    """
    if not profile_text:
        return None
    try:
        data = json.loads(profile_text)
    except Exception:
        return None
    user = data.get("user") or (data.get("data") or {}).get("user") or {}
    if not isinstance(user, dict):
        return None
    uid = user.get("unique_id")
    return uid if isinstance(uid, str) and uid else None


def parse_profile_nickname(profile_text: str) -> Optional[str]:
    """从 user/profile/other 响应体解析账号真实昵称（nickname）。

    用于「前端显示昵称」：用户通过前端添加抖音号时往往只填了 id（handle/数字号），
    没填昵称；此处用主页接口返回的真实昵称回填，使前端展示「峰哥亡命天涯」而非裸 id。
    解析失败 / 无昵称返回 None（交由上层决定是否保留已有值）。
    """
    if not profile_text:
        return None
    try:
        data = json.loads(profile_text)
    except Exception:
        return None
    user = data.get("user") or (data.get("data") or {}).get("user") or {}
    if not isinstance(user, dict):
        return None
    nick = user.get("nickname")
    return nick if isinstance(nick, str) and nick.strip() else None


def parse_profile_avatar(profile_text: str) -> Optional[str]:
    """从 user/profile/other 响应体解析账号头像 URL（avatar_url）。

    用于前端横条视图显示真实头像（替代首字母圆）。解析失败返回 None。
    抖音 user 对象的头像字段有多个：avatar_url（高清）/ avatar_thumb（缩略）/ avatar_medium，
    取首个可用 URL（url_list[0]）。
    """
    if not profile_text:
        return None
    try:
        data = json.loads(profile_text)
    except Exception:
        return None
    user = data.get("user") or (data.get("data") or {}).get("user") or {}
    if not isinstance(user, dict):
        return None
    for key in ("avatar_url", "avatar_medium", "avatar_thumb"):
        v = user.get(key)
        if isinstance(v, dict):
            urls = v.get("url_list") or []
            if urls and isinstance(urls[0], str) and urls[0].startswith("http"):
                return urls[0]
        elif isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _sort_key(it: Dict[str, Any]) -> Tuple[int, int]:
    """取最新作品：优先 create_time，缺失时退化为 aweme_id 数值（近似时间序）。"""
    ct = int(it.get("create_time") or 0)
    aid = int(it.get("aweme_id") or 0)
    return (ct, aid)


# ==================== 浏览器抓取（多策略） ====================

def get_latest_aweme(context, sec_uid: str) -> Optional[Dict[str, Any]]:
    """三层策略获取用户最新作品，返回标准化 dict（含 _conf 置信度）或 None。

    策略顺序（越靠前越精确）：
      0) 移动端老接口 m.douyin.com/web/api/v2/aweme/post/：**无 Cookie 即返回真实作品列表**
         （aweme_id / desc / 视频或图文链接），所有账号通用，作为首选精确检测；
      1) 桌面端 aweme/v1/web/aweme/post/（需登录 Cookie 才返回真实列表，含 create_time）；
      2) 退化 user/profile/other 的 aweme_count（无需 Cookie，作品数增加推测「可能有新作品」）。

    无论走哪条策略，都会额外在桌面端捕获 user/profile/other 拿到 unique_id，
    供 main() 做「中毒防护」校验 sec_uid 是否真对应本账号。

    注：移动端 v2 接口不返回 create_time，排序退化为按 aweme_id 数值（近似时间序，
    抖音作品 id 单调递增），_post_is_newer 已支持该降级。
    """
    # ---------- 策略 0：移动端 v2 接口（无 Cookie 直出真实作品）----------
    mctx = context.browser.new_context(
        user_agent=MOBILE_UA, viewport=MOBILE_VIEWPORT, is_mobile=True, locale="zh-CN",
    )
    mcap: Dict[str, str] = {}
    try:
        mpage = mctx.new_page()

        def on_m(resp):
            if "web/api/v2/aweme/post" in resp.url:
                try:
                    mcap["post"] = resp.body().decode("utf-8", "replace")
                except Exception:
                    pass

        mpage.on("response", on_m)
        mpage.goto(
            f"https://m.douyin.com/share/user/{sec_uid}",
            wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT,
        )
        mpage.wait_for_timeout(SETTLE_WAIT)
    except Exception as e:
        logger.warning("  [%s] 移动端接口异常: %s", sec_uid[:12], e)
    finally:
        mctx.close()

    # ---------- 策略 1/2：桌面端（Cookie 精确接口 + 作品数/unique_id 兜底）----------
    page = context.new_page()
    dcap: Dict[str, str] = {}

    def on_resp(resp):
        u = resp.url
        try:
            if "/aweme/v1/web/aweme/post/" in u:
                dcap["post"] = resp.body().decode("utf-8", "replace")
            elif "user/profile/other" in u:
                dcap["profile"] = resp.body().decode("utf-8", "replace")
        except Exception:
            pass

    try:
        page.on("response", on_resp)
        page.goto(
            f"https://www.douyin.com/user/{sec_uid}",
            wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT,
        )
        page.wait_for_timeout(SETTLE_WAIT)

        actual_uid = parse_profile_handle(dcap.get("profile"))
        actual_nick = parse_profile_nickname(dcap.get("profile"))
        actual_avatar = parse_profile_avatar(dcap.get("profile"))

        # 策略 0 优先：移动端真实作品（无 Cookie）
        items = parse_aweme_list(mcap.get("post")) if mcap.get("post") else []
        if items:
            best = max(items, key=_sort_key)
            best["_conf"] = "api"
            best["_src"] = "mobile"
            best["actual_unique_id"] = actual_uid
            # 真实昵称优先用作品作者，缺失时回退到主页 profile/other 的真实昵称
            best["nickname"] = best.get("nickname") or actual_nick or ""
            best["avatar"] = actual_avatar or best.get("avatar") or ""
            return best

        # 策略 1：桌面端 API 响应（需登录 Cookie）
        items = parse_aweme_list(dcap.get("post")) if dcap.get("post") else []
        if items:
            best = max(items, key=_sort_key)
            best["_conf"] = "api"
            best["_src"] = "desktop"
            best["actual_unique_id"] = actual_uid
            best["nickname"] = best.get("nickname") or actual_nick or ""
            best["avatar"] = actual_avatar or best.get("avatar") or ""
            return best

        # 策略 2（退化）：作品数变化推测
        count = parse_aweme_count(dcap.get("profile"))
        if count is not None:
            return {
                "aweme_id": f"count:{count}",
                "desc": "（接口被风控/未登录，按作品数变化推测可能有新作品，请到主页确认）",
                "video_url": f"https://www.douyin.com/user/{sec_uid}",
                "is_note": False,
                # 即便退化到「作品数推测」，主页 profile/other 仍返回真实昵称，回填供前端展示
                "nickname": actual_nick or "",
                "avatar": actual_avatar or "",
                "create_time": count,
                "_conf": "count",
                "actual_unique_id": actual_uid,
            }
        return None
    except Exception as e:
        logger.warning("  [%s] 获取作品异常: %s", sec_uid[:12], e)
        return None
    finally:
        page.close()


# ==================== 统一日志写入（错误可见 / 新作品进统一日志） ====================

def _truncate_detail(s: str, maxlen: int = 200) -> str:
    """截断 detail 自由文本，避免单条异常堆栈撑爆 history。"""
    s = s or ""
    if len(s) > maxlen:
        s = s[:maxlen] + "…"
    return s


def append_event(rid, name, platform, etype, detail="", level=None, now=None, account=None, push=None):
    """向统一 history.json 追加一条分级事件（原子写 + 错误类节流）。

    所有 history 写入统一经 ``log_utils.append_history``（.tmp+os.replace + 上限裁剪），
    禁止散落直写。错误类（error/cookie_warn）经 ``dedupe_by_throttle`` 节流（同 rid+type
    30min 内不重复写，防刷屏）；其余（new_post/system/live_*）始终写入。

    Args:
        rid: 账号主键（与 history 的 rid 同源）。
        name: 显示名。
        platform: bilibili | douyin。
        etype: 事件 type（见 log_utils.EVENT_TYPES）；非法值降级 system。
        detail: 自由文本（错误原因/作品链接/风控提示），自动截断。
        level: 严重级；缺省由 type 推导。
        now: 当前时间（datetime 或字符串）；缺省 bjnow()。
        account: 账号唯一键（默认 == rid）。
        push: 可选推送状态（如 "pushed_fail"）；缺省 None。
    """
    if etype not in EVENT_TYPES:
        etype = "system"
    now_dt = now if now is not None else bjnow()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(now_dt, "strftime") else str(now_dt)
    if level is None:
        level = level_from_type(etype)
    entry = {
        "time": now_str,
        "name": name,
        "platform": platform,
        # 兼容旧字段：前端按 status 懒推导图标；新代码统一用 type
        "status": etype,
        "title": "",
        "changed": False,
        "prev": None,
        "push": push,
        "rid": rid,
        "type": etype,
        "level": level,
        "detail": _truncate_detail(detail),
        "account": account if account is not None else rid,
    }
    to_write = [entry]
    if etype in ("error", "cookie_warn"):
        to_write = dedupe_by_throttle(to_write, now_dt, history_path=HISTORY_FILE)
        if not to_write:
            # 节流抑制：仅保留 Python logger 控制台输出（调用方已打），不写 history 刷屏
            return
    append_history(HISTORY_FILE, to_write, HISTORY_MAX)


# ==================== 主逻辑 ====================

def _dedup_health_check(tracking: Dict[str, Dict[str, Any]]) -> None:
    """健康检查：tracking 有基线但 dedup 账本为空/缺失时告警。

    这种情况通常意味着 CI 状态持久化出了问题（git push 失败导致去重账本丢失）。
    虽然去重账本丢失不必然导致重复推送（tracking 基线仍能拦截同作品重推），
    但它是状态完整性的重要信号，值得告警。
    """
    # 有基线的账号数
    accounts_with_baseline = sum(
        1 for t in tracking.values()
        if t.get("latest_aweme_id")
    )
    if accounts_with_baseline == 0:
        return  # 首次运行，无基线，不需要告警

    # 检查 dedup 账本
    from notify_dedup import _load as dedup_load
    ledger = dedup_load()
    post_keys = [k for k in ledger if k.startswith("post:")]
    if not post_keys and accounts_with_baseline > 0:
        logger.warning(
            "⚠️ 去重账本为空但 tracking 有 %d 个账号基线 — "
            "CI 状态持久化可能异常（检查 merge_state.py / git push 是否正常）。"
            "当前 tracking 基线仍可防同作品重推，但若 tracking 也丢失则可能重复推送。",
            accounts_with_baseline,
        )


def order_rooms_baseline_first(post_rooms: List[Dict[str, Any]],
                               tracking: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把尚无基线的账号排到最前（稳定排序，其余保持原顺序）。

    为什么：新添加的账号第一次抓取只建基线、不推送。若它排在队尾，前面的账号
    把浏览器会话/token 配额/时间预算耗尽后它整轮轮空 → 「添加后等很久才抓到」。
    让没基线的账号先跑，能最快完成建基线，之后进入正常的「有基线→比对新作」循环。
    快手尤其受益：其会话 token 配额有限（MAX_USES_PER_TOKEN），队尾账号常赶不上。
    """
    def _key(e: Dict[str, Any]) -> int:
        platform = e.get("platform") or "douyin"
        rid = str(e.get("id") or "")
        t = tracking.get(f"{platform}_{rid}") or {}
        return 0 if not t.get("latest_post_id") else 1
    return sorted(post_rooms, key=_key)


#: 快手账号连续被风控 / 返回退化列达到该轮次后，主动推送一次告警（§4.4 防静默漏检）。
#: 作品检测每 15 分钟一轮，阈值 2 ≈ 连续 30 分钟退化才告警，规避单次风控预热失败的误报。
KUAISHOU_GATED_ALERT_THRESHOLD = 2


def _maybe_alert_kuaishou_gated(name: str, rid: str, streak: int,
                                 cfg_all: Dict[str, Any], entry: Dict[str, Any],
                                 now_str: str) -> None:
    """快手账号连续被风控 / 返回退化列表时，主动推送一次告警（§4.4 防静默漏检）。

    现有 ``cookie_warn`` 事件只写 history.json（节流），**不会推送**，所以用户永远
    不知道 Nizi981116 这类账号因出口地域 / 风控拿不到最新作品。本函数经现有推送通道
    发一条可执行提醒（含出口代理 / cookie 两种对策），靠 notify_dedup 6 小时冷却防刷屏；
    未配推送渠道时仅落 history（调用方已写 cookie_warn 事件）。
    """
    dkey = f"kuaishou_gated:{rid}"
    cooldown = 6 * 3600
    if not dedup_should_notify(dkey, cooldown=cooldown):
        return
    ctx = {
        "platform": "kuaishou",
        "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
        "event": "cookie_warn",
    }
    title = f"⚠️ 快手账号「{name}」持续被风控"
    desp = (
        f"## ⚠️ 快手账号「{name}」持续被风控 / 返回退化列表\n\n"
        f"已连续 **{streak}** 轮未取到完整作品列表，新作品可能**静默漏检**。\n\n"
        f"**常见原因与对策：**\n\n"
        f"- **出口地域差异**：CI（海外）比大陆少返回最新作品（Nizi981116 实证）→ "
        f"在 `BLIVE_CONFIG` 配置大陆出口代理 `BROWSER_PROXY`（GitHub Secret）。\n"
        f"- **cookie 过期 / 被风控** → 在 `BLIVE_CONFIG.platforms.kuaishou.credentials` 更新 cookie。\n\n"
        f"---\n检测时间: {now_str}"
    )
    try:
        res = dispatch_event(cfg_all, ctx, title, desp)
        if res is not None and res.ok:
            dedup_record(dkey)
        elif res is not None and res.last_error == "config: empty push_cfg":
            # 未配推送渠道：记一次冷却，避免每轮刷错误日志；配好后冷却到期会重触发
            dedup_record(dkey)
        else:
            logger.error("快手风控告警推送失败: %s",
                         (res.last_error if res else "无响应")[:200])
    except Exception as e:  # noqa: BLE001
        logger.error("快手风控告警异常: %s", e)


def handle_kuaishou_posts(entry: Dict[str, Any], tracking: Dict[str, Dict[str, Any]],
                          cfg_all: Dict[str, Any], silence_cfg: Dict[str, Any],
                          now_str: str, context: Any = None,
                          shared_adapter: Any = None):
    """快手新作检测（走 live_api/profile/public，需浏览器上下文）。

    复用 KuaishouAdapter.fetch_new_posts（传入 caller 提供的 context）：
      - context 由 main() 的 Playwright 浏览器块提供，KuaishouFeedSession 会用
        context.browser.new_context() 自建隔离 context（避免与抖音 UA/cookie 串味）；
      - fetch_new_posts 强制要求 context，context is None 会直接 raise AdapterGated
        （即此前「快手被错误地放在浏览器启动前」导致恒 gated 的根因）。
      - 首次抓取（tracking 无基线）→ 仅建基线、不推送，避免历史作品刷屏；
      - 首次抓取（tracking 无基线）→ 仅建基线、不推送，避免历史作品刷屏；
      - 基线之后 → 每个比基线新的作品写 new_post 事件 + 去重推送；
      - 接口被风控（AdapterGated）→ 写 cookie_warn 事件（与抖音风控一致），不刷屏；
      - 抓取异常 → 写 error 事件。

    封面/链接/描述/类型/昵称写回 tracking[key]，供前端作品卡与推送使用。

    Returns:
        (changed, gated): changed=是否更新了 tracking（需落盘）；gated=是否命中风控。
    """
    from backend.adapters.kuaishou import (
        KuaishouAdapter,
        apply_identity_to_config,
        apply_identity_to_tracking,
        resolve_kuaishou_identity,
    )

    rid = entry.get("id", "")
    name = entry.get("name", rid) or rid
    if not rid:
        return False, False

    key = f"kuaishou_{rid}"
    t = dict(tracking.get(key, {}))          # 拷贝，避免未提交前污染共享引用
    t["degraded_this_round"] = False        # 每轮重置；adapter 在「基线防回退」时置 True
    had_baseline = bool(t.get("latest_post_id"))

    # 任务五/六：Resolve Identity → principalId。身份解析放在读取 tracking 之后，
    # 好把上一轮已校验的 principal_id 当 hint 喂回去 —— 稳态运行时零次网络请求。
    # Fail Soft：解不出就回退用户名（开播提醒仍可用，新作大概率抓不到但不中断整轮）。
    ident = resolve_kuaishou_identity(entry, rid, tracking=t)
    pid = ident.principal_id if ident else ""
    if ident is not None:
        apply_identity_to_tracking(ident, t)
        apply_identity_to_config(ident, entry)   # 任务八：只填空位，回写 post_rooms.json
        if pid and pid != rid:
            logger.info("  [kuaishou] %s 身份解析 principalId=%s（来源=%s 校验=%s）",
                        name, pid, ident.identity_source,
                        "已交叉校验" if (ident.extra or {}).get("verified") else "未校验")
    else:
        logger.warning("  [kuaishou] %s 身份解析失败，回退用户名 %s（新作可能抓不到，"
                       "建议在 post_rooms.json 补 share_url 或 principal_id）", name, rid)

    creds = (cfg_all.get("platforms") or {}).get("kuaishou") or {}
    creds = creds.get("credentials") or {}
    if shared_adapter is not None:
        adapter = shared_adapter
    else:
        adapter = KuaishouAdapter(credentials=creds or None)

    try:
        # 用 principalId（或回退用户名）作 graphql userId
        posts = adapter.fetch_new_posts(pid or rid, baseline=t, context=context)
    except AdapterGated as g:
        append_event(rid, name, "kuaishou", "cookie_warn",
                     detail=f"快手匿名通道被风控（{g.detail}）。默认无需 cookie；"
                            f"若个别账号持续被挡，可在 BLIVE_CONFIG.platforms.kuaishou.credentials.cookie 选择性注入 cookie 增强",
                     now=bjnow())
        t["gated_streak"] = int(t.get("gated_streak") or 0) + 1
        tracking[key] = t
        if t["gated_streak"] >= KUAISHOU_GATED_ALERT_THRESHOLD:
            _maybe_alert_kuaishou_gated(name, rid, t["gated_streak"], cfg_all, entry, now_str)
        return True, True
    except Exception as e:
        logger.error("  [%s] 快手新作获取异常: %s", name, e)
        append_event(rid, name, "kuaishou", "error", detail=f"获取作品异常: {e}", now=bjnow())
        tracking[key] = t
        return True, False

    # 首次建基线要可见：新添加的账号建好基线后明确记一条「监控已生效」，
    # 否则看板上完全看不出这账号到底抓没抓到（2026-08 用户反馈「添加后
    # 等很久很久不知道好没好」）。
    if not had_baseline and t.get("latest_post_id"):
        append_event(rid, name, "kuaishou", "system",
                     detail=(f"已建立基线（最新作品 {t.get('latest_published_at') or '?'}），"
                             f"监控已生效，该账号再发布新作品时将推送通知"),
                     now=bjnow())

    # 退化检测（§4.4）：本轮被风控 / 基线防回退（地域或风控导致列表不完整）→ 可能静默漏检。
    # gated_streak 累计「连续退化」轮次，达到阈值主动推送一次告警（见 _maybe_alert_kuaishou_gated）。
    degraded = bool(t.get("degraded_this_round"))
    if degraded:
        t["gated_streak"] = int(t.get("gated_streak") or 0) + 1
    else:
        t["gated_streak"] = 0
    if degraded and t["gated_streak"] >= KUAISHOU_GATED_ALERT_THRESHOLD:
        _maybe_alert_kuaishou_gated(name, rid, t["gated_streak"], cfg_all, entry, now_str)

    if not posts:
        # 无新作（或风控返回空），基线已在 adapter 内更新（若有变化），落盘即可
        tracking[key] = t
        return True, False

    # 用最新一条作品回填封面/链接/描述/类型/昵称（供前端作品卡 + 推送占位）
    latest = max(posts, key=lambda p: _ks_ts(p.published_at) or 0)
    t["latest_post_id"] = latest.post_id
    t["latest_published_at"] = latest.published_at
    t["latest_ct"] = _ks_ts(latest.published_at) or 0
    t["latest_cover"] = latest.cover or ""
    t["latest_cover_cdn"] = latest.cover or ""
    t["latest_url"] = latest.url or f"https://v.m.chenzhongtech.com/fw/photo/{latest.post_id}"
    t["latest_desc"] = latest.title or ""
    t["latest_type"] = latest.extra.get("type") or "视频"
    if not t.get("nickname"):
        t["nickname"] = latest.author or ""
    # 展示/推送用名：若条目仅填了 id（无真实昵称），用作品作者回填
    if latest.author and (name == rid or not entry.get("name") or entry.get("name") == rid):
        name = latest.author
        entry["name"] = name

    # 首次抓取：建基线，并把「当前最新一条」推给用户，让其添加后立即看到，
    # 而非干等下一部未来新作。仅推一条最新（走 dedup），不刷屏历史作品。
    if not had_baseline:
        dkey = latest.extra.get("dedup_key") or f"post:kuaishou:{latest.post_id}"
        logger.info("  [%s] 快手首次抓取，建立基线（%d 个历史作品），推送当前最新一条", name, len(posts))
        if dedup_should_notify(dkey, cooldown=float("inf")):
            kind = latest.extra.get("type") or "视频"
            link = latest.url or f"https://v.m.chenzhongtech.com/fw/photo/{latest.post_id}"
            title = f"🆕 {name} 已开始监控（最新作品）"
            desp = (
                f"## 🆕 {name} 已开始监控\n\n"
                f"**平台**: 快手\n\n"
                f"**类型**: {kind}\n\n"
                f"**描述**: {latest.title or '[无描述]'}\n\n"
                f"👉 [查看作品]({link})\n\n"
                f"---\n检测时间: {now_str}"
            )
            ctx = {
                "platform": "kuaishou",
                "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
                "event": "new_post",
            }
            pcfg = channel_to_push_cfg(common.resolve_channel(cfg_all, ctx))
            channel = (pcfg.get("type") or "unknown").lower()
            res = dispatch_event(cfg_all, ctx, title, desp)
            logger.info("    → 首次基线作品推送%s", "成功" if res.ok else "失败")
            if res.ok:
                dedup_record(dkey)
            elif res.last_error == "config: empty push_cfg":
                pass
            else:
                last_err = (res.last_error or "未知错误")[:200]
                logger.error("首次基线作品推送失败 channel=%s: %s", channel, last_err)
        tracking[key] = t
        return True, False

    # 基线之后：逐个新作写日志 + 去重推送
    for p in posts:
        detail = f"{(p.title or '[无描述]')}  {(p.url or f'https://v.m.chenzhongtech.com/fw/photo/{p.post_id}')}".strip()
        append_event(rid, name, "kuaishou", "new_post", detail=detail, now=bjnow())
        dkey = p.extra.get("dedup_key") or f"post:kuaishou:{p.post_id}"
        if not dedup_should_notify(dkey, cooldown=float("inf")):
            logger.info("  [%s] 去重跳过：作品 %s 已推送过，不重复", name, p.post_id)
            continue
        # 推送
        kind = p.extra.get("type") or "视频"
        link = p.url or f"https://v.m.chenzhongtech.com/fw/photo/{p.post_id}"
        title = f"🆕 {name} 发布了新作品"
        desp = (
            f"## 🆕 {name} 发布了新作品\n\n"
            f"**类型**: {kind}\n\n"
            f"**描述**: {p.title or '[无描述]'}\n\n"
            f"👉 [查看作品]({link})\n\n"
            f"---\n检测时间: {now_str}"
        )
        if should_skip_by_silence(bjnow(), silence_cfg):
            logger.info("  [%s] 当前处于静默时段，暂缓新作品推送", name)
            continue
        try:
            ctx = {
                "platform": "kuaishou",
                "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
                "event": "new_post",
            }
            pcfg = channel_to_push_cfg(common.resolve_channel(cfg_all, ctx))
            channel = (pcfg.get("type") or "unknown").lower()
            res = dispatch_event(cfg_all, ctx, title, desp)
            logger.info("    → 推送%s", "成功" if res.ok else "失败")
            if res.ok:
                dedup_record(dkey)
            elif res.last_error == "config: empty push_cfg":
                pass
            else:
                last_err = (res.last_error or "未知错误")[:200]
                logger.error(
                    "通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                    channel, res.attempts, res.last_error, title,
                )
                append_event(rid, name, "kuaishou", "error",
                             detail=f"通知发送失败（{channel}）：{last_err}",
                             now=bjnow(), push="pushed_fail")
        except Exception as e:
            logger.error("    → 推送异常: %s", e)

    tracking[key] = t
    return True, False


def _ks_ts(s: Any) -> Optional[int]:
    """把北京时间字符串转 epoch 秒（快手 PostModel.published_at 格式）。"""
    try:
        from datetime import datetime as _dt
        return int(_dt.strptime(str(s), "%Y-%m-%d %H:%M:%S").timestamp())
    except Exception:
        try:
            return int(s)
        except (TypeError, ValueError):
            return 0


def main() -> None:
    """主函数"""
    if os.environ.get("ENABLE_POST_CHECK", "").lower() != "true":
        logger.info("新作品检测已禁用 (设置 ENABLE_POST_CHECK=true 启用)")
        return

    # 加载配置（推送渠道）：优先 BLIVE_CONFIG 环境变量，兼容旧 sendkey 写法
    raw_config = os.environ.get("BLIVE_CONFIG", "{}")
    push_cfg = load_push_cfg(raw_config)
    # 完整配置（参与多通道路由）：供 dispatch_event 消费（A2）；legacy 单通道下等价于仅含 push 段
    cfg_all = json.loads(raw_config) if raw_config else {}
    # A3 静默时段：从 BLIVE_CONFIG.silence 解析（无则 {}，不静默）
    silence_cfg = load_silence_cfg(raw_config)

    # 加载作品监控专属的抖音号列表
    post_rooms: List[Dict[str, str]] = load_json_file(CONFIG_FILE, [])
    if not post_rooms:
        logger.info("post_rooms.json 为空，没有需要监控新作品的抖音号")
        return

    # 加载作品监控状态
    tracking: Dict[str, Dict[str, Any]] = load_json_file(TRACKING_FILE, {})
    now_str = bjnow().strftime("%Y-%m-%d %H:%M:%S")
    changed = False
    gated_hint = False  # 跨平台汇总：抖音/快手任一被风控则末尾提示

    # 健康检查：tracking 有基线但 dedup 账本为空/缺失 → 可能状态丢失，
    # 提示运维检查 CI 持久化（merge_state.py 是否正常工作）
    _dedup_health_check(tracking)

    # 推送前从远端同步去重账本：并发 run 时避免重复推送同一作品（与 check_status.py 同源修复）
    synced = dedup_sync_from_remote()
    if synced:
        logger.info("去重账本已从远端同步 %d 条较新记录", synced)

    logger.info("开始检测 %d 个抖音/快手用户的新作品...", len(post_rooms))

    # 尚无基线的账号排到最前：新添加的账号优先拿到本轮最稳定的浏览器会话与
    # token 配额。排在队尾的新账号容易在前面的账号把时间/配额耗尽后整轮轮空，
    # 表现为「添加后等很久很久才建基线」（2026-08 用户反馈，快手尤甚）。
    # sorted 是稳定排序：同类账号保持原有相对顺序。
    post_rooms = order_rooms_baseline_first(post_rooms, tracking)

    from backend.adapters._browser import sync_playwright

    cookie = load_douyin_cookie()
    # 可选出口代理（BROWSER_PROXY / BLIVE_CONFIG.browser_proxy）：快手按访问地域
    # 返回不同作品列表，海外 runner 会漏最新作品，走大陆出口代理可拿全（2026-08 实测）
    _proxy = load_browser_proxy()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
            **({"proxy": _proxy} if _proxy else {}),
        )
        context = browser.new_context(
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        # 关键：注入登录 Cookie 可突破作品接口风控（可选，未配置则优雅降级）
        apply_douyin_cookie(context, cookie)

        # 快手所有账号共享一个 KuaishouAdapter / KuaishouFeedSession：
        # 一次预热（visitor JS 种 did+风控 token）后全轮复用，避免每账号各开
        # context、各打一次 www.kuaishou.com 预热——后者从同一出口 IP 短时间内
        # 连发 5 次预热 + 70 次 profile 导航，极易触发快手 IP 级风控。
        from backend.adapters.kuaishou import KuaishouAdapter
        _ks_creds = (cfg_all.get("platforms") or {}).get("kuaishou") or {}
        _ks_creds = _ks_creds.get("credentials") or {}
        ks_shared_adapter = KuaishouAdapter(credentials=_ks_creds or None)

        post_rooms_dirty = False
        _ks_account_count = 0  # 快手账号计数器（账号间延迟用）
        for entry in post_rooms:
            rid = entry.get("id", "")
            name = entry.get("name", rid) or rid
            platform = entry.get("platform", "douyin")
            # B3 批量启停：enabled===false 的账号完全跳过检测（看板保留上次状态）
            if not room_enabled(entry):
                logger.info("  [%s] 已暂停（enabled=false），跳过检测", name)
                continue
            if not rid:
                # 条目缺 id（配置不完整）→ 写 system 跳过，不刷屏（不写垃圾 error）
                logger.warning("  post_rooms.json 中存在缺 id 的条目，已跳过")
                append_event("", "(缺id)", platform, "system",
                             detail="账号配置不完整（缺 id），已跳过", now=bjnow())
                continue
            if platform == "kuaishou":
                # 快手冷却控制（KUAISHOU_COOLDOWN_UNTIL 环境变量，ISO 时间戳）
                import os as _os
                from datetime import datetime as _dt, timezone as _tz
                _cooldown = _os.environ.get("KUAISHOU_COOLDOWN_UNTIL", "").strip()
                if _cooldown:
                    try:
                        _until = _dt.fromisoformat(_cooldown.replace("Z", "+00:00"))
                        if _dt.now(_tz.utc) < _until:
                            logger.info("  [%s] 快手冷却中（至 %s），跳过作品检查", name, _cooldown)
                            continue
                    except Exception:
                        pass
                # 快手走 live_api/profile/public（需浏览器上下文）：复用本浏览器，
                # KuaishouFeedSession 会用 context.browser.new_context() 自建隔离 context，
                # 避免与抖音 UA/cookie 串味。此前错误地放在浏览器启动前、且不传 context，
                # 导致 fetch_new_posts 因 context is None 恒 AdapterGated。
                if _ks_account_count > 0:
                    logger.info("  [kuaishou] 账号间冷却 %d 秒（降低 IP 频控风险）...", 12)
                    time.sleep(12)
                _ks_account_count += 1
                try:
                    _chg, _gated = handle_kuaishou_posts(
                        entry, tracking, cfg_all, silence_cfg, now_str, context=context,
                        shared_adapter=ks_shared_adapter)
                except Exception as exc:
                    logger.error("  [%s] 快手新作检测异常: %s", name, exc)
                    _chg, _gated = True, False
                changed = changed or _chg
                gated_hint = gated_hint or _gated
                continue
            key = f"douyin_{rid}"
            t = tracking.get(key, {})

            # 解析 sec_uid：
            #  - 优先用已存值（tracking）或 post_rooms.json 直存值 → 视为「可信」，不再反查；
            #  - 否则从直播页解析（运行时解析，视为「不可信」，需 unique_id 反查校验）。
            stored_sec = t.get("sec_uid") or entry.get("sec_uid")
            if stored_sec:
                sec_uid = stored_sec
                sec_trusted = True
            else:
                sec_uid = resolve_sec_uid(context, rid)
                sec_trusted = False
            if not sec_uid:
                # 缺 sec_uid 且无法解析 → 降级 type=system 跳过刷屏（不写垃圾 error）
                logger.warning("  [%s] 无法获取 sec_uid，跳过（建议开播时或配置 DOUYIN_COOKIE 后重试）", name)
                append_event(rid, name, "douyin", "system",
                             detail="账号配置不完整（缺 sec_uid），已跳过", now=bjnow())
                continue
            t["sec_uid"] = sec_uid
            tracking[key] = t
            changed = True

            # 获取最新作品（三层策略：m.douyin.com 免 Cookie 首选 / 桌面端需 Cookie / count 退化）
            try:
                aweme = get_latest_aweme(context, sec_uid)
            except Exception as exc:  # 抓取失败（意外异常）→ 写 error（节流），跳过该账号
                logger.error("  [%s] 获取作品异常: %s", name, exc)
                append_event(rid, name, "douyin", "error",
                             detail=f"获取作品异常: {exc}", now=bjnow())
                gated_hint = True
                continue
            if not aweme:
                # 接口被风控/未登录（拿不到真实作品列表）→ 写 cookie_warn（节流），跳过
                logger.warning("  [%s] 获取作品失败/被风控（建议配置 douyin_cookie）", name)
                append_event(rid, name, "douyin", "cookie_warn",
                             detail="抖音接口被风控，配置 douyin_cookie 可获取具体作品",
                             now=bjnow())
                gated_hint = True
                continue

            # 中毒防护：用已捕获 profile 的 unique_id 校验 sec_uid 是否真对应本 handle。
            #  - 仅当 rid 形如 handle（非纯数字、非 sec_uid）时才做反查，避免误杀数字号账号；
            #  - 可信（已存）sec_uid 即便反查不一致也保留并告警，绝不清除用户/历史沉淀的值；
            #  - 不可信（运行时解析）的若反查不一致，说明被推荐流污染，跳过并清除毒值，下次重解。
            actual_uid = aweme.get("actual_unique_id")
            if actual_uid and looks_like_handle(rid) and actual_uid != rid:
                if sec_trusted:
                    logger.warning(
                        "  [%s] ⚠️ 已存 sec_uid 指向账号(实际=%s)与填写 id(%s)不一致，"
                        "仍信任已存值继续监控", name, actual_uid, rid,
                    )
                else:
                    logger.warning(
                        "  [%s] ⚠️ 解析的 sec_uid 指向了错误账号(实际=%s≠%s)，"
                        "疑似被推荐流污染，本次跳过并清除该 sec_uid", name, actual_uid, rid,
                    )
                    t.pop("sec_uid", None)
                    tracking[key] = t
                    changed = True
                    continue

            # 写回：若本次 sec_uid 来自运行时解析（post_rooms.json 原本无），将其固化进
            # post_rooms.json，使该账号等价于「预存 sec_uid」的账号——此后即使直播页短暂
            # 取不到也不受影响。这正是让「前端网页添加的账号」与「预存 sec_uid 的账号」行为一致的关窍。
            if not entry.get("sec_uid") and sec_uid:
                entry["sec_uid"] = sec_uid
                post_rooms_dirty = True

            # 展示/推送用名：前端添加常只填了 id（handle/数字号），此处用主页真实昵称回填，
            # 这样推送标题与前端卡片都显示「峰哥亡命天涯」而非裸 id。
            # 同时把昵称写回 post_rooms.json 的 name 字段——否则前端在 tracking 未加载时
            # 仍显示裸 id。这正是「添加后无需等待 CI 即有真实昵称」的关键。
            if aweme.get("nickname") and (name == rid or not entry.get("name") or entry.get("name") == rid):
                name = aweme["nickname"]
                entry["name"] = name
                post_rooms_dirty = True

            conf = aweme.get("_conf", "api")
            desc = aweme.get("desc", "") or "[无描述]"
            kind = "图文" if aweme.get("is_note") else "视频"
            prev_id = t.get("latest_aweme_id", "")
            prev_ct = int(t.get("latest_ct", 0) or 0)
            new_ct = int(aweme.get("create_time", 0) or 0)
            logger.info(
                "  [%s] 取到最新作品[%s]: %s (上次基线: %s)",
                name, conf, aweme["aweme_id"], prev_id or "无",
            )

            prev_mode = t.get("mode") or (
                "count" if (prev_id or "").startswith("count:") else ("api" if prev_id else "")
            )
            cur_mode = conf  # "api" 或 "count"

            notify = False
            do_update = True
            dedup_key = None  # 推送成功后需要记录的去重键

            if conf == "api":
                # 精确：确有比基线更新的作品才推送；接口延迟返回更旧作品则保留基线
                candidate = should_notify_new_post(prev_id, prev_ct, aweme["aweme_id"], new_ct)
                do_update = should_update_baseline(prev_id, prev_ct, aweme["aweme_id"], new_ct)
                # 新作品事件写入统一日志（与推送去重解耦：检测到即写，无论是否推送成功）
                if candidate:
                    append_event(
                        rid, name, "douyin", "new_post",
                        detail=f"{desc}  {aweme.get('video_url', '')}".strip(),
                        now=bjnow(),
                    )
                post_dkey = f"post:{sec_uid}:{aweme['aweme_id']}"
                if candidate and dedup_should_notify(post_dkey, cooldown=float("inf")):
                    notify = True
                    dedup_key = post_dkey
                elif candidate:
                    logger.info("  [%s] 去重跳过：作品 %s 已推送过，不重复", name, aweme["aweme_id"])
                # 首次监控（无基线）：把当前最新作品推一条，让用户添加后立即看到，
                # 而非干等下一部未来新作。仅推「一条最新」且走 dedup，不会刷屏历史作品。
                if not prev_id and dedup_should_notify(post_dkey, cooldown=float("inf")):
                    notify = True
                    dedup_key = post_dkey
                    append_event(
                        rid, name, "douyin", "new_post",
                        detail="首次监控基线作品：" + f"{desc}  {aweme.get('video_url', '')}".strip(),
                        now=bjnow(),
                    )
                # 真实封面回填：api 模式拿到真实作品即写入（即使基线未变也刷新，
                # 让所有已监控账号在下次 CI 尽快显示真实封面，而非一直占位）
                if aweme.get("cover"):
                    t["latest_cover"] = aweme["cover"]
                    # 单独持久化 CDN 源 URL：latest_cover 会被 transcode 改写为仓库 raw URL，
                    # 若不另存 CDN 源，新作品封面“晚到”时将无源可重新下载（见 于冬来 案例）。
                    t["latest_cover_cdn"] = aweme["cover"]
            else:  # conf == "count"：推测，仅当作品数确实增加且已有基线才提示
                if prev_mode and prev_mode != cur_mode:
                    # 模式切换（如从无 Cookie 计数推测切到有 Cookie 真实接口，或反之）：
                    # 无法确定其间是否真有新作品，仅静默重建基线，避免误报
                    notify = False
                    do_update = True
                else:
                    prev_count = int(t.get("latest_count", 0) or 0)
                    candidate = bool(prev_count) and new_ct > prev_count
                    count_dkey = f"post:{sec_uid}:count:{new_ct}"
                    # 新作品事件写入统一日志（与推送去重解耦：检测到即写）
                    if candidate:
                        append_event(
                            rid, name, "douyin", "new_post",
                            detail=f"作品数 {prev_count}→{new_ct}",
                            now=bjnow(),
                        )
                    if candidate and dedup_should_notify(count_dkey, cooldown=float("inf")):
                        notify = True
                        dedup_key = count_dkey
                    elif candidate:
                        logger.info("  [%s] 去重跳过：作品数 %d 已推送过", name, new_ct)
                    do_update = True

            if notify:
                if conf == "api":
                    logger.info("  [%s] 🆕 新作品(%s): %s", name, kind, desc[:40])
                    title = f"🆕 {name} 发布了新作品"
                    desp = (
                        f"## 🆕 {name} 发布了新作品\n\n"
                        f"**类型**: {kind}\n\n"
                        f"**描述**: {desc}\n\n"
                        f"👉 [查看作品]({aweme['video_url']})\n\n"
                        f"---\n检测时间: {now_str}"
                    )
                else:
                    prev_count = int(t.get("latest_count", 0) or 0)
                    logger.info("  [%s] 🔔 作品数 %d→%d，推测可能有新作品", name, prev_count, new_ct)
                    title = f"🔔 {name} 可能发布了新作品"
                    desp = (
                        f"## 🔔 {name} 可能发布了新作品\n\n"
                        f"**作品数变化**: {prev_count} → {new_ct}\n\n"
                        f"接口被风控/未登录，无法获取具体作品，请到主页确认：\n"
                        f"👉 [打开 {name} 的主页]({aweme['video_url']})\n\n"
                        f"---\n检测时间: {now_str}"
                    )
                if should_skip_by_silence(bjnow(), silence_cfg):
                    # A3 静默时段：仅跳过推送，作品基线已正常记录
                    logger.info("  [%s] 当前处于静默时段，暂缓新作品推送", name)
                else:
                    try:
                        # 统一路由 + 发送（每新作品独立路由到其通道，不跨房间聚合）
                        ctx = {
                            "platform": "douyin",
                            "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
                            "event": "new_post",
                        }
                        pcfg = channel_to_push_cfg(common.resolve_channel(cfg_all, ctx))
                        channel = (pcfg.get("type") or "unknown").lower()
                        res = dispatch_event(cfg_all, ctx, title, desp)
                        logger.info("    → 推送%s", "成功" if res.ok else "失败")
                        if res.ok:
                            # 仅推送成功后才记录去重（失败不标记，下一轮可补推）
                            if dedup_key:
                                dedup_record(dedup_key)
                        elif res.last_error == "config: empty push_cfg":
                            # 未配置通道：等价 legacy 无 push 分支，静默跳过（不刷 error）
                            pass
                        else:
                            # 失败：写 error 级统一日志事件（含渠道+原因），下一个 CI 周期可补推。
                            # runtime.log 经 logger.error 始终落盘（不受 30min 节流）；
                            # append_event 内 error 类事件会经 dedupe_by_throttle 落 history.json（受节流防刷屏）。
                            last_err = (res.last_error or "未知错误")[:200]
                            logger.error(
                                "通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                                channel, res.attempts, res.last_error, title,
                            )
                            append_event(
                                rid, name, "douyin", "error",
                                detail=f"通知发送失败（{channel}）：{last_err}",
                                now=bjnow(),
                                push="pushed_fail",
                            )
                    except Exception as e:
                        logger.error("    → 推送异常: %s", e)

            if do_update:
                t["latest_aweme_id"] = aweme["aweme_id"]
                t["latest_ct"] = new_ct
                t["mode"] = conf
                t["latest_count"] = new_ct
                # 真实昵称在所有模式都回填（前端展示/推送都用它，避免只显示裸 id）
                t["nickname"] = aweme.get("nickname") or t.get("nickname", "")
                # 头像 URL 回填（前端横条视图显示真实头像，替代首字母圆）
                if aweme.get("avatar"):
                    t["avatar"] = aweme["avatar"]
                # need_cookie 标记：账号稳定走 count 退化（接口被风控/未登录，拿不到真实
                # 作品列表）时置 True，引导用户到 BLIVE_CONFIG 配置 douyin_cookie 突破风控。
                # api 模式下拿到真实作品则清除该标记。
                if conf == "count":
                    t["need_cookie"] = True
                else:
                    t.pop("need_cookie", None)
                if conf == "api":
                    t["latest_desc"] = aweme.get("desc", "")
                    t["latest_type"] = kind
                    t["latest_url"] = aweme.get("video_url", "")
            else:
                logger.info("  [%s] 接口返回作品较旧，保留已有基线（抖音接口延迟）", name)
            tracking[key] = t
            changed = True

        # 关闭快手共享 session（其自建的隔离 context）
        try:
            ks_shared_adapter.close()
        except Exception:
            pass
        context.close()
        browser.close()

    if gated_hint:
        logger.warning(
            "部分账号作品接口被风控/未登录，新作品可能漏检。"
            "请在 BLIVE_CONFIG 增加 douyin_cookie（浏览器登录抖音后的 Cookie），"
            "或设置环境变量 DOUYIN_COOKIE 以突破风控。"
        )

    # ===== 固化阶段：级联清理 + 字段合并（替代原内联应急补丁，统一收口到 state_prune）=====
    # 重读磁盘当前 post_rooms.json（不依赖启动内存副本），避免与前端增删竞态：
    # 若用启动时的内存副本整体覆盖写回，会把用户在「本轮期间」删除的账号又加回来、
    # 或丢失用户新增的账号；再经 merge_state.py 的并集合并后，被删账号会复活。
    current_rooms = load_json_file(CONFIG_FILE, []) or []
    # 本轮解析/写回过的账号（仅取确有 sec_uid 的）
    # 本轮解析/写回过的账号：有 sec_uid 的（抖音）或平台为 kuaishou 的（无 sec_uid，靠 name 写回）
    resolved = {
        str(e.get("id", "")): e
        for e in post_rooms
        if e.get("id") and (e.get("sec_uid") or e.get("platform") == "kuaishou")
    }

    # 字段合并：仅对仍存在的账号原地更新 sec_uid/name（不复活已删账号）
    rooms_changed = state_prune.merge_post_rooms_fields(CONFIG_FILE, resolved)

    # 孤儿清理：基于「当前磁盘」账号集合（含平台前缀），删除 post_tracking 中已移除账号的状态
    cur_keys = {
        f"{(e.get('platform') or 'douyin')}_{e.get('id')}"
        for e in current_rooms
        if e.get("id")
    }
    tracking_before = len(tracking)
    tracking = state_prune.prune_tracking_orphans(tracking, cur_keys)

    if rooms_changed:
        logger.info("已将解析到的 sec_uid 合并写回 post_rooms.json（已保留前端增删，不复活已删账号）")

    if changed or len(tracking) != tracking_before:
        save_json_file(TRACKING_FILE, tracking)

    # 清理去重账本中的过期 live: key（post: key 永久保留）。
    # check_status.py 也会调 prune，但若本脚本单独运行时确保账本不无限增长。
    dedup_prune()

    logger.info("新作品检测完成")


def run_post_check(*, cfg_all: Dict[str, Any], persist: Any, now: Optional[Any] = None,
                   context: Any = None, adapters: Any = None) -> None:
    """后端驱动的新作品检测编排（不写 JSON，全部经 ``persist`` 落库）。

    复用本模块纯函数（get_latest_aweme / should_notify_new_post / should_update_baseline /
    resolve_sec_uid / 解析辅助）与 common / push_utils 的路由/模板/推送逻辑（一字不改）；
    「写 post_tracking.json / history.json / notify_dedup.json」改为调用 ``persist`` 回调。

    ``persist`` 协议（见 backend/jobs/post_check.PostPersist）：
        persist.list_rooms()                          -> [{platform,external_id,name,enabled,tags,meta}]
        persist.get_tracking(platform, rid)           -> dict（meta 基线）
        persist.set_room_status(platform, rid, kind='post', meta_update=...) -> 写 Room.meta
        persist.append_event(entry)                   -> 写 events_history
        persist.dedup_should_notify(key, cooldown)    -> bool
        persist.dedup_record(key)                     -> 标记去重（仅推送成功后）
        persist.notify_log(channel_id, event_type, content_hash, status)

    Args:
        cfg_all: BLIVE_CONFIG 完整 dict。
        persist: 后端持久化门面。
        now: 当前北京时间（测试用）；缺省 ``bjnow()``。
        context: Playwright BrowserContext（可选）；为 None 时本函数内部创建
            （需 playwright + 已安装 Chromium）。仅供后端 scheduler 注入复用。
    """
    if now is None:
        now = bjnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    silence_cfg = load_silence_cfg(json.dumps(cfg_all) if cfg_all else "{}")

    post_rooms = persist.list_rooms() or []
    if not post_rooms:
        logger.info("[run_post_check] post_rooms 为空，没有需要监控新作品的账号")
        return

    # 适配器注册表（阶段三 T03）：缺省从 cfg_all['platforms'] + 内置 bilibili/douyin 构建。
    if adapters is None:
        from backend.adapters import AdapterRegistry

        adapters = AdapterRegistry.from_config(cfg_all or {})
    from backend.adapters.base import AdapterGated, AdapterSkip

    def _content_hash(channel_id, event_type, content):
        return content_hash(channel_id, event_type, content)

    def _append(rid, name, platform, etype, detail="", level=None, push=None):
        persist.append_event({
            "time": bjnow().strftime("%Y-%m-%d %H:%M:%S"),
            "name": name, "platform": platform, "status": etype, "title": "",
            "changed": False, "prev": None, "push": push,
            "rid": rid, "type": etype,
            "level": level if level is not None else level_from_type(etype),
            "detail": (detail or "")[:200], "account": rid,
        })

    def _process_post(post, platform, rid, name, entry):
        """通用新作推送：逐条判定去重 -> 渲染标题/正文 -> 路由推送 -> 写事件/账本。

        复用 common / push_utils 的路由与推送（一字不改）；适配器已按基线过滤出
        「新于基线」的作品，此处仅做跨轮去重 + 推送 + 落库。
        """
        conf = post.extra.get("conf", "api")
        kind = post.extra.get("type", "视频")
        dkey = post.extra.get("dedup_key") or f"post:{platform}:{post.post_id}"
        if conf == "count":
            title = f"🔔 {name} 可能发布了新作品"
            prev_count = post.extra.get("prev_count")
            new_count = post.extra.get("new_count")
            desp = (
                f"## 🔔 {name} 可能发布了新作品\n\n**作品数变化**: {prev_count} → {new_count}\n\n"
                f"接口被风控/未登录，无法获取具体作品，请到主页确认：\n"
                f"👉 [打开 {name} 的主页]({post.url})\n\n---\n检测时间: {now_str}"
            )
            detail = f"作品数 {prev_count}→{new_count}"
        else:
            title = f"🆕 {name} 发布了新作品"
            desc = post.title or "[无描述]"
            desp = (
                f"## 🆕 {name} 发布了新作品\n\n**类型**: {kind}\n\n"
                f"**描述**: {desc}\n\n👉 [查看作品]({post.url})\n\n---\n检测时间: {now_str}"
            )
            detail = f"{desc}  {post.url}".strip()
        # 去重：适配器已按基线过滤「新作品」，此处防跨轮重复推送
        if not persist.dedup_should_notify(dkey, cooldown=float("inf")):
            logger.info("  [%s] 去重跳过：作品 %s 已推送过", name, post.post_id)
            return
        if should_skip_by_silence(bjnow(), silence_cfg):
            logger.info("  [%s] 当前处于静默时段，暂缓新作品推送", name)
            return
        try:
            ctx = {
                "platform": platform,
                "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
                "event": "new_post",
            }
            pcfg = channel_to_push_cfg(common.resolve_channel(cfg_all or {}, ctx))
            channel = (pcfg.get("type") or "unknown").lower()
            res = dispatch_event(cfg_all or {}, ctx, title, desp)
            logger.info("    → 推送%s", "成功" if res.ok else "失败")
            content_hash = _content_hash(channel, "new_post", title + desp)
            if res.ok:
                _append(rid, name, platform, "new_post", detail=detail)
                persist.dedup_record(dkey)
                persist.notify_log(channel_id=channel, event_type="new_post",
                                   content_hash=content_hash, status="ok")
            elif res.last_error == "config: empty push_cfg":
                pass
            else:
                last_err = (res.last_error or "未知错误")[:200]
                logger.error("通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                             channel, res.attempts, res.last_error, title)
                _append(rid, name, platform, "error",
                        detail=f"通知发送失败（{channel}）：{last_err}", push="pushed_fail")
                persist.notify_log(channel_id=channel, event_type="new_post",
                                   content_hash=content_hash, status="fail")
        except Exception as e:  # noqa: BLE001
            logger.error("    → 推送异常: %s", e)

    # 浏览器上下文（按需创建；无头、与 main() 同启动参数）
    own_context = False
    if context is None:
        try:
            from backend.adapters._browser import sync_playwright
            pw = sync_playwright().__enter__()
            _proxy = load_browser_proxy()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled", "--disable-gpu"],
                **({"proxy": _proxy} if _proxy else {}),
            )
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 900}, locale="zh-CN",
            )
            cookie = load_douyin_cookie()
            apply_douyin_cookie(context, cookie)
            own_context = True
        except Exception as e:
            logger.error("[run_post_check] 无法创建浏览器上下文，新作品检测跳过: %s", e)
            return

    try:
        gated_hint = False
        for entry in post_rooms:
            rid = entry.get("external_id", "") or entry.get("id", "")
            name = entry.get("name", rid)
            platform = entry.get("platform", "douyin")
            if not room_enabled(entry):
                logger.info("  [%s] 已暂停（enabled=false），跳过检测", name)
                continue
            if not rid:
                logger.warning("  post_rooms 中存在缺 id 的条目，已跳过")
                _append("", "(缺id)", "douyin", "system",
                        detail="账号配置不完整（缺 id），已跳过")
                continue
            adapter = adapters.get(platform) if adapters else None
            if platform != "douyin":
                # 阶段三 T03：非抖音平台经适配器取新作（注册表驱动，能力标志跳过不支持者）
                if adapter is None or not getattr(adapter, "supports_posts", True):
                    logger.info("  [%s] 平台(%s)不支持新作检测，跳过", name, platform)
                    _append(rid, name, platform, "system", detail="平台不支持新作检测，已跳过")
                    continue
                key = f"{platform}_{rid}"
                meta = dict(persist.get_tracking(platform, rid) or {})
                # 快手：先 Resolve Identity 拿 principalId（graphql 真正需要的 userId）再取新作。
                # meta 里带着上一轮已校验的身份，喂回 resolver 可跳过全部网络请求。
                posts_rid = rid
                if platform == "kuaishou":
                    from backend.adapters.kuaishou import (
                        apply_identity_to_tracking,
                        resolve_kuaishou_identity,
                    )
                    _ident = resolve_kuaishou_identity(entry, rid, tracking=meta)
                    if _ident is not None:
                        apply_identity_to_tracking(_ident, meta)
                        if _ident.principal_id and _ident.principal_id != rid:
                            logger.info("  [kuaishou] %s 身份解析 principalId=%s（来源=%s）",
                                        name, _ident.principal_id, _ident.identity_source)
                            posts_rid = _ident.principal_id
                    else:
                        logger.warning("  [kuaishou] %s 身份解析失败，回退用户名 %s", name, rid)
                # 需要浏览器的平台：注入凭证（context 由 scheduler 或本函数创建）
                ctx = context
                if getattr(adapter, "needs_context", False) and ctx is not None:
                    try:
                        adapter.apply_credentials(ctx)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[%s] 注入凭证失败: %s", platform, e)
                try:
                    posts = adapter.fetch_new_posts(posts_rid, since=None, baseline=meta, context=ctx)
                except AdapterSkip as e:
                    logger.info("  [%s] 跳过新作检测: %s", name, e.detail or e.reason)
                    _append(rid, name, platform, "system", detail=e.detail or f"跳过: {e.reason}")
                    persist.set_room_status(platform=platform, external_id=rid, kind="post",
                                            name=name, meta_update=meta)
                    continue
                except AdapterGated as e:
                    logger.warning("  [%s] 新作接口被风控/需凭证: %s", name, e.detail)
                    _append(rid, name, platform, "cookie_warn",
                            detail=e.detail or "接口被风控，需凭证")
                    gated_hint = True
                    persist.set_room_status(platform=platform, external_id=rid, kind="post",
                                            name=name, meta_update=meta)
                    continue
                except Exception as e:  # noqa: BLE001
                    logger.error("  [%s] 获取新作异常: %s", name, e)
                    _append(rid, name, platform, "error", detail=f"获取新作异常: {e}")
                    gated_hint = True
                    persist.set_room_status(platform=platform, external_id=rid, kind="post",
                                            name=name, meta_update=meta)
                    continue
                # 通用「新作」处理：逐条推送 + 事件 + 去重 + 落库
                for post in (posts or []):
                    _process_post(post, platform, rid, name, entry)
                persist.set_room_status(platform=platform, external_id=rid, kind="post",
                                        name=name, meta_update=meta)
                for post in (posts or []):
                    persist.upsert_post({
                        "platform": post.platform,
                        "post_id": post.post_id,
                        "author": post.author,
                        "url": post.url,
                        "cover": post.cover,
                        "published_at": post.published_at,
                    })
                continue
            # 抖音：沿用既有内联检测逻辑（零重写，复用 resolve_sec_uid / get_latest_aweme 等）
            key = f"douyin_{rid}"
            t = dict(persist.get_tracking("douyin", rid) or {})

            stored_sec = t.get("sec_uid") or entry.get("sec_uid")
            if stored_sec:
                sec_uid = stored_sec
                sec_trusted = True
            else:
                sec_uid = resolve_sec_uid(context, rid)
                sec_trusted = False
            if not sec_uid:
                logger.warning("  [%s] 无法获取 sec_uid，跳过", name)
                _append(rid, name, "douyin", "system",
                        detail="账号配置不完整（缺 sec_uid），已跳过")
                continue
            t["sec_uid"] = sec_uid

            try:
                aweme = get_latest_aweme(context, sec_uid)
            except Exception as exc:
                logger.error("  [%s] 获取作品异常: %s", name, exc)
                _append(rid, name, "douyin", "error", detail=f"获取作品异常: {exc}")
                gated_hint = True
                persist.set_room_status(platform="douyin", external_id=rid, kind="post",
                                       name=name, meta_update=t)
                continue
            if not aweme:
                logger.warning("  [%s] 获取作品失败/被风控（建议配置 douyin_cookie）", name)
                _append(rid, name, "douyin", "cookie_warn",
                        detail="抖音接口被风控，配置 douyin_cookie 可获取具体作品")
                gated_hint = True
                persist.set_room_status(platform="douyin", external_id=rid, kind="post",
                                       name=name, meta_update=t)
                continue

            actual_uid = aweme.get("actual_unique_id")
            if actual_uid and looks_like_handle(rid) and actual_uid != rid:
                if sec_trusted:
                    logger.warning("  [%s] ⚠️ 已存 sec_uid 指向账号(实际=%s)与填写 id(%s)不一致，"
                                   "仍信任已存值继续监控", name, actual_uid, rid)
                else:
                    logger.warning("  [%s] ⚠️ 解析的 sec_uid 指向了错误账号(实际=%s≠%s)，"
                                   "疑似被推荐流污染，本次跳过并清除该 sec_uid", name, actual_uid, rid)
                    t.pop("sec_uid", None)
                    persist.set_room_status(platform="douyin", external_id=rid, kind="post",
                                           name=name, meta_update=t)
                    continue

            if aweme.get("nickname") and (name == rid or not entry.get("name") or entry.get("name") == rid):
                name = aweme["nickname"]
                entry["name"] = name

            conf = aweme.get("_conf", "api")
            desc = aweme.get("desc", "") or "[无描述]"
            kind = "图文" if aweme.get("is_note") else "视频"
            prev_id = t.get("latest_aweme_id", "")
            prev_ct = int(t.get("latest_ct", 0) or 0)
            new_ct = int(aweme.get("create_time", 0) or 0)
            logger.info("  [%s] 取到最新作品[%s]: %s (上次基线: %s)", name, conf,
                        aweme["aweme_id"], prev_id or "无")

            prev_mode = t.get("mode") or (
                "count" if (prev_id or "").startswith("count:") else ("api" if prev_id else "")
            )
            cur_mode = conf

            notify = False
            do_update = True
            dedup_key = None

            if conf == "api":
                candidate = should_notify_new_post(prev_id, prev_ct, aweme["aweme_id"], new_ct)
                do_update = should_update_baseline(prev_id, prev_ct, aweme["aweme_id"], new_ct)
                if candidate:
                    _append(rid, name, "douyin", "new_post",
                            detail=f"{desc}  {aweme.get('video_url', '')}".strip())
                post_dkey = f"post:{sec_uid}:{aweme['aweme_id']}"
                if candidate and persist.dedup_should_notify(post_dkey, cooldown=float("inf")):
                    notify = True
                    dedup_key = post_dkey
                elif candidate:
                    logger.info("  [%s] 去重跳过：作品 %s 已推送过，不重复", name, aweme["aweme_id"])
                if aweme.get("cover"):
                    t["latest_cover"] = aweme["cover"]
                    # 单独持久化 CDN 源 URL：latest_cover 会被 transcode 改写为仓库 raw URL，
                    # 若不另存 CDN 源，新作品封面“晚到”时将无源可重新下载（见 于冬来 案例）。
                    t["latest_cover_cdn"] = aweme["cover"]
            else:
                if prev_mode and prev_mode != cur_mode:
                    notify = False
                    do_update = True
                else:
                    prev_count = int(t.get("latest_count", 0) or 0)
                    candidate = bool(prev_count) and new_ct > prev_count
                    count_dkey = f"post:{sec_uid}:count:{new_ct}"
                    if candidate:
                        _append(rid, name, "douyin", "new_post",
                                detail=f"作品数 {prev_count}→{new_ct}")
                    if candidate and persist.dedup_should_notify(count_dkey, cooldown=float("inf")):
                        notify = True
                        dedup_key = count_dkey
                    elif candidate:
                        logger.info("  [%s] 去重跳过：作品数 %d 已推送过", name, new_ct)
                    do_update = True

            if notify:
                if conf == "api":
                    logger.info("  [%s] 🆕 新作品(%s): %s", name, kind, desc[:40])
                    title = f"🆕 {name} 发布了新作品"
                    desp = (f"## 🆕 {name} 发布了新作品\n\n**类型**: {kind}\n\n"
                            f"**描述**: {desc}\n\n👉 [查看作品]({aweme['video_url']})\n\n"
                            f"---\n检测时间: {now_str}")
                else:
                    prev_count = int(t.get("latest_count", 0) or 0)
                    logger.info("  [%s] 🔔 作品数 %d→%d，推测可能有新作品", name, prev_count, new_ct)
                    title = f"🔔 {name} 可能发布了新作品"
                    desp = (f"## 🔔 {name} 可能发布了新作品\n\n**作品数变化**: {prev_count} → {new_ct}\n\n"
                            f"接口被风控/未登录，无法获取具体作品，请到主页确认：\n"
                            f"👉 [打开 {name} 的主页]({aweme['video_url']})\n\n---\n检测时间: {now_str}")
                if should_skip_by_silence(bjnow(), silence_cfg):
                    logger.info("  [%s] 当前处于静默时段，暂缓新作品推送", name)
                else:
                    try:
                        ctx = {"platform": "douyin",
                               "tag": (entry.get("tags") or [None])[0] if entry.get("tags") else None,
                               "event": "new_post"}
                        pcfg = channel_to_push_cfg(common.resolve_channel(cfg_all or {}, ctx))
                        channel = (pcfg.get("type") or "unknown").lower()
                        res = dispatch_event(cfg_all or {}, ctx, title, desp)
                        logger.info("    → 推送%s", "成功" if res.ok else "失败")
                        content_hash = _content_hash(channel, "new_post", title + desp)
                        if res.ok:
                            if dedup_key:
                                persist.dedup_record(dedup_key)
                            persist.notify_log(channel_id=channel, event_type="new_post",
                                               content_hash=content_hash, status="ok")
                        elif res.last_error == "config: empty push_cfg":
                            pass
                        else:
                            last_err = (res.last_error or "未知错误")[:200]
                            logger.error("通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                                         channel, res.attempts, res.last_error, title)
                            _append(rid, name, "douyin", "error",
                                    detail=f"通知发送失败（{channel}）：{last_err}", push="pushed_fail")
                            persist.notify_log(channel_id=channel, event_type="new_post",
                                               content_hash=content_hash, status="fail")
                    except Exception as e:
                        logger.error("    → 推送异常: %s", e)

            if do_update:
                t["latest_aweme_id"] = aweme["aweme_id"]
                t["latest_ct"] = new_ct
                t["mode"] = conf
                t["latest_count"] = new_ct
                t["nickname"] = aweme.get("nickname") or t.get("nickname", "")
                if aweme.get("avatar"):
                    t["avatar"] = aweme["avatar"]
                if conf == "count":
                    t["need_cookie"] = True
                else:
                    t.pop("need_cookie", None)
                if conf == "api":
                    t["latest_desc"] = aweme.get("desc", "")
                    t["latest_type"] = kind
                    t["latest_url"] = aweme.get("video_url", "")
            else:
                logger.info("  [%s] 接口返回作品较旧，保留已有基线（抖音接口延迟）", name)
            persist.set_room_status(platform="douyin", external_id=rid, kind="post",
                                   name=name, meta_update=t)

        if gated_hint:
            logger.warning("部分账号作品接口被风控/未登录，新作品可能漏检。请在 BLIVE_CONFIG "
                           "增加 douyin_cookie 或设置环境变量 DOUYIN_COOKIE。")
        logger.info("[run_post_check] 新作品检测完成")
    finally:
        # 适配器可能持有自建的浏览器上下文（如快手为跨账号复用风控预热而缓存的
        # 那个）。统一回收：context 由 scheduler 传入时不会走下面的 browser.close()，
        # 不显式释放就会随进程一直堆积。
        if adapters is not None:
            for _code in (adapters.list_platforms() or []):
                _closer = getattr(adapters.get(_code), "close", None)
                if callable(_closer):
                    try:
                        _closer()
                    except Exception:  # noqa: BLE001
                        pass
        if own_context:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw.__exit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    main()
