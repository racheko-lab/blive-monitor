"""阶段三 T05：快手适配器（直播 + 新作，优雅降级）。

直播主路径为 SSR 解析（live.kuaishou.com/u/<id> 的 window.__INITIAL_STATE__），
真实结构：liveroom.playList[0].isLiving / .liveStream.*；旧结构 liveroom.living 兼容。
新作：visionProfilePhotoList graphql 返回 {"result":2}（风控/未登录）→ raise AdapterGated。
"""

import urllib.error
import urllib.request

import pytest

from backend.adapters import AdapterGated
from backend.adapters.kuaishou import KuaishouAdapter


class _FakeResp:
    """供 monkeypatch urllib.request.urlopen 的伪响应（支持 with 上下文）。"""

    def __init__(self, data: bytes):
        self._d = data

    def read(self) -> bytes:
        return self._d

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a) -> bool:
        return False


def test_kuaishou_capability_flags():
    a = KuaishouAdapter()
    assert a.platform == "kuaishou"
    assert a.supports_live is True
    assert a.supports_posts is True
    # 新作走 live_api/profile/public，该接口只认页面自身发出的带签名请求，
    # 因此必须有浏览器上下文（裸 HTTP 恒被挡，见 kuaishou_feed 模块文档）
    assert a.needs_context is True
    assert a.poll_interval == 300


# ---------------- 直播：SSR 主路径 ----------------

def _ssr_html(liveroom_state: str) -> bytes:
    return (
        '<html><body><script>window.__INITIAL_STATE__='
        + liveroom_state
        + ';</script></body></html>'
    ).encode("utf-8")


def test_kuaishou_live_ssr_real_structure(monkeypatch):
    """真实 SSR 结构：playList[0].isLiving=true + liveStream.*。"""
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":true,'
        '"liveStream":{"caption":"直播中标题","watcherCount":123,"coverUrl":"http://c.jpg"},'
        '"author":{}}]}}'
    )

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    m = KuaishouAdapter().fetch_room_status("123")
    assert m.live_status is True
    assert m.title == "直播中标题"
    assert m.online == 123
    assert m.cover == "http://c.jpg"
    assert m.extra.get("source") == "ssr"


def test_kuaishou_live_ssr_offline(monkeypatch):
    """playList[0].isLiving=false 且兜底也说没播 → offline。

    注意必须把 dynamicIcon 一起打桩：它是真实网络调用，不桩住的话这条单测会
    在 CI 里偷偷打外网，结果随快手线上状态漂移。
    """
    html = _ssr_html('{"liveroom":{"playList":[{"isLiving":false,"liveStream":{}}]}}')

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    # 兜底不可用（返回 None）时应保持 SSR 的结论
    monkeypatch.setattr(KuaishouAdapter, "_live_via_dynamic_icon", lambda self, rid: None)
    m = KuaishouAdapter().fetch_room_status("123")
    assert m.live_status is False
    assert m.extra.get("source") == "ssr"


def test_kuaishou_live_ssr_extracts_nickname(monkeypatch):
    """SSR 解析应提取主播昵称（author.name），用于前端显示用户名；
    即使 SSR 判 offline 触发 dynamicIcon 兜底，昵称也应从 SSR 透传、不被丢弃。"""
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":false,"liveStream":{},'
        '"author":{"id":"Sandy88888","name":"肥阿肥"}}]}}'
    )

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    # 控制 dynamicIcon 兜底返回，确保命中兜底分支且昵称被透传
    monkeypatch.setattr(
        KuaishouAdapter, "_live_via_dynamic_icon",
        lambda self, rid: {"living": False, "live_stream_id": "test_stream"},
    )
    m = KuaishouAdapter().fetch_room_status("Sandy88888")
    assert m.name == "肥阿肥"
    assert m.extra.get("source") == "dynamic_icon"


def test_kuaishou_live_ssr_extracts_avatar(monkeypatch):
    """SSR 解析应提取主播头像（author.avatar），用于前端头像显示；
    快手 SSR 把 '/' 转义为 '\\u002F'，需还原为真实 URL 分隔符。"""
    avatar_raw = "https:\\u002F\\u002Fp2-pro.a.yximgs.com\\u002Fuhead\\u002FAB\\u002Fxx_s.jpg"
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":false,"liveStream":{},'
        '"author":{"id":"Sandy88888","name":"肥阿肥","avatar":"'
        + avatar_raw + '"}}]}}'
    )

    def fake_get(self, url, headers=None, timeout=10):
        return html

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_get)
    # SSR 主路径：dynamicIcon 兜底返回 None，确保走 _room_from_html 提取
    monkeypatch.setattr(
        KuaishouAdapter, "_live_via_dynamic_icon", lambda self, rid: None,
    )
    m = KuaishouAdapter().fetch_room_status("Sandy88888")
    assert m.avatar == "https://p2-pro.a.yximgs.com/uhead/AB/xx_s.jpg"
    assert m.name == "肥阿肥"


def test_kuaishou_live_degrade_on_failure(monkeypatch):
    """SSR 与 dynamicIcon 双双失败 → 优雅降级 offline，不抛异常。"""
    def fake_raise(self, *args, **kwargs):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(KuaishouAdapter, "_http_get", fake_raise)
    monkeypatch.setattr(KuaishouAdapter, "_live_via_dynamic_icon", lambda self, rid: None)
    m = KuaishouAdapter().fetch_room_status("123")
    assert m.live_status is False
    assert m.extra.get("degraded") is True


def test_kuaishou_room_from_html_ssr():
    """兼容旧结构 liveroom.living（无 playList）。"""
    html = (
        'var x=1;window.__INITIAL_STATE__={"liveroom":'
        '{"living":true,"caption":"SSR标题","watcherCount":42,"coverUrl":"http://s.jpg"}};'
        "</script>"
    )
    m = KuaishouAdapter._room_from_html("123", html)
    assert m.live_status is True
    assert m.title == "SSR标题"
    assert m.online == 42
    assert m.extra.get("source") == "ssr"


def test_kuaishou_room_from_html_real_playlist():
    """真实结构直接从 _room_from_html 解析。"""
    html = _ssr_html(
        '{"liveroom":{"playList":[{"isLiving":true,'
        '"liveStream":{"caption":"真实标题","watcherCount":7,"coverUrl":"http://r.jpg"}}]}}'
    )
    m = KuaishouAdapter._room_from_html("123", html)
    assert m.live_status is True
    assert m.title == "真实标题"
    assert m.online == 7
    assert m.cover == "http://r.jpg"


def test_kuaishou_room_from_html_no_state():
    """无 __INITIAL_STATE__ → offline（不抛异常）。"""
    m = KuaishouAdapter._room_from_html("123", "<html>no state</html>")
    assert m.live_status is False


# ---------------- 新作：live_api/profile/public（浏览器通道）----------------
#
# 为什么这批测试推倒重来：旧实现打 www.kuaishou.com/graphql 的
# visionProfilePhotoList，而该端点已被快手前端弃用（打开 profile 页，页面自身
# 发出的 graphql 请求为 0），裸请求恒返回 result=2。旧测试用假 feeds 喂
# _fetch_graphql_photos，测的是「解析假数据的能力」，对「线上能不能抓到作品」
# 零覆盖 —— 所以它们全绿，而生产环境一条新作也报不出来。
#
# 下面的样本 **全部截自真实响应**（账号 pineapple2005，principalId
# 3x7ju263tgi5dn9），尤其是 CDN URL：发布时间就藏在里面，随手编的假 URL 解不出
# 时间，也就测不到真正的解析逻辑。

#: 真实响应片段：注意条目里既没有 timestamp 也没有 caption，且**前两条是置顶**
#: （2025-11-05 / 2025-03-05），真正最新的是第三条 2026-08-07。
_REAL_LIST = [
    {
        "id": "3x65q35quat5aku",
        "poster": "https://p2.a.yximgs.com/upic/2025/11/05/08/BMjAyNTExMDUwODUzNTBfMTgwNTM0MDAyXzE3OTA5MzU1NjQ1NV8xXzY=_Bbd34b6b79569510e180d2181ef37e6c0.jpg?clientCacheKey=3x65q35quat5aku.jpg",  # noqa: E501
        "workType": "multiple",
        "playUrl": "",
        "imgUrls": ["http://p2.a.yximgs.com/ufile/atlas/x_0.webp"],
        "musicName": "西厢寻他（0.8x）片段",
        "author": {"id": "pineapple2005", "name": "魅力驿站", "living": False},
    },
    {
        "id": "3xf6tyg537gawuk",
        "poster": "https://p2.a.yximgs.com/upic/2025/03/05/17/BMjAyNTAzMDUxNzAzMDRfMTgwNTM0MDAyXzE1ODQ4Mzc5NzQyM18xXzM=_Bx.jpg",  # noqa: E501
        "workType": "video",
        "playUrl": "https://hwmov.a.yximgs.com/upic/2025/03/05/17/BMjAyNTAzMDUxNzAzMDRfMTgwNTM0MDAyXzE1ODQ4Mzc5NzQyM18xXzM=_b_Bf7a20ead02a2e70556dd73c09611965e.mp4",  # noqa: E501
        "author": {"id": "pineapple2005", "name": "魅力驿站", "living": False},
    },
    {
        "id": "3x2ywf5zitae5zg",
        "poster": "https://p2.a.yximgs.com/upic/2026/08/07/16/BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_B94b24437ca17953af619e2b62ffed9b8.jpg",  # noqa: E501
        "workType": "video",
        "playUrl": "https://hwmov.a.yximgs.com/upic/2026/08/07/16/BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_b_B63e35bd16b353ea0e00535842fce5dbf.mp4",  # noqa: E501
        "author": {"id": "pineapple2005", "name": "魅力驿站", "living": False,
                   "avatar": "https://p2.a.yximgs.com/uhead/AB/abc_s.jpg"},
    },
]

_REAL_PAYLOAD = {
    "data": {
        "live": {"author": {"living": False}, "living": False},
        "list": _REAL_LIST,
        "result": 1,
    }
}


class _FakeSession:
    """替身作品流会话：只替掉「开浏览器」这一步，解析/排序/校验仍走真实代码。

    不是把结果 mock 成想要的样子 —— 喂进去的是真实响应 JSON，
    parse_profile_public / sort_by_time / pick_latest / verify_ownership
    全部照常执行，测的是真链路。
    """

    def __init__(self, payload=None, caption="#热辣一夏", exc=None):
        from backend.adapters import kuaishou_feed as kf

        self._kf = kf
        self._payload = payload if payload is not None else _REAL_PAYLOAD
        self._caption = caption
        self._exc = exc
        self.caption_calls = 0

    def fetch(self, pid):
        if self._exc:
            raise self._exc
        return self._kf.parse_profile_public(self._payload)

    def fetch_caption(self, photo_id):
        self.caption_calls += 1
        return self._caption

    def close(self):
        pass


def _adapter_with(session):
    a = KuaishouAdapter()
    a._feed_session = session
    return a


def test_kuaishou_新作按真实时间排序而非列表顺序():
    """接口把置顶作品放最前，取 list[0] 会让新作永远发现不了。

    这是该通道最凶险的坑：置顶作品长期不变，若把它当「最新」，基线就永远卡死，
    之后真发了新作也检测不到 —— 而且表现为「一直没有新作品」，毫无报错。
    """
    a = _adapter_with(_FakeSession())
    t = {"latest_post_id": "OLD", "latest_timestamp": 1}
    posts = a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())

    assert len(posts) == 1
    # 必须是 2026-08-07 那条，而不是排在首位的 2025-11-05 置顶
    assert posts[0].post_id == "3x2ywf5zitae5zg"
    assert posts[0].published_at == "2026-08-07 16:20:57"
    assert t["latest_post_id"] == "3x2ywf5zitae5zg"


def test_kuaishou_新作字段完整():
    a = _adapter_with(_FakeSession())
    t = {"latest_post_id": "OLD", "latest_timestamp": 1}
    p = a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())[0]

    assert p.platform == "kuaishou"
    assert p.author == "魅力驿站"
    assert p.title == "#热辣一夏"          # 接口不给 caption，从详情页标题补
    assert p.url == "https://www.kuaishou.com/short-video/3x2ywf5zitae5zg"
    assert p.cover.startswith("https://p2.a.yximgs.com/")
    assert p.extra["type"] == "视频"
    assert p.extra["conf"] == "api"
    assert p.extra["dedup_key"] == "post:kuaishou:3x2ywf5zitae5zg"


def test_kuaishou_首轮只建基线不推历史作品():
    """无基线时不能把历史作品当新作推送，否则新增一个号就刷屏。"""
    a = _adapter_with(_FakeSession())
    t = {}
    posts = a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert posts == []
    assert t["latest_post_id"] == "3x2ywf5zitae5zg"   # 但基线要建好


def test_kuaishou_基线一致时无新作():
    a = _adapter_with(_FakeSession())
    t = {"latest_post_id": "3x2ywf5zitae5zg", "latest_timestamp": 1786090857}
    assert a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object()) == []


def test_kuaishou_无新作且不缺文案时不浪费请求取文案():
    """文案要额外开一个详情页：已抓到文案的「无新作」轮不再重复付这个成本。"""
    sess = _FakeSession()
    a = _adapter_with(sess)
    a.fetch_new_posts("3x7ju263tgi5dn9",
                      baseline={"latest_post_id": "3x2ywf5zitae5zg",
                                "latest_timestamp": 1786090857,
                                "latest_desc": "已有文案"},
                      context=object())
    assert sess.caption_calls == 0


def test_kuaishou_缺文案时补取一次():
    """建基线/历史首次抓取时文案缺失，应补取一次（取完即停，不是每轮都取）。

    此前只在「确认有新作」时才付文案请求成本，导致建基线那轮不取文案——
    账号最新作品若是旧作、且无新作，文案永远空、作品卡显示「(无描述)」。
    """
    sess = _FakeSession()
    a = _adapter_with(sess)
    t = {"latest_post_id": "3x2ywf5zitae5zg", "latest_timestamp": 1786090857}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert sess.caption_calls == 1
    assert t["latest_desc"] == "#热辣一夏"


def test_kuaishou_头像写入tracking():
    """作品卡头像来自 profile 响应里的作者头像（条目 author.avatar 或 data.user），
    应写进 tracking[...].avatar，供前端作品卡显示（不再只靠直播头像兜底）。"""
    a = _adapter_with(_FakeSession())
    t = {}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    # 最新一条作品的作者头像（_REAL_LIST 第三条带 avatar）
    assert t["avatar"] == "https://p2.a.yximgs.com/uhead/AB/abc_s.jpg"


def test_kuaishou_头像取data_user兜底():
    """条目 author 没头像时，从 profile 属主 data.user.avatar 兜底。"""
    payload = {
        "data": {
            "user": {"id": "u1", "name": "n1", "avatar": "https://p2.a.yximgs.com/uhead/AB/user_s.jpg"},
            "list": [
                {"id": "p1",
                 "poster": "https://p2.a.yximgs.com/upic/2026/08/07/16/BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_Bx.jpg",
                 "workType": "video",
                 "playUrl": "https://hwmov.a.yximgs.com/upic/2026/08/07/16/BMjAyNjA4MDcxNjIwNTdfMTgwNTM0MDAyXzIwNDc2OTkzNjI0Ml8xXzM=_b_Bx.mp4",
                 "author": {"id": "u1", "name": "n1"}},
            ],
            "result": 1,
        }
    }
    a = _adapter_with(_FakeSession(payload=payload))
    t = {}
    a.fetch_new_posts("u1", baseline=t, context=object())
    assert t["avatar"] == "https://p2.a.yximgs.com/uhead/AB/user_s.jpg"


def test_kuaishou_归属校验拦截他人作品():
    """URL 反解出的 userId 与账号对不上时必须拒绝，不能把别人的作品推给用户。"""
    from backend.adapters.base import AdapterSkip

    a = _adapter_with(_FakeSession())
    # tracking 里记的是另一个人的 userId
    t = {"origin_user_id": "999999", "latest_post_id": "OLD", "latest_timestamp": 1}
    with pytest.raises(AdapterSkip):
        a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())


def test_kuaishou_首轮自举记录归属标识():
    """首轮把反解到的 userId / 用户名固化，之后即可用作强校验。"""
    a = _adapter_with(_FakeSession())
    t = {}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert t["origin_user_id"] == "180534002"
    assert t["unique_name"] == "pineapple2005"
    assert t["nickname"] == "魅力驿站"


def test_kuaishou_无浏览器上下文直接gated():
    """没有浏览器就别假装尝试：裸请求 100% 空，会把「被挡」伪装成「没有新作」。"""
    with pytest.raises(AdapterGated):
        KuaishouAdapter().fetch_new_posts("3x7ju263tgi5dn9", baseline={}, context=None)


def test_kuaishou_result2记为gated而非无新作():
    """result=2 是风控预热未过，必须与「确实没有新作」区分开。"""
    payload = {"data": {"list": [], "result": 2, "live": {"author": {"living": False}}}}
    a = _adapter_with(_FakeSession(payload=payload))
    t = {}
    with pytest.raises(AdapterGated):
        a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert t.get("gated_count")


def test_kuaishou_抓取异常记为gated():
    a = _adapter_with(_FakeSession(exc=RuntimeError("浏览器崩了")))
    with pytest.raises(AdapterGated):
        a.fetch_new_posts("3x7ju263tgi5dn9", baseline={}, context=object())


def test_kuaishou_解不出时间时拒绝猜测():
    """一条时间都解不出就如实报 gated，绝不用置顶作品冒充最新。"""
    payload = {
        "data": {
            "list": [{"id": "pX", "poster": "https://example.com/no-date.jpg",
                      "workType": "video", "playUrl": "",
                      "author": {"id": "u", "name": "n"}}],
            "result": 1,
            "live": {"author": {"living": False}},
        }
    }
    a = _adapter_with(_FakeSession(payload=payload))
    with pytest.raises(AdapterGated):
        a.fetch_new_posts("3x7ju263tgi5dn9", baseline={}, context=object())


def test_kuaishou_顺带产出直播态():
    """同一响应带 live.author.living，写进 tracking 供直播链路对账。"""
    a = _adapter_with(_FakeSession())
    t = {}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert t["living_hint"] is False


def test_kuaishou_如实记录为匿名凭证等级():
    """该通道不使用登录 Cookie，tracking 不得谎称用了更高等级凭证。"""
    a = _adapter_with(_FakeSession())
    t = {}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert t["credential_level"] == "anonymous"
    assert t["cookie_used"] is False
    assert t["did_used"] is False


# ---------------- 直播：dynamicIcon 兜底 ----------------

def test_kuaishou_dynamic_icon解析(monkeypatch):
    """真实响应形态：{"result":1,"data":{"isLiving":true,"liveStreamId":"..."}}。"""
    def fake_urlopen(req, timeout=10):
        return _FakeResp(b'{"result":1,"data":{"isLiving":true,"liveStreamId":"xbfLJKLsoX0"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = KuaishouAdapter()._live_via_dynamic_icon("3x7ju263tgi5dn9")
    assert got == {"living": True, "live_stream_id": "xbfLJKLsoX0"}


def test_kuaishou_dynamic_icon失败返回None(monkeypatch):
    """兜底失败必须返回 None（保持原判定），不能反过来把在播判成离线。"""
    def boom(req, timeout=10):
        raise urllib.error.URLError("nope")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert KuaishouAdapter()._live_via_dynamic_icon("x") is None


def test_kuaishou_SSR判离线时用dynamic_icon纠正(monkeypatch):
    """SSR 被风控喂空页面时会静默判 offline，与真没开播长得一样 —— 开播漏报的典型成因。"""
    def fake_urlopen(req, timeout=10):
        if getattr(req, "method", "GET") == "POST":
            return _FakeResp(b'{"result":1,"data":{"isLiving":true,"liveStreamId":"S1"}}')
        return _FakeResp(b"<html>no state</html>")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    m = KuaishouAdapter().fetch_room_status("3x7ju263tgi5dn9")
    assert m.live_status is True
    assert m.extra["source"] == "dynamic_icon"
    assert m.extra["live_stream_id"] == "S1"


# ---------------------------------------------------------------------------
# 时区回归：tracking 里的「北京时间」不得随运行机器时区漂移
# ---------------------------------------------------------------------------
# 背景（真实生产 Bug）：kuaishou.py 曾自带 _ts_to_bj，内部用裸
# datetime.fromtimestamp(ts) —— 跟随系统时区。GitHub Actions runner 默认 UTC
# 且 workflow 未设 TZ，导致线上写入 latest_published_at 的「北京时间」实际是
# UTC，整体偏早 8 小时。common.epoch_to_beijing 的 docstring 写着它已「合并自
# kuaishou 的 _ts_to_bj」，但适配器当时并未真正切过去，属重构遗留。


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Shanghai"])
def test_kuaishou_时间戳转换不随系统时区漂移(tz, monkeypatch):
    """无论 runner 在哪个时区，epoch 都必须换算成 +8 的北京时间。"""
    import time

    from backend.adapters.kuaishou import _ts_to_bj

    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        # 1700000000 = 2023-11-14 22:13:20 UTC = 2023-11-15 06:13:20 北京
        assert _ts_to_bj(1700000000) == "2023-11-15 06:13:20"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_kuaishou_当前时间使用北京时区():
    """_now_bj 必须与 common.bjnow 同源，不能是裸 datetime.now()。"""
    from datetime import datetime

    from backend.adapters.kuaishou import _now_bj
    from common import bjnow

    got = datetime.strptime(_now_bj(), "%Y-%m-%d %H:%M:%S")
    assert abs((got - bjnow()).total_seconds()) < 5


def test_kuaishou_运行观测字段可JSON序列化():
    """任务七字段最终要落进 post_tracking.json，不能混入枚举等非 JSON 类型。"""
    import json

    from backend.adapters.kuaishou import KuaishouAdapter

    a = KuaishouAdapter()
    t: dict = {}
    a._write_run_tracking(t, "3xrgxqkqp829xz6", success=True)
    json.dumps(t)  # 不抛即通过
    assert t["principal_id"] == "3xrgxqkqp829xz6"
    assert t["last_success"]


def test_kuaishou_运行观测不覆盖已有principal_id():
    """tracking 里已校验过的 principal_id 优先，避免被入参 rid 冲掉。"""
    from backend.adapters.kuaishou import KuaishouAdapter

    t = {"principal_id": "3xoldoldoldold1"}
    KuaishouAdapter()._write_run_tracking(t, "3xrgxqkqp829xz6", success=False)
    assert t["principal_id"] == "3xoldoldoldold1"
    assert "last_success" not in t  # 失败轮次不得刷新成功时间


def test_kuaishou_退化列表不能把基线打回旧帖():
    """基线是 2026-08-07（最新），本轮列表退化只剩旧帖 → 基线保持不动。

    2026-08 实测：快手按访问者地域返回不同列表——同一账号，海外出口的列表
    比大陆出口少最新一条（还回 pcursor=no_more 伪装成「就这些」）。若此时把
    基线回写成旧帖：① 看板上「最新作品」倒退；② 下一轮正常列表又把那条旧帖
    当新作重推。基线必须只进不退。
    """
    degraded_payload = {
        "data": {
            "live": {"author": {"living": False}, "living": False},
            "list": _REAL_LIST[:2],   # 只剩 2025-11-05 / 2025-03-05 两条旧帖
            "result": 1,
        }
    }
    a = _adapter_with(_FakeSession(payload=degraded_payload))
    t = {"latest_post_id": "3x2ywf5zitae5zg", "latest_timestamp": 1786090857,
         "latest_published_at": "2026-08-07 16:20:57"}
    posts = a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())

    assert posts == []                              # 不推
    assert t["latest_post_id"] == "3x2ywf5zitae5zg"  # 基线不回退
    assert t["latest_timestamp"] == 1786090857
    assert t["latest_published_at"] == "2026-08-07 16:20:57"
    assert t.get("last_success")                    # 本轮抓取本身是成功的
    # §4.4：退化轮需显式打标，否则 check_new_posts 只会看到「无新作」而静默放过
    assert t.get("degraded_this_round") is True


def test_kuaishou_首轮建基线不受回退保护影响():
    """没有基线时照常建基线（哪怕列表很旧）——回退保护只保护已有基线。"""
    a = _adapter_with(_FakeSession(payload={
        "data": {"live": {"author": {"living": False}, "living": False},
                 "list": _REAL_LIST[:1], "result": 1},
    }))
    t: dict = {}
    posts = a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    assert posts == []                       # 首轮不推历史
    assert t["latest_post_id"] == "3x65q35quat5aku"


def test_kuaishou_本轮实际列表写入观测字段():
    """last_fetch_items 记录接口真实返回（id+ts），供排查地域/风控差异。"""
    a = _adapter_with(_FakeSession())
    t: dict = {}
    a.fetch_new_posts("3x7ju263tgi5dn9", baseline=t, context=object())
    items = t.get("last_fetch_items")
    assert isinstance(items, list) and len(items) == 3
    assert {it["id"] for it in items} == {
        "3x65q35quat5aku", "3xf6tyg537gawuk", "3x2ywf5zitae5zg"}
    assert all("ts" in it for it in items)
