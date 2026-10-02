#!/usr/bin/env python3
"""
B站/抖音直播状态检测（GitHub Actions 用）

功能说明：
- B站: 官方 API 批量查询
- 抖音: 页面 SSR 数据提取（多种策略兜底）
- 状态变化时通过多通道推送（Bark / Server酱 / 企业微信 / PushPlus / Telegram）
- 更新 status.json / state.json / history.json / tracking.json
"""

import json
import os
import re
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# 公共工具（时间/JSON 读写），避免与 check_new_posts.py 重复定义
from common import (
    bjnow,
    load_json_file,
    save_json_file,
    DEFAULT_USER_AGENT,
    BEIJING_TZ,
    room_enabled,
    load_silence_cfg,
    should_skip_by_silence,
    content_hash,
)
import common  # A2/A4 统一路由：common.resolve_channel / common.render_template（dispatch_event 同源）
# 多通道推送（与 check_new_posts.py 共用 push_utils.py）
from push_utils import load_push_cfg, dispatch_event, channel_to_push_cfg
# 通知去重账本：与状态持久化解耦的独立防线，杜绝重复推送
from notify_dedup import should_notify as dedup_should_notify, record as dedup_record, prune as dedup_prune, sync_from_remote as dedup_sync_from_remote
# 横切模块：运行时日志 + history 读写/上限（HISTORY_MAX 唯一来源）
from log_utils import (
    HISTORY_MAX, init_runtime_logging, load_history, append_history,
    type_from_status, level_from_type, dedupe_by_throttle,
)
# 级联清理（history 孤儿）：固化阶段裁剪已删房间的残留日志
import state_prune

# ==================== 常量配置 ====================

# 文件路径
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(REPO_DIR, "state.json")
STATUS_FILE = os.path.join(REPO_DIR, "status.json")
HISTORY_FILE = os.path.join(REPO_DIR, "history.json")
TRACKING_FILE = os.path.join(REPO_DIR, "tracking.json")
ROOMS_FILE = os.path.join(REPO_DIR, "rooms.json")

# 历史日志保留条数：统一引用 log_utils.HISTORY_MAX（500，唯一来源），不再各自定义。

# HTTP 请求默认配置（DEFAULT_USER_AGENT 定义在 common.py，两脚本共用）
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 2

# 抖音状态码
DOUYIN_STATUS_LIVE = 2
DOUYIN_STATUS_OFFLINE = 4

# B站状态码映射
BILIBILI_STATUS_MAP = {
    0: "offline",
    1: "live",
    2: "replay",
}

# 小红书直播开播检测已于 2026-07-10 移除（无头 Chromium 渲染直播间方案；平台每次开播
# 短链会变、数据中心 IP 触发风控，维护成本高且误判/漏推难根除）。当前小红书直播监控未支持。

# ==================== 日志配置 ====================

# 统一走 log_utils 的运行时日志（控制台 + 文件轮转 + 结构化上下文）。
# 其余 logger.info/warning/error 调用零改动（account 缺省空串，兼容既有输出解析）。
init_runtime_logging()
logger = logging.getLogger(__name__)


# ==================== 工具函数 ====================
# bjnow / load_json_file / save_json_file 见 common.py（与 check_new_posts.py 共用）

# ==================== 配置加载 ====================

def load_config() -> Dict[str, Any]:
    """加载配置（rooms.json + 环境变量 BLIVE_CONFIG）"""
    # 从 rooms.json 加载房间列表
    rooms: List[Dict[str, str]] = []
    if os.path.exists(ROOMS_FILE):
        try:
            with open(ROOMS_FILE, "r", encoding="utf-8") as f:
                rooms = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("加载 rooms.json 失败: %s", e)

    # 推送配置：多通道（serverchan/wecom/pushplus/bark/telegram），兼容旧 sendkey
    raw_config = os.environ.get("BLIVE_CONFIG", "{}")
    push_cfg = load_push_cfg(raw_config)
    # A3 静默时段：从 BLIVE_CONFIG.silence 解析（无则 {}，不静默）
    silence_cfg = load_silence_cfg(raw_config)
    # 完整配置（参与多通道路由 / 模板渲染）：供 dispatch_event 消费（A2/A4）
    cfg_all = json.loads(raw_config) if raw_config else {}

    return {
        "push_cfg": push_cfg,
        "silence": silence_cfg,
        "rooms": rooms,
        "cfg_all": cfg_all,
    }


# ==================== HTTP 请求 ====================

def fetch_with_retry(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT,
) -> bytes:
    """带重试的 HTTP 请求

    Args:
        url: 请求 URL
        headers: 请求头
        retries: 重试次数
        timeout: 超时时间（秒）

    Returns:
        响应内容 bytes

    Raises:
        Exception: 所有重试都失败时抛出最后一次异常
    """
    last_err: Optional[Exception] = None
    base_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        base_headers.update(headers)

    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            if i < retries:
                delay = 2 ** i
                logger.warning("请求失败，%d秒后重试 (%d/%d): %s", delay, i + 1, retries, e)
                time.sleep(delay)

    if last_err is None:
        raise RuntimeError("fetch_with_retry: 不应到达此处（重试耗尽但无错误）")
    raise last_err


# ==================== B站 API ====================

def fetch_bilibili_batch(room_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """B站直播间批量检测 - getRoomBaseInfo 接口

    Args:
        room_ids: 直播间 ID 列表

    Returns:
        {room_id_str: {live_status, title, uname, online, ...}}

    Raises:
        Exception: API 返回错误时抛出
    """
    params = [("req_biz", "web_room_componet")]
    for rid in room_ids:
        params.append(("room_ids", rid))

    url = (
        "https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo?"
        + urllib.parse.urlencode(params)
    )

    raw = fetch_with_retry(
        url,
        headers={
            "Referer": "https://live.bilibili.com/",
        },
    )

    data = json.loads(raw)
    if data.get("code") != 0:
        raise Exception(f"B站批量接口错误: code={data.get('code')}, msg={data.get('message')}")

    return data["data"]["by_room_ids"]


# ==================== B站 真实头像（live_user/v1/Master/info） ====================

def _fetch_bilibili_face(uid: str) -> str:
    """取主播真实头像（face）。

    参考本仓库同系项目 new-monitor-project：getRoomBaseInfo 已返回 uid，
    再用 live_user/v1/Master/info?uid={uid} 取 face（无需 WBI 签名，实测
    数据中心 IP 不风控，code=0）。失败优雅降级返回 ''，前端回退首字母圆。
    任何异常都不抛出，避免影响整轮检测。
    """
    try:
        url = f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}"
        data = json.loads(
            fetch_with_retry(url, headers={"Referer": "https://live.bilibili.com/"})
        )
        if data.get("code") != 0:
            return ""
        return data.get("data", {}).get("info", {}).get("face", "") or ""
    except Exception as e:
        logger.warning("B站头像获取失败 (uid=%s): %s", uid, e)
        return ""


# ==================== B站 主播名提取 ====================

def _bili_uname(d: Dict[str, Any]) -> str:
    """从 getRoomBaseInfo 的 by_room_ids[rid] 条目提取主播名。

    真实接口在顶层 ``uname`` 与嵌套 ``uinfo.base.name`` 两处都可能出现，
    兼容提取；都缺时返回空串（编排层回退到 rooms.json 的 name）。
    """
    if not isinstance(d, dict):
        return ""
    u = d.get("uname")
    if u:
        return str(u)
    uinfo = d.get("uinfo") or {}
    base = uinfo.get("base") or {}
    n = base.get("name")
    return str(n) if n else ""


# ==================== 抖音数据提取（多种策略） ====================

def _extract_douyin_from_render_data(html: str) -> Optional[Dict[str, Any]]:
    """策略1: 从 RENDER_DATA 中提取房间数据"""
    # 尝试匹配房间状态数据（多种格式变体）
    patterns = [
        # 标准格式
        r'\\"id_str\\":\\"(\d+)\\",\\"status\\":(\d+),\\"status_str\\":\\"(\d+)\\",\\"title\\":\\"([^"\\]*)\\".*?\\"user_count_str\\":\\"(\d+)\\"',
        # 不带 status_str
        r'\\"id_str\\":\\"(\d+)\\",\\"status\\":(\d+),\\"title\\":\\"([^"\\]*)\\".*?\\"user_count_str\\":\\"(\d+)\\"',
        # user_count 是数字
        r'\\"id_str\\":\\"(\d+)\\",\\"status\\":(\d+),\\"title\\":\\"([^"\\]*)\\".*?\\"user_count\\":(\d+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            groups = match.groups()
            # 根据匹配组数解析
            if len(groups) == 5:
                _, status_code, _, title, user_count = groups
            elif len(groups) == 4:
                _, status_code, title, user_count = groups
            else:
                continue

            try:
                status_code_int = int(status_code)
                user_count_int = int(user_count)
            except (ValueError, TypeError):
                continue

            status = "live" if status_code_int == DOUYIN_STATUS_LIVE else "offline"
            return {
                "status": status,
                "title": title,
                "online": user_count_int,
            }

    return None


def _extract_douyin_from_share_meta(html: str) -> Optional[Dict[str, Any]]:
    """策略2: 从分享 meta 标签提取"""
    # 检查是否直播中
    share_desc_match = re.search(
        r'shareDesc["\s]*value=["\s]*([^"]+)', html
    )
    if share_desc_match and "正在直播" in share_desc_match.group(1):
        title_match = re.search(
            r'shareTitle["\s]*value=["\s]*([^"]+)', html
        )
        title = title_match.group(1).replace("的直播", "") if title_match else ""
        return {
            "status": "live",
            "title": title,
            "online": 0,
        }

    # 检查是否已结束
    if "直播已结束" in html:
        return {
            "status": "offline",
            "title": "",
            "online": 0,
        }

    return None


def _extract_douyin_from_page_text(html: str) -> Optional[Dict[str, Any]]:
    """策略3: 从页面文本关键词推断"""
    # 直播中的特征文本
    live_indicators = ["正在直播", "直播中", "观看人数"]
    offline_indicators = ["直播已结束", "该主播暂无直播", "主播不在"]

    live_count = sum(1 for indicator in live_indicators if indicator in html)
    offline_count = sum(1 for indicator in offline_indicators if indicator in html)

    if live_count > offline_count and live_count >= 2:
        return {"status": "live", "title": "", "online": 0}
    if offline_count >= 1:
        return {"status": "offline", "title": "", "online": 0}

    return None


def _extract_douyin_nickname(html: str) -> str:
    """提取主播昵称"""
    # 从 nickname 字段提取
    for match in re.finditer(r'\\"nickname\\":\\"([^"\\]+)\\"', html):
        val = match.group(1)
        if val and val != "$undefined" and not val.startswith("$"):
            return val

    # 从 og:title 提取
    og_title_match = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', html)
    if og_title_match:
        title = og_title_match.group(1)
        if "的直播" in title:
            return title.replace("的直播", "").strip()
        return title.strip()

    return ""


def _extract_douyin_sec_uid(html: str) -> str:
    """提取 sec_uid"""
    # 方法1: 直接查找 sec_uid 字段
    idx = html.find('sec_uid')
    if idx >= 0:
        start = html.find('\\"', idx + 10)
        if start >= 0:
            end = html.find('\\"', start + 2)
            if end >= 0 and end - start < 200:
                return html[start + 2 : end]

    # 方法2: 正则匹配
    match = re.search(r'\\"sec_uid\\":\\"([^"\\]+)\\"', html)
    if match:
        return match.group(1)

    return ""


def _extract_douyin_avatar(html: str) -> str:
    """从抖音 RENDER_DATA 提取主播真实头像 URL（avatar_thumb.url_list[0]）。

    兼容两种形态：
    - 转义 JSON（html 中形如 \\"avatar_thumb\\":{\\"uri\\":...,\\"url_list\\":[\\"https://...\\"]}）
    - URL 编码（%22avatar_thumb%22%3A%7B...%22url_list%22%3A%5B%22https%3A%2F%2F...%22）
    取 avatar_thumb 之后第一个 http(s) 链接；含 % 则 URL 解码。失败返回 ''。
    """
    pat = re.compile(
        r'avatar_thumb.{0,1200}?(https?%?(?:://|%3A%2F%2F).*?)(?=%22|\\"|\s|$)',
        re.IGNORECASE | re.DOTALL,
    )
    m = pat.search(html)
    if not m:
        return ""
    url = m.group(1).strip()
    if "%" in url:
        try:
            url = urllib.parse.unquote(url)
        except Exception:
            pass
    return url


def _get_douyin_ttwid() -> str:
    """获取抖音 ttwid cookie（设备标识，无需登录）。

    通过 ttwid.bytedance.com 注册接口获取，用于 webcast/room/web/enter 接口。
    失败返回空字符串（接口仍可尝试不带 ttwid 请求）。
    """
    try:
        body = json.dumps({
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.douyin.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }).encode()
        req = urllib.request.Request(
            "https://ttwid.bytedance.com/ttwid/union/register/",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            set_cookie = resp.headers.get("Set-Cookie", "")
            match = re.search(r"ttwid=([^;]+)", set_cookie)
            if match:
                return match.group(1)
    except Exception as e:
        logger.debug("获取 ttwid 失败: %s", e)
    return ""


def _fetch_douyin_enter(web_rid: str) -> Optional[Dict[str, Any]]:
    """通过 webcast/room/web/enter 接口获取抖音直播间结构化数据。

    该接口返回 JSON 格式的房间信息，比 HTML 解析更稳定。
    需要 ttwid cookie（通过 _get_douyin_ttwid 获取）。

    Args:
        web_rid: 直播间 web_rid（即 URL 中的短ID）

    Returns:
        包含 status/title/online/nickname/sec_uid/avatar 的字典，失败返回 None
    """
    now_str = bjnow().strftime("%Y-%m-%d %H:%M:%S")
    ttwid = _get_douyin_ttwid()

    params = {
        "aid": "6383",
        "app_name": "douyin_web",
        "live_id": "1",
        "device_platform": "web",
        "language": "zh-CN",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "130.0.0.0",
        "web_rid": web_rid,
    }
    url = f"https://live.douyin.com/webcast/room/web/enter/?{urllib.parse.urlencode(params)}"

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
        "Referer": f"https://live.douyin.com/{web_rid}",
    }
    if ttwid:
        headers["Cookie"] = f"ttwid={ttwid}"

    try:
        raw = fetch_with_retry(url, headers=headers, retries=1, timeout=8)
        data = json.loads(raw)

        room_list = data.get("data", {}).get("data", [])
        if not room_list:
            logger.debug("抖音 enter 接口无 room 数据: %s", web_rid)
            return None

        room = room_list[0]
        status_code = room.get("status", 0)
        title = room.get("title", "")
        user_count = _as_int(room.get("user_count_str", room.get("user_count", 0)))

        # room_status: 0=正在直播, 2=未在直播
        # room.status: 2=正在直播(DOUYIN_STATUS_LIVE), 4=未在直播(DOUYIN_STATUS_OFFLINE)
        status = "live" if status_code == DOUYIN_STATUS_LIVE else "offline"

        # 从 user 对象提取主播信息
        user = data.get("data", {}).get("user", {})
        nickname = user.get("nickname", "")
        sec_uid = user.get("sec_uid", "")
        avatar = ""
        avatar_thumb = user.get("avatar_thumb", {})
        if isinstance(avatar_thumb, dict):
            url_list = avatar_thumb.get("url_list", [])
            if url_list:
                avatar = url_list[0] if isinstance(url_list, list) else str(url_list)

        result = {
            "status": status,
            "title": title,
            "online": user_count,
            "area": "",
            "nickname": nickname,
            "sec_uid": sec_uid,
            "avatar": avatar,
            "time": now_str,
        }
        logger.debug("抖音 enter 接口成功: %s → %s", web_rid, status)
        return result

    except Exception as e:
        logger.debug("抖音 enter 接口失败 (%s): %s", web_rid, e)
        return None


def fetch_douyin(web_rid: str) -> Dict[str, Any]:
    """抖音直播间检测 - 多种策略兜底提取

    策略优先级：
    0. webcast/room/web/enter 接口（结构化 JSON，最稳定）
    1. RENDER_DATA 提取
    2. 分享 meta 提取
    3. 页面文本关键词推断（兜底）

    Args:
        web_rid: 直播间 web_rid

    Returns:
        直播间状态字典
    """
    now_str = bjnow().strftime("%Y-%m-%d %H:%M:%S")

    # 策略0: webcast/room/web/enter 接口（优先，结构化 JSON 最稳定）
    result = _fetch_douyin_enter(web_rid)
    if result:
        logger.debug("抖音策略0成功 (enter API): %s", web_rid)
        return result

    # 策略1-3: 回退到 HTML 解析
    url = f"https://live.douyin.com/{web_rid}"

    try:
        raw = fetch_with_retry(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        html = raw.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("获取抖音页面失败 (%s): %s", web_rid, e)
        return {
            "status": "error",
            "title": f"获取失败: {str(e)}",
            "online": 0,
            "area": "",
            "nickname": "",
            "sec_uid": "",
            "avatar": "",
            "time": now_str,
        }

    # 提取公共字段
    nickname = _extract_douyin_nickname(html)
    sec_uid = _extract_douyin_sec_uid(html)
    avatar = _extract_douyin_avatar(html)

    # 策略1: 从 RENDER_DATA 提取（最准确）
    result = _extract_douyin_from_render_data(html)
    if result:
        result.update(
            {
                "area": "",
                "nickname": nickname,
                "sec_uid": sec_uid,
                "avatar": avatar,
                "time": now_str,
            }
        )
        logger.debug("抖音策略1成功 (RENDER_DATA): %s", web_rid)
        return result

    # 策略2: 从分享 meta 提取
    result = _extract_douyin_from_share_meta(html)
    if result:
        result.update(
            {
                "area": "",
                "nickname": nickname,
                "sec_uid": sec_uid,
                "avatar": avatar,
                "time": now_str,
            }
        )
        logger.debug("抖音策略2成功 (share_meta): %s", web_rid)
        return result

    # 策略3: 页面文本关键词推断（兜底）
    result = _extract_douyin_from_page_text(html)
    if result:
        result.update(
            {
                "area": "",
                "nickname": nickname,
                "sec_uid": sec_uid,
                "avatar": avatar,
                "time": now_str,
            }
        )
        logger.debug("抖音策略3成功 (page_text): %s", web_rid)
        return result

    # 所有策略都失败，默认返回离线状态
    logger.warning("抖音所有提取策略都失败: %s", web_rid)
    return {
        "status": "offline",
        "title": "",
        "online": 0,
        "area": "",
        "nickname": nickname,
        "sec_uid": sec_uid,
        "avatar": avatar,
        "time": now_str,
    }


def fetch_kuaishou(web_rid: str, cfg_all: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """快手直播间检测（SSR 解析，匿名 scraping 尽力而为）。

    从 ``cfg_all.platforms.kuaishou.credentials`` 构建 KuaishouAdapter（无凭证则匿名）。
    映射 RoomModel → 统一 result dict；异常兜底为 error（与 bili/douyin 一致，不中断整轮）。

    Args:
        web_rid: 快手用户 ID（live.kuaishou.com/u/<id> 的 <id>）。
        cfg_all: 完整 BLIVE_CONFIG dict（用于读取 kuaishou 凭证；可空）。

    Returns:
        直播间状态字典 {status, title, online, area, avatar, time}。
    """
    now_str = bjnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        creds: Dict[str, Any] = {}
        if cfg_all:
            creds = (cfg_all.get("platforms") or {}).get("kuaishou") or {}
            creds = creds.get("credentials") or {}
        from backend.adapters.kuaishou import KuaishouAdapter
        adapter = KuaishouAdapter(credentials=creds or None)
        m = adapter.fetch_room_status(web_rid)
        return {
            "status": "live" if m.live_status else "offline",
            "title": m.title or "",
            "online": int(m.online or 0),
            "area": "",
            "avatar": m.avatar or "",  # 主播真实头像（SSR author.avatar 解析）
            "nickname": m.name or "",  # 主播真实昵称（SSR author.name 解析）
            "time": now_str,
        }
    except Exception as e:
        logger.warning("快手直播检测失败 (%s): %s", web_rid, e)
        return {
            "status": "error",
            "title": f"获取失败: {str(e)}",
            "online": 0,
            "area": "",
            "avatar": "",
            "time": now_str,
        }


def fetch_douyu(rid: str) -> Dict[str, Any]:
    """斗鱼直播间检测（开放平台免鉴权公开接口，匿名可用、无需 Cookie）。

    数据源：``https://open.douyucdn.cn/api/RoomApi/room/{rid}``
    映射 RoomModel → 统一 result dict（与 bili/douyin/kuaishou 形状一致）。

    与快手一致：异常兜底为 ``status="error"`` 而非静默 offline——斗鱼接口对
    「房间不存在 / 房间号非法」会显式报错（error=101 / HTTP 404），静默当 offline
    会把失效房间伪装成「未开播」，既不污染历史也便于在看板上暴露。

    Args:
        rid: 斗鱼房间号（纯数字，如 ``15000``）。

    Returns:
        直播间状态字典 {status, title, online, area, avatar, nickname, time}。
    """
    now_str = bjnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        from backend.adapters.douyu import DouyuAdapter
        m = DouyuAdapter().fetch_room_status(rid)
        return {
            "status": "live" if m.live_status else "offline",
            "title": m.title or "",
            "online": int(m.online or 0),
            "area": m.area or "",
            "avatar": m.avatar or "",      # 主播头像（落 status.json，前端展示）
            "nickname": m.name or "",      # 主播昵称（用于刷新 rooms.json 显示名）
            "time": now_str,
        }
    except Exception as e:
        logger.warning("斗鱼直播检测失败 (%s): %s", rid, e)
        return {
            "status": "error",
            "title": f"获取失败: {str(e)}",
            "online": 0,
            "area": "",
            "avatar": "",
            "nickname": "",
            "time": now_str,
        }


# ==================== 工具函数 ====================

def _as_int(v: Any) -> int:
    """把可能是字符串/数字的值安全转 int。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

def should_push(prev_status: Optional[str], curr_status: str) -> bool:
    """判断是否需要推送通知

    去重的核心防线是 notify_dedup（2h 冷却/永久去重），state.json 的状态转换只做粗筛。
    因此 error/unknown 等「非正常」前态不再硬挡——dedup 会吸收闪烁/状态丢失造成的重复。

    Args:
        prev_status: 之前的状态
        curr_status: 当前状态

    Returns:
        是否需要推送
    """
    if curr_status == "offline" or curr_status == "error":
        return False
    if curr_status not in ("live", "replay"):
        return False
    if prev_status is None:
        return True
    # 确定性「已在播/已回放」→ 不重复推
    if prev_status in ("live", "replay"):
        # replay→live 视为升级，仍需推送
        return prev_status == "replay" and curr_status == "live"
    # offline / error / unknown 或其他异常前态 → 允许推送，由 dedup 账本拦截重复
    return True


def bili_status_on_batch_failure(prev_status: Optional[str]) -> str:
    """B站批量接口整体失败时，沿用上次已知状态；首次检测则记为 unknown。

    避免把整批房间误标为 error（既污染历史，又因 error→live 不推送而漏报恢复开播）。
    """
    return prev_status or "unknown"


def format_push_title(name: str, result: Dict[str, Any]) -> str:
    """格式化推送标题"""
    if result["status"] == "live":
        return f"🔴 {name} 开播了！"
    return f"▶️ {name} 轮播/回放中"


def format_push_desp(
    name: str, platform: str, rid: str, result: Dict[str, Any]
) -> str:
    """格式化推送内容"""
    platform_label = {
        "bilibili": "B站",
        "douyin": "抖音",
        "kuaishou": "快手",
        "douyu": "斗鱼",
    }.get(platform, platform)
    if platform == "bilibili":
        live_url = f"https://live.bilibili.com/{rid}"
    elif platform == "douyin":
        live_url = f"https://live.douyin.com/{rid}"
    elif platform == "douyu":
        live_url = f"https://www.douyu.com/{rid}"
    else:  # kuaishou 等
        live_url = f"https://live.kuaishou.com/u/{rid}"
    now = bjnow().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"## 🎬 {name} 开播了！"
        if result["status"] == "live"
        else f"## ▶️ {name} 轮播/回放中",
        "",
        f"**平台**: {platform_label}",
        f"**标题**: {result.get('title', '-')}",
    ]

    if result.get("area"):
        lines.append(f"**分区**: {result['area']}")
    if result.get("online"):
        lines.append(f"**人气**: {result['online']}")

    lines.extend(
        [
            "",
            f"👉 [进入直播间]({live_url})",
            "",
            f"---",
            f"检测时间: {now}",
        ]
    )

    return "\n".join(lines)


def render_body(s: Dict[str, Any], event: str, cfg_all: Dict[str, Any]) -> str:
    """构建单房间推送正文（desp）。

    模板优先（A4）：若 ``cfg_all['templates'][event]`` 存在，用 ``common.render_template``
    渲染模板正文（占位符 ``{name}{title}{platform}{time}{url}``）；否则降级为
    ``format_push_desp``（legacy 路径，与改造前逐字节一致）。

    Args:
        s: newly_live 中的单房间记录（含 name/platform/rid/result/tags）。
        event: 事件类型（如 ``"live_on"``）。
        cfg_all: BLIVE_CONFIG 完整 dict（用于读取 templates）。

    Returns:
        渲染后的正文字符串。
    """
    tpl = (cfg_all.get("templates") or {}).get(event)
    if tpl:
        platform = s.get("platform", "bilibili")
        rid = s.get("rid", "")
        if platform == "bilibili":
            url = f"https://live.bilibili.com/{rid}"
        elif platform == "douyin":
            url = f"https://live.douyin.com/{rid}"
        elif platform == "douyu":
            url = f"https://www.douyu.com/{rid}"
        else:  # kuaishou 等
            url = f"https://live.kuaishou.com/u/{rid}"
        tpl_ctx = {
            "name": s["name"],
            "title": s["result"].get("title", ""),
            "platform": platform,
            "time": bjnow().strftime("%Y-%m-%d %H:%M:%S"),
            "url": url,
        }
        return common.render_template(tpl, tpl_ctx)
    return format_push_desp(s["name"], s["platform"], s["rid"], s["result"])


# ==================== 直播时长计算 ====================

def calculate_duration(start_str: str, now_dt: datetime) -> str:
    """计算直播时长

    Args:
        start_str: 开始时间字符串 "%Y-%m-%d %H:%M:%S"
        now_dt: 当前时间

    Returns:
        格式化的时长字符串，如 "1h30min" 或 "45min"
    """
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
        secs = int((now_dt - start_dt).total_seconds())
        if secs < 0:
            return ""
        h, m = divmod(secs, 3600)
        m, _ = divmod(m, 60)
        return f"{h}h{m}min" if h > 0 else f"{m}min"
    except (ValueError, TypeError):
        return ""


# ==================== 主逻辑 ====================

def main() -> None:
    """主函数"""
    cfg = load_config()
    rooms = cfg.get("rooms", [])
    push_cfg = cfg.get("push_cfg", {})
    # 完整配置：多通道路由 / 模板渲染（A2/A4）；legacy 单通道下等价于仅含 push 段
    cfg_all = cfg.get("cfg_all", {})
    # A3 静默（仅用于推送前拦截；不影响状态抓取）
    silence_cfg = cfg.get("silence", {})

    if not rooms:
        logger.info("没有配置监控房间")
        return

    # 加载之前的状态
    prev_state: Dict[str, str] = load_json_file(STATE_FILE, {})
    tracking: Dict[str, Dict[str, Any]] = load_json_file(TRACKING_FILE, {})

    # 加载上一次写入 status.json 的完整房间信息，用于在 B站批量接口整体失败时
    # 继承 title/online/area，避免看板在故障期间把房间信息清空。
    prev_status_full: Dict[str, Dict[str, Any]] = {}
    _prev_doc = load_json_file(STATUS_FILE, {})
    for _it in _prev_doc.get("rooms", []) or []:
        _pk = f"{_it.get('platform', 'bilibili')}_{_it.get('id', '')}"
        prev_status_full[_pk] = _it

    new_state: Dict[str, str] = {}
    status_list: List[Dict[str, Any]] = []
    log_entries: List[Dict[str, Any]] = []
    newly_live: List[Dict[str, Any]] = []

    now = bjnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    logger.info("开始检测 %d 个房间...", len(rooms))

    # Step 1: 批量查询所有 B站房间
    bili_rooms = [(r, i) for i, r in enumerate(rooms) if r.get("platform", "bilibili") == "bilibili"]
    bili_data: Dict[str, Dict[str, Any]] = {}

    bili_batch_failed = False
    if bili_rooms:
        try:
            bili_ids = [r["id"] for r, _ in bili_rooms]
            bili_data = fetch_bilibili_batch(bili_ids)
            # 真实头像：B站需 WBI 取 face（CI 无登录态可能风控失败→留空，前端回退首字母）。
            # 仅当房间含 uid 时尝试；测试 fixture 无 uid，不会触发真实网络请求。
            for r, _ in bili_rooms:
                _d = bili_data.get(str(r["id"]))
                if not _d:
                    continue
                _uid = _d.get("uid")
                if not _uid:
                    _d["avatar"] = ""
                    continue
                try:
                    _d["avatar"] = _fetch_bilibili_face(str(_uid))
                except Exception as _e:
                    logger.warning("B站头像获取失败 (%s): %s", _uid, _e)
                    _d["avatar"] = ""
            logger.info("B站批量查询成功，获取 %d 个房间数据", len(bili_data))
        except Exception as e:
            logger.error("B站批量查询失败: %s", e)
            bili_batch_failed = True

    # Step 2: 逐个检测所有房间
    # 推送前先从远端同步去重账本：并发 run 时（concurrency 偶发不排队），
    # 后启动的 run 能看到先完成 run 已 record 的去重条目，避免重复推送。
    synced = dedup_sync_from_remote()
    if synced:
        logger.info("去重账本已从远端同步 %d 条较新记录", synced)

    for room in rooms:
        platform = room.get("platform", "bilibili")
        rid = room.get("id", "")
        name = room.get("name", f"{platform}-{rid}")
        key = f"{platform}_{rid}"
        # B3 批量启停：enabled===false 的房间完全跳过检测（看板保留上次状态）
        if not room_enabled(room):
            logger.info("[%s] 已暂停（enabled=false），跳过检测", name)
            continue
        push_result: Optional[str] = None

        # 获取当前状态
        try:
            if platform == "bilibili":
                d = bili_data.get(str(rid))
                if not d:
                    if bili_batch_failed:
                        # 批量接口整体失败：沿用上次已知状态，不误标为 error
                        # （避免污染历史；且 error→live 不推送会漏报恢复开播）
                        prev = prev_state.get(key)
                        prev_full = prev_status_full.get(key, {})
                        logger.warning("[%s] B站批量查询失败，沿用上次状态: %s", name, prev)
                        result = {
                            "status": bili_status_on_batch_failure(prev),
                            "title": prev_full.get("title", ""),
                            "online": prev_full.get("online", 0),
                            "area": prev_full.get("area", ""),
                        }
                    else:
                        raise Exception(f"批量接口未返回房间 {rid} 的数据")
                else:
                    status_code = d.get("live_status", 0)
                    result = {
                        "status": BILIBILI_STATUS_MAP.get(status_code, "unknown"),
                        "title": d.get("title", ""),
                        "online": d.get("online", 0),
                        "area": (
                            f"{d.get('parent_area_name', '')}·{d.get('area_name', '')}".strip("·")
                            or ""
                        ),
                        "avatar": d.get("avatar", ""),
                        "uname": _bili_uname(d),
                    }
            elif platform == "douyin":
                result = fetch_douyin(rid)
            elif platform == "kuaishou":
                # 快手降频/冷却控制（通过环境变量配置，默认不限制）：
                # - KUAISHOU_COOLDOWN_UNTIL: ISO 时间戳，在此之前完全跳过快手请求
                # - KUAISHOU_LIVE_INTERVAL_MIN: 直播检查间隔分钟数（默认 0=每轮检查）
                import os as _os
                from datetime import datetime as _dt, timezone as _tz
                _now_utc = _dt.now(_tz.utc)
                _cooldown = _os.environ.get("KUAISHOU_COOLDOWN_UNTIL", "").strip()
                _interval = int(_os.environ.get("KUAISHOU_LIVE_INTERVAL_MIN", "0") or "0")
                _skip = False
                if _cooldown:
                    try:
                        _until = _dt.fromisoformat(_cooldown.replace("Z", "+00:00"))
                        if _now_utc < _until:
                            _skip = True
                    except Exception:
                        pass
                if not _skip and _interval > 0:
                    _skip = (_now_utc.minute % _interval) > 4
                # 检查出口 IP 是否在已知封锁段（KUAISHOU_BLOCKED_IPS 环境变量，逗号分隔）
                if not _skip:
                    try:
                        import urllib.request as _ur
                        _blocked = [p.strip() for p in _os.environ.get("KUAISHOU_BLOCKED_IPS", "13.105.117.").split(",") if p.strip()]
                        _ip = _ur.urlopen("https://api.ipify.org", timeout=5).read().decode().strip()
                        if any(_ip.startswith(p) for p in _blocked):
                            _skip = True
                            logger.info("  [%s] 出口 IP %s 在快手封锁段，跳过直播检查", name, _ip)
                    except Exception:
                        pass
                if _skip:
                    _prev_ks = prev_status_full.get(key, {})
                    if _prev_ks:
                        logger.info("  [%s] 快手降频窗口外，沿用上次状态: %s", name, _prev_ks.get("status"))
                        result = {
                            "status": _prev_ks.get("status", "unknown"),
                            "title": _prev_ks.get("title", ""),
                            "online": _prev_ks.get("online", 0),
                            "area": _prev_ks.get("area", ""),
                            "nickname": _prev_ks.get("nickname", ""),
                            "time": now_str,
                        }
                    else:
                        result = {"status": "unknown", "title": "", "online": 0, "time": now_str}
                else:
                    result = fetch_kuaishou(rid, cfg_all)
            elif platform == "douyu":
                # 斗鱼走开放平台公开接口（匿名、无风控），无需降频/冷却控制
                result = fetch_douyu(rid)
            else:
                logger.warning("[%s] 未知平台，跳过检测: %s", name, platform)
                result = {"status": "offline", "title": "", "online": 0, "time": now_str}

        except Exception as e:
            logger.warning("[%s] 检测失败: %s", name, e)
            result = {
                "status": "error",
                "title": str(e),
                "online": 0,
                "area": "",
                "time": now_str,
            }
            push_result = "error"

        # 显示名称处理
        display_name = name
        if platform == "douyin" and result.get("nickname") and result["nickname"] != name:
            display_name = result["nickname"]
        elif platform == "bilibili" and result.get("uname") and result["uname"] != name:
            display_name = result["uname"]
        elif platform == "kuaishou" and result.get("nickname") and result["nickname"] != name:
            display_name = result["nickname"]
        elif platform == "douyu" and result.get("nickname") and result["nickname"] != name:
            display_name = result["nickname"]

        logger.info(
            "  [%s] %s - %s",
            display_name,
            result["status"],
            result.get("title", "")[:30],
        )

        # 更新状态
        new_state[key] = result["status"]

        # 开播追踪
        t = tracking.get(key, {})
        last_live = t.get("last_live", "")
        live_start_str = t.get("live_start", "")
        live_duration = ""

        if result["status"] == "live":
            if not live_start_str:
                live_start_str = now_str
            else:
                live_duration = calculate_duration(live_start_str, now)
        elif live_start_str:
            # 刚下播，记录上次直播信息
            last_live = live_start_str
            t["last_duration"] = calculate_duration(live_start_str, now)
            live_start_str = ""

        t["last_live"] = last_live
        t["live_start"] = live_start_str
        if live_duration:
            t["live_duration"] = live_duration
        if platform == "douyin" and result.get("sec_uid"):
            t["sec_uid"] = result["sec_uid"]
        tracking[key] = t

        # 构建状态列表项
        status_item = {
            "platform": platform,
            "id": rid,
            "name": display_name,
            "status": result["status"],
            "title": result.get("title", ""),
            "online": result.get("online", 0),
            "area": result.get("area", ""),
            "time": result.get("time", now_str),
            "sec_uid": result.get("sec_uid", ""),
            "avatar": result.get("avatar", ""),
            "last_live": last_live,
            "live_duration": live_duration,
        }
        status_list.append(status_item)

        # 状态变化检测
        prev_status = prev_state.get(key)
        changed = prev_status is not None and prev_status != result["status"]

        if should_push(prev_status, result["status"]):
            dkey = f"live:{key}"
            if dedup_should_notify(dkey):
                newly_live.append(
                    {
                        "name": display_name,
                        "platform": platform,
                        "rid": rid,
                        "result": result,
                        # 携带房间主 tag（A2 路由维度；无则 None，与 resolve_channel 标量匹配一致）
                        "tags": room.get("tags"),
                    }
                )
                push_result = "queued"
            else:
                # 冷却期内已推送过（闪烁 / 状态文件短暂丢失后的重复首检），跳过
                logger.info(
                    "[%s] 去重跳过：开播通知 %s 在冷却期内已发送", display_name, dkey
                )
                push_result = "deduped"

        # 记录日志
        entry_type = type_from_status(result["status"])
        log_entries.append(
            {
                "time": now_str,
                "name": display_name,
                "platform": platform,
                "status": result["status"],
                "title": result.get("title", ""),
                "changed": changed,
                "prev": prev_status if changed else None,
                "push": push_result,
                # 新增 rid：取值为房间 id，用于级联清理时精确匹配孤儿（向后兼容：前端忽略未知字段）
                "rid": rid,
                # 统一日志模型（日志模块功能性重写）：type 区分事件类型、level 由 type 推导、
                # account == rid 供按账号视图聚合。
                "type": entry_type,
                "level": level_from_type(entry_type),
                "detail": "",
                "account": rid,
            }
        )

    # Step 3: 合并推送（多通道分组投递：按通道路由聚合；同通道房间合为一条消息）
    # A3 静默时段：推送前拦截（仅跳过推送，不影响状态抓取与看板）——位置不变
    if should_skip_by_silence(now, silence_cfg):
        logger.info("当前处于静默时段，暂缓推送（状态已正常抓取，看板不受影响）")
        for le in log_entries:
            if le["push"] == "queued":
                le["push"] = "silenced"
    elif newly_live:
        try:
            # ---- 分组：按路由选通道；同通道房间归一组（路由确定性：每房间单通道）----
            groups: Dict[str, Dict[str, Any]] = {}
            # 预建 queued 日志条目索引（platform_rid -> [log_entry]），按组结果精准标记
            queued_index: Dict[str, List[Dict[str, Any]]] = {}
            for le in log_entries:
                if le.get("push") == "queued":
                    queued_index.setdefault(
                        f"{le.get('platform')}_{le.get('rid')}", []
                    ).append(le)

            for s in newly_live:
                room_ctx = {
                    "platform": s.get("platform", "bilibili"),
                    "tag": (s.get("tags") or [None])[0] if s.get("tags") else None,
                    "event": "live_on",
                }
                ch = common.resolve_channel(cfg_all, room_ctx)
                gid = ch.get("id") or "default"
                g = groups.setdefault(gid, {"rooms": [], "ctx": room_ctx, "pcfg": None})
                g["rooms"].append(s)
                if g["pcfg"] is None:
                    g["pcfg"] = channel_to_push_cfg(ch)

            for gid, g in groups.items():
                group_rooms = g["rooms"]
                group_ctx = g["ctx"]
                pcfg = g["pcfg"]

                # 未配置通道：等价 legacy 无 push 分支（info 跳过 + no_sendkey，不刷 error）
                if not pcfg or not pcfg.get("type"):
                    logger.info(
                        "%d 个房间状态变化，但未配置推送渠道", len(group_rooms)
                    )
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "no_sendkey"
                    continue

                # ---- 构建分组 title / desp（模板正文可选，legacy 路径逐字节一致）----
                if len(group_rooms) == 1:
                    s = group_rooms[0]
                    title = format_push_title(s["name"], s["result"])
                    desp = render_body(s, "live_on", cfg_all)
                else:
                    names = "、".join(s["name"] for s in group_rooms)
                    title = f"🔴 {len(group_rooms)}位主播开播：{names}"
                    desp = "\n\n---\n\n".join(
                        render_body(s, "live_on", cfg_all) for s in group_rooms
                    )

                res = dispatch_event(cfg_all, group_ctx, title, desp)
                channel = (pcfg.get("type") or "unknown").lower()
                logger.info("推送%s: %s", "成功" if res.ok else "失败", title)

                if res.ok:
                    # 推送成功后才记录去重（失败则不标记，下一轮可补推；房间级去重不变）
                    for s in group_rooms:
                        dedup_record(f"live:{s['platform']}_{s['rid']}")
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "pushed_ok"
                elif res.last_error == "config: empty push_cfg":
                    # 兜底（分组短路已覆盖）：仍标记未配置，不刷 error
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "no_sendkey"
                else:
                    # 失败：写 error 级统一日志事件（含渠道+原因），下一轮 CI 可补推。
                    # runtime.log 经 logger.error 始终落盘（不受 30min 节流）；
                    # 注入 log_entries 的 error 事件会经 dedupe_by_throttle 落 history.json（受节流防刷屏）。
                    primary = group_rooms[0]
                    last_err = (res.last_error or "未知错误")[:200]
                    logger.error(
                        "通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                        channel, res.attempts, res.last_error, title,
                    )
                    log_entries.append(
                        {
                            "time": now_str,
                            "name": primary["name"],
                            "platform": primary["platform"],
                            "status": "error",
                            "title": title,
                            "changed": False,
                            "prev": None,
                            "push": "pushed_fail",
                            "rid": primary["rid"],
                            "type": "error",
                            "level": "error",
                            "detail": f"通知发送失败（{channel}）：{last_err}",
                            "account": primary["rid"],
                        }
                    )
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "pushed_fail"
                        # 关键修复：推送失败时回退 state 为上次已知状态，让下一轮 CI 重新检测到
                        # 「offline→live」等状态变化从而补推；否则 new_state[key]=live 会阻断
                        # should_push 的状态变化判断，失败的推送永远不会重试。
                        sk = f"{s['platform']}_{s['rid']}"
                        prev_val = prev_state.get(sk)
                        if prev_val is not None:
                            new_state[sk] = prev_val
                        else:
                            new_state.pop(sk, None)
        except Exception as e:
            logger.error("推送异常: %s", e)
            for le in log_entries:
                if le["push"] == "queued":
                    le["push"] = "push_error"
            # 异常兜底：所有 queued 房间同样回退 state
            for le in log_entries:
                if le.get("push") == "push_error":
                    sk = f"{le.get('platform', 'bilibili')}_{le.get('rid', '')}"
                    prev_val = prev_state.get(sk)
                    if prev_val is not None:
                        new_state[sk] = prev_val
                    else:
                        new_state.pop(sk, None)

    # 保存状态文件
    save_json_file(STATE_FILE, new_state)
    save_json_file(
        STATUS_FILE,
        {"updated": now_str, "rooms": status_list},
    )
    save_json_file(TRACKING_FILE, tracking)

    # 固化阶段：级联清理 history 孤儿（重读磁盘 rooms.json 构造 active_keys，避免与前端增删竞态）
    # 重新从磁盘读取最新 rooms.json（本轮期间前端可能已增删房间），绝不用启动内存副本
    rooms_now = load_json_file(ROOMS_FILE, []) or []
    active_keys = {
        f"{r.get('platform', 'bilibili')}|{r.get('id', '')}"
        for r in rooms_now
        if r.get("id")
    }

    # 错误类（type=error）事件经节流去重：同 rid+type 30min 内不重复写，防刷屏
    log_entries = dedupe_by_throttle(log_entries, now, history_path=HISTORY_FILE)

    # 追加本轮条目（每条已带 rid/type），保留最近 HISTORY_MAX 条（统一上限，单一来源）
    append_history(HISTORY_FILE, log_entries, HISTORY_MAX)

    # 读取刚写入的 history，按 active_keys 裁掉已删房间的孤儿记录，再原子写回
    history = load_history(HISTORY_FILE)
    history = state_prune.prune_history_orphans(history, active_keys)
    save_json_file(HISTORY_FILE, history)

    # 固化阶段：级联清理 live tracking.json 孤儿（基于当前磁盘 rooms.json 的 platform_rid 活钥）。
    # 与 history 孤儿同理，防止前端删除房间后残留 live 跟踪状态（重加即带旧基线）。
    live_active_keys = {
        f"{r.get('platform', 'bilibili')}_{r.get('id', '')}"
        for r in rooms_now
        if r.get("id")
    }
    tracking = state_prune.prune_tracking_orphans(tracking, live_active_keys)
    save_json_file(TRACKING_FILE, tracking)

    # 固化阶段：清理孤儿封面（无对应房间的新作品/直播封面）。
    # 覆盖命名 {platform}_{id}.jpg，活房间集合 = rooms.json ∪ post_rooms.json。
    # 删除自身由 check.yml 的 `git add -A assets/covers` 纳入提交（区别于 git add -f 不会 stage 删除）。
    try:
        covers_dir = os.path.join(REPO_DIR, "assets", "covers")
        post_rooms_now = load_json_file(os.path.join(REPO_DIR, "post_rooms.json"), []) or []
        orphan_covers = state_prune.find_orphan_covers(rooms_now, post_rooms_now, covers_dir)
        for _oc in orphan_covers:
            try:
                os.remove(_oc)
                logger.info("已删除孤儿封面: %s", _oc)
            except OSError as e:
                logger.warning("删除孤儿封面失败 %s: %s", _oc, e)
        if orphan_covers:
            logger.info("清理孤儿封面 %d 个", len(orphan_covers))
    except Exception as e:
        logger.warning("清理孤儿封面失败（不影响主流程）: %s", e)

    # 裁剪去重账本（丢弃过期 live: key，post: key 永久保留）
    try:
        dedup_prune()
    except Exception as e:
        logger.warning("裁剪去重账本失败（不影响主流程）: %s", e)

    logger.info("检测完成，状态已更新")


def _event_content_hash(channel_id: str, event_type: str, content: str) -> str:
    """推送内容指纹（委托到 common.content_hash，保持调用点不变）。"""
    return content_hash(channel_id, event_type, content)


def _room_model_to_result(model, now_str: str) -> Dict[str, Any]:
    """把适配器归一化 ``RoomModel`` 映射回 ``run_live_check`` 内部使用的 result dict。

    live_status 为 BOOL，经 ``extra['status_str']``（若存在，如 bilibili 的
    live/offline/replay）映射为字符串；否则退化为 "live"/"offline"。其余字段逐一对齐。
    """
    status_str = model.extra.get("status_str")
    if not status_str:
        status_str = "live" if model.live_status else "offline"
    return {
        "status": status_str,
        "title": model.title,
        "online": model.online,
        "area": model.area,
        "sec_uid": model.extra.get("sec_uid", ""),
        "nickname": model.name,
        "time": now_str,
        "url": model.url,
        "cover": model.cover,
        "avatar": model.extra.get("avatar", "") or model.avatar,
    }


def run_live_check(*, cfg_all: Dict[str, Any], persist: Any, now: Optional[datetime] = None,
                   adapters: Any = None) -> None:
    """后端驱动的直播检测编排（不写 JSON，全部经 ``persist`` 落库）。

    复用本模块纯函数（fetch_bilibili_batch / fetch_douyin / should_push /
    format_push_* / render_body / calculate_duration）与 common / push_utils 的
    路由/模板/推送逻辑（一字不改）；编排逻辑与原 ``main()`` 等价，但「写 state.json /
    status.json / history.json」改为调用 ``persist`` 回调：

        persist.list_rooms()                         -> [{platform,external_id,name,enabled,tags,meta}]
        persist.get_prev_status(platform, rid)       -> str|None（上一轮 live_status）
        persist.get_tracking(platform, rid)          -> dict（meta 基线）
        persist.set_room_status(..., status_item, meta_update) -> 写 Room 状态列+基线
        persist.append_event(entry)                  -> 写 events_history（返回是否写入）
        persist.dedup_should_notify(key, cooldown)   -> bool
        persist.dedup_record(key)                    -> 标记去重（仅推送成功后）
        persist.notify_log(channel_id, event_type, content_hash, status) -> 写通知账本

    Args:
        cfg_all: BLIVE_CONFIG 完整 dict（参与多通道路由/模板/静默判定）。
        persist: 后端持久化门面（见 backend/jobs/live_check.LivePersist）。
        now: 可注入当前北京时间（测试用）；缺省 ``bjnow()``。
    """
    if now is None:
        now = bjnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    silence_cfg = (cfg_all or {}).get("silence") or {}

    rooms = persist.list_rooms() or []
    if not rooms:
        logger.info("[run_live_check] 没有配置监控房间")
        return

    # 适配器注册表（阶段三 T03）：缺省从 cfg_all['platforms'] + 内置 bilibili/douyin 构建。
    # 编排层由此「遍历注册表」而非按平台 if/else 内联分支。
    if adapters is None:
        from backend.adapters import AdapterRegistry

        adapters = AdapterRegistry.from_config(cfg_all or {})

    # 上一轮状态 & 基线（本轮更新前读取）
    prev_state = {
        f"{r['platform']}_{r['external_id']}": persist.get_prev_status(r["platform"], r["external_id"])
        for r in rooms
    }
    tracking = {
        f"{r['platform']}_{r['external_id']}": dict(persist.get_tracking(r["platform"], r["external_id"]) or {})
        for r in rooms
    }

    # Step 1: B站批量预取（经 BilibiliAdapter.fetch_room_status_batch，保留批量效率；
    # 内置 pure function fetch_bilibili_batch 仍被适配器内部调用，既有 monkeypatch 测试继续生效）。
    bili_adapter = adapters.get("bilibili") if adapters else None
    bili_cache: Dict[str, Any] = {}
    bili_batch_failed = False
    bili_rooms = [(r, i) for i, r in enumerate(rooms) if r.get("platform", "bilibili") == "bilibili"]
    if bili_rooms and bili_adapter is not None and hasattr(bili_adapter, "fetch_room_status_batch"):
        try:
            bili_ids = [str(r["external_id"]) for r, _ in bili_rooms]
            bili_cache = bili_adapter.fetch_room_status_batch(bili_ids)
            logger.info("B站批量查询成功，获取 %d 个房间数据", len(bili_cache))
        except Exception as e:  # noqa: BLE001
            logger.error("B站批量查询失败: %s", e)
            bili_batch_failed = True

    log_entries: List[Dict[str, Any]] = []
    newly_live: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    for room in rooms:
        platform = room.get("platform", "bilibili")
        rid = str(room.get("external_id", ""))
        name = room.get("name", f"{platform}-{rid}")
        key = f"{platform}_{rid}"
        if not room_enabled(room):
            logger.info("[%s] 已暂停（enabled=false），跳过检测", name)
            continue
        push_result: Optional[str] = None

        try:
            adapter = adapters.get(platform) if adapters else None
            if adapter is None or not getattr(adapter, "supports_live", True):
                logger.warning("[%s] 未知/不支持直播的平台，跳过检测: %s", name, platform)
                result = {"status": "offline", "title": "", "online": 0, "time": now_str}
            elif platform == "bilibili":
                # 命中批量缓存；缺失处理沿用既有「沿用上次状态 / 报错」语义
                model = bili_cache.get(str(rid))
                if model is None:
                    if bili_batch_failed:
                        prev = prev_state.get(key)
                        logger.warning("[%s] B站批量查询失败，沿用上次状态: %s", name, prev)
                        result = {
                            "status": bili_status_on_batch_failure(prev),
                            "title": "",
                            "online": 0,
                            "area": "",
                        }
                    else:
                        raise Exception(f"批量接口未返回房间 {rid} 的数据")
                else:
                    result = _room_model_to_result(model, now_str)
            else:
                model = adapter.fetch_room_status(rid)
                result = _room_model_to_result(model, now_str)

        except Exception as e:
            logger.warning("[%s] 检测失败: %s", name, e)
            result = {
                "status": "error",
                "title": str(e),
                "online": 0,
                "area": "",
                "time": now_str,
            }
            push_result = "error"

        display_name = name
        if platform == "douyin" and result.get("nickname") and result["nickname"] != name:
            display_name = result["nickname"]

        logger.info("  [%s] %s - %s", display_name, result["status"], result.get("title", "")[:30])

        # 开播追踪基线
        t = tracking.get(key, {})
        last_live = t.get("last_live", "")
        live_start_str = t.get("live_start", "")
        live_duration = ""
        if result["status"] == "live":
            if not live_start_str:
                live_start_str = now_str
            else:
                live_duration = calculate_duration(live_start_str, now)
        elif live_start_str:
            last_live = live_start_str
            t["last_duration"] = calculate_duration(live_start_str, now)
            live_start_str = ""
        t["last_live"] = last_live
        t["live_start"] = live_start_str
        if live_duration:
            t["live_duration"] = live_duration
        if platform == "douyin" and result.get("sec_uid"):
            t["sec_uid"] = result["sec_uid"]
        tracking[key] = t

        status_item = {
            "platform": platform,
            "id": rid,
            "name": display_name,
            "status": result["status"],
            "title": result.get("title", ""),
            "online": result.get("online", 0),
            "area": result.get("area", ""),
            "time": result.get("time", now_str),
            "sec_uid": result.get("sec_uid", ""),
            "avatar": result.get("avatar", ""),
            "last_live": last_live,
            "live_duration": live_duration,
        }

        prev_status = prev_state.get(key)
        changed = prev_status is not None and prev_status != result["status"]

        if should_push(prev_status, result["status"]):
            dkey = f"live:{key}"
            if persist.dedup_should_notify(dkey):
                newly_live.append({
                    "name": display_name,
                    "platform": platform,
                    "rid": rid,
                    "result": result,
                    "tags": room.get("tags"),
                })
                push_result = "queued"
            else:
                logger.info("[%s] 去重跳过：开播通知 %s 在冷却期内已发送", display_name, dkey)
                push_result = "deduped"

        entry_type = type_from_status(result["status"])
        entry = {
            "time": now_str,
            "name": display_name,
            "platform": platform,
            "status": result["status"],
            "title": result.get("title", ""),
            "changed": changed,
            "prev": prev_status if changed else None,
            "push": push_result,
            "rid": rid,
            "type": entry_type,
            "level": level_from_type(entry_type),
            "detail": "",
            "account": rid,
        }
        log_entries.append(entry)
        rows.append({
            "platform": platform,
            "rid": rid,
            "name": display_name,
            "result": result,
            "status_item": status_item,
            "meta": t,
            "entry": entry,
        })

    # Step 3: 合并推送（多通道分组投递）
    if should_skip_by_silence(now, silence_cfg):
        logger.info("当前处于静默时段，暂缓推送（状态已正常抓取，看板不受影响）")
        for le in log_entries:
            if le["push"] == "queued":
                le["push"] = "silenced"
    elif newly_live:
        try:
            groups: Dict[str, Dict[str, Any]] = {}
            queued_index: Dict[str, List[Dict[str, Any]]] = {}
            for le in log_entries:
                if le.get("push") == "queued":
                    queued_index.setdefault(f"{le.get('platform')}_{le.get('rid')}", []).append(le)

            for s in newly_live:
                room_ctx = {
                    "platform": s.get("platform", "bilibili"),
                    "tag": (s.get("tags") or [None])[0] if s.get("tags") else None,
                    "event": "live_on",
                }
                ch = common.resolve_channel(cfg_all or {}, room_ctx)
                gid = ch.get("id") or "default"
                g = groups.setdefault(gid, {"rooms": [], "ctx": room_ctx, "pcfg": None})
                g["rooms"].append(s)
                if g["pcfg"] is None:
                    g["pcfg"] = channel_to_push_cfg(ch)

            for gid, g in groups.items():
                group_rooms = g["rooms"]
                group_ctx = g["ctx"]
                pcfg = g["pcfg"]
                if not pcfg or not pcfg.get("type"):
                    logger.info("%d 个房间状态变化，但未配置推送渠道", len(group_rooms))
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "no_sendkey"
                    continue

                if len(group_rooms) == 1:
                    s = group_rooms[0]
                    title = format_push_title(s["name"], s["result"])
                    desp = render_body(s, "live_on", cfg_all or {})
                else:
                    names = "、".join(s["name"] for s in group_rooms)
                    title = f"🔴 {len(group_rooms)}位主播开播：{names}"
                    desp = "\n\n---\n\n".join(render_body(s, "live_on", cfg_all or {}) for s in group_rooms)

                res = dispatch_event(cfg_all or {}, group_ctx, title, desp)
                channel = (pcfg.get("type") or "unknown").lower()
                logger.info("推送%s: %s", "成功" if res.ok else "失败", title)
                content_hash = _event_content_hash(channel, "live_on", title + desp)

                if res.ok:
                    for s in group_rooms:
                        persist.dedup_record(f"live:{s['platform']}_{s['rid']}")
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "pushed_ok"
                    persist.notify_log(channel_id=channel, event_type="live_on",
                                       content_hash=content_hash, status="ok")
                elif res.last_error == "config: empty push_cfg":
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "no_sendkey"
                else:
                    primary = group_rooms[0]
                    last_err = (res.last_error or "未知错误")[:200]
                    logger.error("通知推送失败 channel=%s attempts=%d last_error=%s: %s",
                                 channel, res.attempts, res.last_error, title)
                    log_entries.append({
                        "time": now_str,
                        "name": primary["name"],
                        "platform": primary["platform"],
                        "status": "error",
                        "title": title,
                        "changed": False,
                        "prev": None,
                        "push": "pushed_fail",
                        "rid": primary["rid"],
                        "type": "error",
                        "level": "error",
                        "detail": f"通知发送失败（{channel}）：{last_err}",
                        "account": primary["rid"],
                    })
                    persist.notify_log(channel_id=channel, event_type="live_on",
                                       content_hash=content_hash, status="fail")
                    for s in group_rooms:
                        for le in queued_index.get(f"{s['platform']}_{s['rid']}", []):
                            le["push"] = "pushed_fail"
        except Exception as e:
            logger.error("推送异常: %s", e)
            for le in log_entries:
                if le["push"] == "queued":
                    le["push"] = "push_error"

    # 落库：状态列 + 基线 + 事件
    for row in rows:
        persist.set_room_status(
            platform=row["platform"],
            external_id=row["rid"],
            kind="live",
            name=row["name"],
            result=row["result"],
            meta_update=row["meta"],
            now_str=now_str,
            status_item=row["status_item"],
        )
    for entry in log_entries:
        persist.append_event(entry)

    logger.info("[run_live_check] 检测完成")


if __name__ == "__main__":
    main()
