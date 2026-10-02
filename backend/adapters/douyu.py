"""DouyuAdapter：斗鱼直播（仅直播 ✅，新作 ❌）。

数据源：斗鱼开放平台免鉴权公开接口
    https://open.douyucdn.cn/api/RoomApi/room/{room_id}

实测（2026-10-02，沙箱出口 IP 匿名、无 Cookie）:
    15000   -> {"error":0,"data":{...,"room_status":"0","owner_name":"用户已注销"}}
    36252   -> {"error":0,"data":{...,"room_status":"1","owner_name":"MrGemini",
                "online":5622051,"cate_name":"王者荣耀","start_time":"2026-10-02 13:25:13"}}
    9999    -> {"error":0,"data":{...,"room_status":"1","owner_name":"yyfyyf"}}

边界（异常必须显式报错，不能静默当 offline，否则会污染历史/漏报）:
    /room/0         -> {"error":101,"data":"房间未找到"}
    /room/abc       -> {"error":101,"data":"仅支持房间ID获取房间详情"}
    /room/999999999 -> 纯文本 "Not Found"（非 JSON）

为什么不用 SSR：斗鱼房间页（www.douyu.com/{rid}）的直播态走 WebSocket/长轮询，
SSR HTML 里没有可靠状态位；而 open.douyucdn.cn 是官方公开的 CDN 接口，
匿名可用、响应小（gift 数组虽大但可截断）、无需 Cookie，是本仓库最省心的平台。

字段映射:
    data.room_name  -> RoomModel.title        （直播间标题）
    data.owner_name -> RoomModel.name         （主播昵称）
    data.room_status == "1" -> live_status=True
    data.online     -> RoomModel.online       （人气值，斗鱼为加权热度非真实人数）
    data.cate_name  -> RoomModel.area         （分区）
    data.avatar     -> RoomModel.avatar
    data.room_thumb -> RoomModel.cover
    url = https://www.douyu.com/{room_id}
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from common import DEFAULT_USER_AGENT
from backend.adapters.base import PlatformAdapter, RoomModel, AdapterError

logger = logging.getLogger(__name__)

#: 房间详情接口（免鉴权，公开 CDN）
ROOM_API = "https://open.douyucdn.cn/api/RoomApi/room/{room_id}"

#: room_status 语义。斗鱼官方状态位：0=未开播，1=直播中。
#: 2 在部分文档中标为「视频回放」，但本轮未在真实响应中实测到，
#: 因此不做映射（视为未开播），仅把原值存进 extra 供后续排查。
STATUS_LIVE = "1"


class DouyuAdapter(PlatformAdapter):
    """斗鱼直播适配器（仅直播）。"""

    platform = "douyu"
    supports_live = True
    supports_posts = False  # 斗鱼无「新作品」形态，本轮不做
    poll_interval = 60
    rate_limit = {"max_requests": 60, "window_sec": 60, "backoff_sec": 5}
    needs_context = False

    def __init__(self, credentials: Optional[Dict[str, Any]] = None,
                 poll_interval: Optional[int] = None,
                 rate_limit: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(credentials or {}, poll_interval, rate_limit)

    def _http_get_json(self, url: str, timeout: int = 10) -> Dict[str, Any]:
        """GET + JSON 解析。非 JSON / HTTP 错误一律抛 AdapterError（不静默降级）。"""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": "https://www.douyu.com/",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise AdapterError(f"斗鱼接口 HTTP {e.code}") from e
        except Exception as e:  # noqa: BLE001  # 网络层异常统一归一
            raise AdapterError(f"斗鱼接口请求失败: {e}") from e
        try:
            obj = json.loads(raw)
        except Exception as e:  # noqa: BLE001  # 例如超大房间号返回纯文本 "Not Found"
            raise AdapterError(f"斗鱼接口返回非 JSON: {raw[:40]}") from e
        if not isinstance(obj, dict):
            raise AdapterError(f"斗鱼接口返回结构异常: {type(obj).__name__}")
        return obj

    def fetch_room_status(self, room_id: str) -> RoomModel:
        """查询单个斗鱼房间直播态。

        Raises:
            AdapterError: 房间不存在 / 房间号非法 / 接口不可达。
                编排层（check_status.fetch_douyu）会捕获并转为 status="error"，
                与 bili/douyin/kuaishou 一致，不会静默当 offline。
        """
        room_id = str(room_id).strip()
        if not room_id.isdigit():
            raise AdapterError(f"斗鱼房间号必须为纯数字: {room_id!r}")

        obj = self._http_get_json(ROOM_API.format(room_id=room_id))
        if obj.get("error") not in (0, "0"):
            raise AdapterError(f"斗鱼房间不可用: error={obj.get('error')} {obj.get('data')}")
        data = obj.get("data")
        if not isinstance(data, dict) or not data:
            raise AdapterError(f"斗鱼房间 {room_id} 无数据")

        status_raw = str(data.get("room_status") or "")
        return RoomModel(
            platform="douyu",
            room_id=str(data.get("room_id") or room_id),
            name=str(data.get("owner_name") or ""),
            title=str(data.get("room_name") or ""),
            live_status=(status_raw == STATUS_LIVE),
            url=f"https://www.douyu.com/{room_id}",
            cover=str(data.get("room_thumb") or ""),
            avatar=str(data.get("avatar") or ""),
            online=int(data.get("online") or 0),
            area=str(data.get("cate_name") or ""),
            extra={
                "room_status_raw": status_raw,
                "start_time": str(data.get("start_time") or ""),
                "source": "open.douyucdn.cn/RoomApi",
            },
        )

    def fetch_room_status_batch(self, room_ids: List[str]) -> Dict[str, RoomModel]:
        """批量查询（默认逐个串行；斗鱼公开接口无真批量端点）。

        单房失败不影响其余房间——失败项不进返回 dict，由编排层按「无数据」处理。
        """
        out: Dict[str, RoomModel] = {}
        for rid in room_ids or []:
            try:
                out[str(rid)] = self.fetch_room_status(rid)
            except Exception as e:  # noqa: BLE001
                logger.warning("[douyu] 房间 %s 查询失败（跳过）: %s", rid, e)
        return out

    def fetch_new_posts(self, author_or_room: str, since: Optional[Any] = None,
                        baseline: Optional[Dict[str, Any]] = None,
                        context: Any = None) -> List[Any]:
        # 斗鱼无「新作品」形态（supports_posts=False）；编排层不会调用，
        # 但显式抛 NotImplementedError 以防误用。
        raise NotImplementedError("斗鱼仅支持直播检测（supports_posts=False）")
