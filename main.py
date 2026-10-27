# -*- coding: utf-8 -*-
"""B站弹幕点歌机器人（独立项目，不修改视频点播机器人）

依赖：Python 3.10+、bilibili-api-python==17.4.2、requests
点歌指令：点歌 歌名；点歌 歌名 歌手
管理员切歌：切歌（仅 uid.txt 中的 UID）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import webbrowser
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tkinter as tk
from tkinter import ttk

try:
    import qrcode
    from PIL import ImageTk
except ImportError:
    qrcode = None
    ImageTk = None
from collections import deque
from pathlib import Path
from typing import Any, Optional

import requests
from bilibili_api import live
from bilibili_api import Credential

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ROOM_CONFIG = BASE_DIR / "room_config.txt"
COOKIE_FILE = BASE_DIR / "bili_cookie.txt"
UID_FILE = BASE_DIR / "uid.txt"
LOG_FILE = BASE_DIR / "songbot.log"
DEFAULT_LX_API = "http://127.0.0.1:23330"
DEFAULT_ROOM_ID = "请填写直播间ID"
DEFAULT_REQUIRE_FANS_MEDAL = "0"  # 0=必须有本房间点亮粉丝牌；1=不要求粉丝牌
DEFAULT_COOLDOWN = 10
DEFAULT_QUEUE_LIMIT = 100
PANEL_HOST = "127.0.0.1"
PANEL_PORT = 8766
PANEL_HTML_FILE = BASE_DIR / "queue_panel.html"
DISPLAY_HTML_FILE = BASE_DIR / "display_panel.html"
main_async_loop: Optional[asyncio.AbstractEventLoop] = None
bot_instance: Optional["SongBot"] = None
panel_server: Optional[ThreadingHTTPServer] = None
shutdown_async_event: Optional[asyncio.Event] = None
MEDAL_CACHE_SECONDS = 60
HTTP_TIMEOUT = 8

def disable_windows_console_quick_edit() -> None:
    """关闭 Windows 控制台 QuickEdit，避免鼠标选中文字时暂停控制台输出。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_std_handle.argtypes = [wintypes.DWORD]
        get_std_handle.restype = wintypes.HANDLE
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_console_mode.restype = wintypes.BOOL
        set_console_mode = kernel32.SetConsoleMode
        set_console_mode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        set_console_mode.restype = wintypes.BOOL

        # STD_INPUT_HANDLE = -10；QuickEdit=0x0040，ExtendedFlags=0x0080
        handle = get_std_handle(0xFFFFFFF6)
        mode = wintypes.DWORD()
        if handle and get_console_mode(handle, ctypes.byref(mode)):
            new_mode = (mode.value | 0x0080) & ~0x0040
            if not set_console_mode(handle, new_mode):
                # 不影响主程序，只记录在后续日志中无法保证关闭成功。
                pass
    except Exception:
        # 某些 IDE/打包环境没有真实控制台，忽略即可。
        pass


disable_windows_console_quick_edit()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("BiliLXSongBot")


def read_kv_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip().lower()] = value.strip()
    except Exception:
        log.exception("读取配置文件失败：%s", path.name)
    return result


def ensure_config_files() -> None:
    if not ROOM_CONFIG.exists():
        ROOM_CONFIG.write_text(
            "# B站弹幕点歌机器人配置（UTF-8）\n"
            f"room_id={DEFAULT_ROOM_ID}\n"
            f"lx_api={DEFAULT_LX_API}\n"
            f"require_fans_medal={DEFAULT_REQUIRE_FANS_MEDAL}\n"
            f"cooldown_seconds={DEFAULT_COOLDOWN}\n"
            f"queue_limit={DEFAULT_QUEUE_LIMIT}\n",
            encoding="utf-8",
        )
        log.warning("已创建 room_config.txt，请填写 room_id 后重新启动")
    if not UID_FILE.exists():
        UID_FILE.write_text("# 每行填写一个管理员的B站UID\n", encoding="utf-8")
        log.info("已创建 uid.txt，请按需填写管理员 UID")
    if not COOKIE_FILE.exists():
        COOKIE_FILE.write_text("# 粘贴B站登录Cookie，格式示例：SESSDATA=...; bili_jct=...; buvid3=...\n", encoding="utf-8")
        log.warning("已创建 bili_cookie.txt；粉丝牌校验需要有效的 B站登录 Cookie")


def parse_int(config: dict[str, str], key: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(config.get(key, str(default))))
    except (TypeError, ValueError):
        return default


ensure_config_files()
CONFIG = read_kv_file(ROOM_CONFIG)
ROOM_ID_RAW = CONFIG.get("room_id", DEFAULT_ROOM_ID).strip()
LX_API = CONFIG.get("lx_api", DEFAULT_LX_API).rstrip("/")
REQUIRE_FANS_MEDAL = CONFIG.get("require_fans_medal", DEFAULT_REQUIRE_FANS_MEDAL).strip()
COOLDOWN_SECONDS = parse_int(CONFIG, "cooldown_seconds", DEFAULT_COOLDOWN)
QUEUE_LIMIT = parse_int(CONFIG, "queue_limit", DEFAULT_QUEUE_LIMIT, 1)

if not ROOM_ID_RAW.isdigit():
    log.error("room_config.txt 的 room_id 尚未填写正确的数字直播间 ID。请修改后重新启动。")
    ROOM_ID = 0
else:
    ROOM_ID = int(ROOM_ID_RAW)


def load_admin_uids() -> set[int]:
    values: set[int] = set()
    try:
        for raw in UID_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            for part in re.split(r"[,;\s]+", line):
                if part.isdigit():
                    values.add(int(part))
    except Exception:
        log.exception("读取 uid.txt 失败")
    return values


ADMIN_UIDS = load_admin_uids()


def read_cookie_string() -> str:
    try:
        raw = COOKIE_FILE.read_text(encoding="utf-8-sig")
    except Exception:
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    cookie = "; ".join(lines)
    if cookie.lower().startswith("cookie="):
        cookie = cookie.split("=", 1)[1].strip()
    return cookie


def parse_cookie(cookie: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key.strip():
            result[key.strip()] = value.strip()
    return result


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132 Safari/537.36", "Referer": "https://live.bilibili.com/"})
COOKIE = read_cookie_string()
COOKIE_DICT = parse_cookie(COOKIE)
if COOKIE_DICT:
    SESSION.cookies.update(COOKIE_DICT)


def save_cookie_string(cookie_values: dict[str, str]) -> None:
    """保存扫码登录得到的 Cookie，不输出 Cookie 内容。"""
    if not cookie_values.get("SESSDATA"):
        raise RuntimeError("扫码登录完成，但没有取得 SESSDATA")
    preferred = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "buvid4", "sid")
    lines = [f"{key}={cookie_values[key]}" for key in preferred if cookie_values.get(key)]
    for key, value in cookie_values.items():
        if key not in preferred and value and key != "acw_tc":
            lines.append(f"{key}={value}")
    COOKIE_FILE.write_text("; ".join(lines) + "\n", encoding="utf-8")


def validate_cookie_string(cookie_string: str) -> tuple[bool, str]:
    """通过 B 站导航接口验证登录态。"""
    values = parse_cookie(cookie_string)
    if not values.get("SESSDATA"):
        return False, "缺少 SESSDATA"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    })
    session.cookies.update(values)
    try:
        response = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") == 0 and isinstance(payload.get("data"), dict) and payload["data"].get("isLogin") is True:
            return True, "B站登录状态验证成功"
        return False, "B站未确认登录，Cookie 可能已失效"
    except Exception as exc:
        return False, f"无法验证 B 站登录状态：{type(exc).__name__}"


def qr_login_and_save_cookie() -> bool:
    """弹出二维码窗口，扫码确认后取得并验证 Cookie。"""
    if qrcode is None or ImageTk is None:
        log.error("缺少二维码依赖，请运行：python -m pip install qrcode[pil]")
        return False
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    })
    try:
        response = session.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate", timeout=10)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("code") != 0 or not isinstance(data, dict) or not data.get("url") or not data.get("qrcode_key"):
            log.error("申请 B 站登录二维码失败：接口返回异常")
            return False
        login_url, qrcode_key = str(data["url"]), str(data["qrcode_key"])
    except Exception as exc:
        log.error("申请 B 站登录二维码失败：%s", type(exc).__name__)
        return False

    root = tk.Tk()
    root.title("B站点歌机器人 - 扫码登录")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="请使用哔哩哔哩手机 App 扫描二维码登录", font=("Microsoft YaHei UI", 11)).pack(pady=(0, 10))
    qr_image = qrcode.make(login_url).convert("RGB").resize((280, 280))
    tk_image = ImageTk.PhotoImage(qr_image)
    image_label = ttk.Label(frame, image=tk_image)
    image_label.image = tk_image
    image_label.pack()
    status_label = ttk.Label(frame, text="等待扫码……", font=("Microsoft YaHei UI", 10))
    status_label.pack(pady=(10, 8))
    ttk.Label(frame, text="扫码后请在手机上确认登录；关闭窗口将取消启动。 ").pack()
    state = {"done": False, "success": False, "closed": False}

    def close_window() -> None:
        state["closed"] = True
        root.destroy()

    def poll() -> None:
        if state["closed"] or state["done"]:
            return
        try:
            poll_response = session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key}, timeout=8,
            )
            poll_response.raise_for_status()
            result = poll_response.json()
            qr_data = result.get("data", {}) if isinstance(result, dict) else {}
            code = qr_data.get("code")
            if code == 86101:
                status_label.configure(text="等待扫码……")
            elif code == 86090:
                status_label.configure(text="已扫码，请在手机上确认登录……")
            elif code == 86038:
                status_label.configure(text="二维码已过期，请重新启动程序获取新二维码。")
                state["done"] = True
                return
            elif code == 0:
                # B站新版扫码返回跨域票据 URL，真实 Cookie 可能在该请求的 Set-Cookie 中下发。
                ticket_url = qr_data.get("url")
                if ticket_url:
                    try:
                        session.get(str(ticket_url), timeout=10, allow_redirects=True)
                    except Exception:
                        pass
                cookie_values = {cookie.name: cookie.value for cookie in session.cookies}
                cookie_string = "; ".join(f"{key}={value}" for key, value in cookie_values.items())
                valid, reason = validate_cookie_string(cookie_string)
                if valid:
                    save_cookie_string(cookie_values)
                    state["success"] = True
                    state["done"] = True
                    status_label.configure(text="登录成功，Cookie 已保存。")
                    root.after(700, root.destroy)
                    return
                log.error("扫码后登录验证失败：%s", reason)
                status_label.configure(text="扫码完成但登录验证失败，请重新启动程序重试。")
                state["done"] = True
                return
            else:
                status_label.configure(text=f"登录状态异常（{code}），请重新启动程序。")
                state["done"] = True
                return
        except Exception as exc:
            status_label.configure(text=f"检查登录状态失败（{type(exc).__name__}），正在重试……")
        if not state["done"] and not state["closed"]:
            root.after(1800, poll)

    root.protocol("WM_DELETE_WINDOW", close_window)
    root.after(500, poll)
    root.mainloop()
    if state["success"]:
        log.info("B站扫码登录成功，Cookie 已保存到 bili_cookie.txt")
        return True
    log.error("未完成 B站扫码登录，程序停止；不会使用匿名身份连接弹幕")
    return False


def ensure_valid_login_cookie() -> bool:
    """现有 Cookie 无效或不存在时弹出二维码；验证成功前禁止连接弹幕。"""
    valid, reason = validate_cookie_string(read_cookie_string())
    if valid:
        log.info("bili_cookie.txt 登录状态有效")
        return True
    log.warning("bili_cookie.txt 无有效登录态：%s", reason)
    log.info("正在打开 B站扫码登录窗口……")
    return qr_login_and_save_cookie()

MEDAL_CACHE: dict[int, tuple[float, bool, str]] = {}


def _find_medal_list(data: Any) -> Optional[list]:
    """仅在响应结构明确包含勋章列表时返回列表；无法确认则返回 None。"""
    if not isinstance(data, dict):
        return None
    for key in ("list", "medal_list", "fans_medal_list"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    # 有些响应把列表放在 data.data 下
    nested = data.get("data")
    if isinstance(nested, dict):
        return _find_medal_list(nested)
    return None


def _medal_matches_room(item: dict[str, Any], room_id: int) -> tuple[bool, Optional[bool]]:
    """返回(房间匹配, 是否明确点亮)。缺少点亮字段视为无法确认。"""
    candidates = [item]
    for key in ("medal", "medal_info", "fans_medal", "target_medal"):
        value = item.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    room_values = []
    light_values = []
    for obj in candidates:
        for key in ("roomid", "room_id", "target_roomid", "target_room_id"):
            if key in obj and obj[key] not in (None, ""):
                try:
                    room_values.append(int(obj[key]))
                except (TypeError, ValueError):
                    pass
        for key in ("is_light", "is_lighted", "light_status", "is_wearing"):
            if key in obj:
                val = obj[key]
                if isinstance(val, bool):
                    light_values.append(val)
                elif str(val).strip() in ("0", "1"):
                    light_values.append(str(val).strip() == "1")
    room_matches = room_id in room_values
    # 若同一条记录中任一明确点亮字段为 false，则按未点亮处理；必须明确 true 才通过。
    is_lit: Optional[bool] = None
    if light_values:
        is_lit = all(light_values)
    return room_matches, is_lit


def check_fan_medal_sync(uid: int) -> tuple[bool, str]:
    """检查当前直播间粉丝牌且处于点亮状态。失败关闭，绝不因接口异常放行。"""
    if not COOKIE_DICT.get("SESSDATA"):
        return False, "bili_cookie.txt 缺少有效的 SESSDATA，无法核验粉丝牌"
    now = time.time()
    cached = MEDAL_CACHE.get(uid)
    if cached and now - cached[0] < MEDAL_CACHE_SECONDS:
        return cached[1], cached[2]
    try:
        response = SESSION.get(
            "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall",
            params={"target_id": uid}, timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            reason = "粉丝牌接口返回异常，无法确认粉丝牌状态"
            MEDAL_CACHE[uid] = (now, False, reason)
            return False, reason
        medals = _find_medal_list(payload["data"])
        if medals is None:
            reason = "粉丝牌接口缺少完整勋章列表，拒绝点歌"
            MEDAL_CACHE[uid] = (now, False, reason)
            return False, reason
        found_room_medal = False
        found_lit_medal = False
        for item in medals:
            if not isinstance(item, dict):
                continue
            matches, is_lit = _medal_matches_room(item, ROOM_ID)
            if matches:
                found_room_medal = True
                if is_lit is True:
                    found_lit_medal = True
                    break
        if found_lit_medal:
            result = (now, True, "已确认拥有本直播间且已点亮的粉丝牌")
        elif found_room_medal:
            result = (now, False, "检测到本直播间粉丝牌，但无法确认其处于点亮状态")
        else:
            result = (now, False, "未找到本直播间粉丝牌")
        MEDAL_CACHE[uid] = result
        return result[1], result[2]
    except Exception as exc:
        reason = f"粉丝牌查询失败：{type(exc).__name__}，拒绝点歌"
        MEDAL_CACHE[uid] = (now, False, reason)
        log.warning("UID %s %s", uid, reason)
        return False, reason


async def check_fan_medal(uid: int) -> tuple[bool, str]:
    return await asyncio.to_thread(check_fan_medal_sync, uid)


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def get_status_text(status: dict[str, Any], keys: tuple[str, ...]) -> str:
    """从洛雪状态 API 的常见字段名及嵌套对象中读取文本字段。"""
    wanted = {key.casefold() for key in keys}
    stack: list[dict[str, Any]] = [status]
    seen: set[int] = set()
    while stack:
        obj = stack.pop(0)
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        for key, value in obj.items():
            if str(key).casefold() in wanted and isinstance(value, (str, int, float)):
                text_value = str(value).strip()
                if text_value:
                    return text_value
        for value in obj.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(x for x in value if isinstance(x, dict))
    return ""


STATUS_TITLE_KEYS = ("name", "title", "song", "songName", "musicName", "trackName", "music_name")
STATUS_ARTIST_KEYS = ("singer", "artist", "artists", "artistName", "singerName", "author", "musicArtist", "music_artist")


def parse_request(text: str) -> Optional[tuple[str, str]]:
    """支持“点歌 歌名”和“点歌 歌名 歌手”。双字段形式以最后一个空格分隔歌手。"""
    content = text.strip()
    if not content.startswith("点歌"):
        return None
    body = content[2:].strip()
    if not body:
        return None
    # 明确分隔写法优先：点歌 歌名 - 歌手 / 点歌 歌名|歌手
    for separator in (" - ", " | ", "｜"):
        if separator in body:
            title, artist = body.rsplit(separator, 1)
            if title.strip() and artist.strip():
                return title.strip(), artist.strip()
    # 常规两段写法：最后一个空格后的词视为歌手；若用户要点多词歌名，推荐用“ - 歌手”区分。
    parts = body.rsplit(None, 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return body, ""


BAD_VERSION_PATTERN = re.compile(
    r"live|dj|r&b版|cover|翻唱|新版|合唱|空灵鼓|练习曲|remix|mix|片段|现场|串烧|dj版|慢摇|演唱会|伴奏|纯音乐|instrumental",
    re.IGNORECASE,
)


def compact_music_name(value: str) -> str:
    """用于匹配的曲名规范化：忽略空格及常见标点，不忽略汉字/数字。"""
    return re.sub(r"[\s\-‐‑‒–—_.,，。!！?？:：;；'\"（）()【】\[\]《》<>·]+", "", value).casefold()


def _music_get_json(url: str, *, referer: str = "https://www.kuwo.cn/") -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/132 Safari/537.36",
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    }
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    text = response.text.strip()
    # 某些音乐接口会返回 JSONP。
    if text.startswith("{") or text.startswith("["):
        return response.json()
    left, right = text.find("("), text.rfind(")")
    if left >= 0 and right > left:
        return json.loads(text[left + 1:right])
    return json.loads(text)


def _types_default(extra: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    # Scheme 要求 types 字段。播放时由洛雪根据实际源信息获取音频链接。
    return [{"type": "128k", "size": None}]


def search_kuwo(keyword: str) -> list[dict[str, Any]]:
    url = "https://search.kuwo.cn/r.s?" + urllib.parse.urlencode({
        "all": keyword, "ft": "music", "itemset": "web_2013", "client": "kt",
        "pn": 0, "rn": 10, "rformat": "json", "encoding": "utf8",
    })
    data = _music_get_json(url)
    rows = data.get("abslist", []) if isinstance(data, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("SONGNAME", "")).strip()
        artist = str(row.get("ARTIST", "")).strip()
        song_id = str(row.get("MUSICRID", row.get("MUSICID", ""))).strip()
        if song_id.startswith("MUSIC_"):
            song_id = song_id[6:]
        if not title or not artist or not song_id:
            continue
        out.append({"title": title, "artist": artist, "source": "kw", "songmid": song_id,
                    "albumId": str(row.get("ALBUMID", "")), "albumName": str(row.get("ALBUM", "")),
                    "interval": str(row.get("DURATION", "")), "types": _types_default(),
                    "_search_source": "kw"})
    return out


def search_kugou(keyword: str) -> list[dict[str, Any]]:
    url = "https://mobilecdn.kugou.com/api/v3/search/song?" + urllib.parse.urlencode({
        "format": "json", "keyword": keyword, "page": 1, "pagesize": 10,
    })
    data = _music_get_json(url, referer="https://www.kugou.com/")
    rows = ((data.get("data") or {}).get("info") or []) if isinstance(data, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"<[^>]+>", "", str(row.get("songname", ""))).strip()
        artist = re.sub(r"<[^>]+>", "", str(row.get("singername", ""))).strip()
        song_hash = str(row.get("hash", "")).strip()
        if not title or not artist or not song_hash:
            continue
        out.append({"title": title, "artist": artist, "source": "kg", "songmid": song_hash,
                    "hash": song_hash, "albumId": str(row.get("album_id", "")),
                    "albumName": str(row.get("album_name", "")),
                    "interval": str(row.get("duration", "")),
                    "types": [{"type": "128k", "size": None, "hash": song_hash}],
                    "_search_source": "kg"})
    return out


def search_qq(keyword: str) -> list[dict[str, Any]]:
    url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?" + urllib.parse.urlencode({
        "ct": 24, "qqmusic_ver": 1298, "new_json": 1, "remoteplace": "txt.yqq.song",
        "t": 0, "aggr": 1, "cr": 1, "catZhida": 1, "lossless": 0, "flag_qc": 0,
        "p": 1, "n": 10, "w": keyword, "format": "json",
    })
    data = _music_get_json(url, referer="https://y.qq.com/")
    rows = (((data.get("data") or {}).get("song") or {}).get("list") or []) if isinstance(data, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("name", "")).strip()
        singers = row.get("singer", [])
        artist = "、".join(str(x.get("name", "")).strip() for x in singers if isinstance(x, dict) and x.get("name")) if isinstance(singers, list) else ""
        song_id = str(row.get("mid", "")).strip()
        media_mid = str(row.get("file", {}).get("media_mid", "") if isinstance(row.get("file"), dict) else "").strip()
        album_mid = str(row.get("album", {}).get("mid", "") if isinstance(row.get("album"), dict) else "").strip()
        if not title or not artist or not song_id or not media_mid:
            continue
        out.append({"title": title, "artist": artist, "source": "tx", "songmid": song_id,
                    "strMediaMid": media_mid, "albumMid": album_mid,
                    "albumId": str(row.get("album", {}).get("id", "") if isinstance(row.get("album"), dict) else ""),
                    "albumName": str(row.get("album", {}).get("name", "") if isinstance(row.get("album"), dict) else ""),
                    "interval": str(row.get("interval", "")), "types": _types_default(),
                    "_search_source": "tx"})
    return out


def search_netease(keyword: str) -> list[dict[str, Any]]:
    url = "https://music.163.com/api/search/get/web?" + urllib.parse.urlencode({
        "s": keyword, "type": 1, "offset": 0, "total": "true", "limit": 10,
    })
    data = _music_get_json(url, referer="https://music.163.com/")
    rows = ((data.get("result") or {}).get("songs") or []) if isinstance(data, dict) else []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("name", "")).strip()
        artists = row.get("artists", row.get("ar", []))
        artist = "、".join(str(x.get("name", "")).strip() for x in artists if isinstance(x, dict) and x.get("name")) if isinstance(artists, list) else ""
        song_id = str(row.get("id", "")).strip()
        album = row.get("album", row.get("al", {}))
        if not isinstance(album, dict):
            album = {}
        if not title or not artist or not song_id:
            continue
        out.append({"title": title, "artist": artist, "source": "wy", "songmid": song_id,
                    "albumId": str(album.get("id", "")), "albumName": str(album.get("name", "")),
                    "interval": str(row.get("duration", "")), "types": _types_default(),
                    "_search_source": "wy"})
    return out


def find_matching_song(title: str, artist: str) -> tuple[Optional[dict[str, Any]], str]:
    """先直接请求常见音源的搜索接口，再对结构化结果进行严格匹配。"""
    keyword = f"{title} {artist}".strip()
    providers = (("kw", search_kuwo), ("kg", search_kugou), ("tx", search_qq), ("wy", search_netease))
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    for source_name, search_func in providers:
        try:
            candidates.extend(search_func(keyword))
        except Exception as exc:
            errors.append(f"{source_name}:{type(exc).__name__}")
    wanted_title = compact_music_name(title)
    wanted_artist = compact_music_name(artist)
    matches: list[tuple[int, dict[str, Any]]] = []
    for item in candidates:
        candidate_title = compact_music_name(str(item.get("title", "")))
        candidate_artist = compact_music_name(str(item.get("artist", "")))
        # 必须有实质性的歌名匹配，不能只因为搜索结果排第一就放行。
        title_match = bool(candidate_title and wanted_title) and (
            candidate_title == wanted_title or candidate_title.startswith(wanted_title) or wanted_title.startswith(candidate_title)
        )
        if not title_match:
            continue
        if artist and not (wanted_artist and wanted_artist in candidate_artist):
            continue
        # 有指定歌手时必须包含歌手；所有情况都拒绝明显的翻唱、Live、DJ、伴奏等版本。
        if BAD_VERSION_PATTERN.search(str(item.get("title", ""))) or BAD_VERSION_PATTERN.search(str(item.get("artist", ""))):
            continue
        score = 100 if candidate_title == wanted_title else 60
        if artist and wanted_artist in candidate_artist:
            score += 30
        # 同分时优先酷我，其次酷狗、QQ、网易云；只作为候选排序，不代表原唱保证。
        score += {"kw": 4, "kg": 3, "tx": 2, "wy": 1}.get(str(item.get("source")), 0)
        matches.append((score, item))
    if not matches:
        if not candidates and errors:
            return None, "所有音乐源搜索请求均失败（" + ", ".join(errors) + "）"
        if artist:
            return None, f"未找到歌名匹配且歌手包含“{artist}”的歌曲"
        return None, "未找到符合歌名且非明显翻唱/现场/DJ/伴奏的歌曲"
    matches.sort(key=lambda pair: pair[0], reverse=True)
    selected = matches[0][1]
    return selected, "匹配成功"


def lx_scheme_play(song: dict[str, Any]) -> str:
    """用已验证的歌曲 ID 和音源调用官方 music/play Scheme，不让洛雪重新搜索选歌。"""
    info = song.get("music_info") if isinstance(song.get("music_info"), dict) else song
    payload: dict[str, Any] = {
        "name": str(info.get("title", info.get("name", song.get("title", "")))),
        "singer": str(info.get("artist", info.get("singer", song.get("artist", "")))),
        "source": str(info.get("source", "kw")),
        "songmid": str(info.get("songmid", info.get("id", ""))),
        "types": info.get("types") or [{"type": "128k", "size": None}],
    }
    for key in ("img", "albumId", "interval", "albumName", "hash", "strMediaMid", "albumMid", "copyrightId", "lrcUrl"):
        if info.get(key) not in (None, ""):
            payload[key] = info[key]
    if not payload["name"] or not payload["singer"] or not payload["songmid"]:
        raise ValueError("已匹配歌曲缺少播放所需的名称、歌手或歌曲 ID")
    encoded = urllib.parse.quote(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), safe="")
    return f"lxmusic://music/play?data={encoded}"


def play_song_in_lx(song: dict[str, Any]) -> bool:
    """通过官方 music/play Scheme 播放已匹配的具体歌曲。"""
    try:
        uri = lx_scheme_play(song)
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]
        else:
            webbrowser.open(uri)
        log.info(
            "洛雪播放调用已发起：目标=%s - %s；source=%s；songmid=%s。此日志仅表示系统已调用 Scheme，不代表洛雪已成功播放。",
            song.get("title", ""), song.get("artist", ""), song.get("source", ""), song.get("songmid", ""),
        )
        return True
    except Exception:
        log.exception("调用已匹配歌曲播放失败：%s", song.get("title", "未知歌曲"))
        return False


def get_lx_status() -> Optional[dict[str, Any]]:
    """读取本地 API 状态；API 不可用时返回 None。"""
    for endpoint in ("/status", "/api/status"):
        try:
            response = requests.get(LX_API + endpoint, timeout=2)
            if response.ok:
                data = response.json()
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return None


def verify_requested_song(
    song: dict[str, Any],
    before_status: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> tuple[bool, str]:
    """核对洛雪实际播放结果。无论是否填写歌手，都必须确认曲名；填写歌手时还要核对歌手。"""
    requested_title = normalize_name(str(song.get("title", "")))
    requested_artist = normalize_name(str(song.get("artist", "")))
    before_title = normalize_name(get_status_text(before_status or {}, STATUS_TITLE_KEYS))
    deadline = time.monotonic() + timeout
    last_status: Optional[dict[str, Any]] = None
    saw_new_playback = False

    while time.monotonic() < deadline:
        status = get_lx_status()
        if status:
            last_status = status
            raw_title = get_status_text(status, STATUS_TITLE_KEYS)
            raw_artist = get_status_text(status, STATUS_ARTIST_KEYS)
            actual_title = normalize_name(raw_title)
            actual_artist = normalize_name(raw_artist)
            status_text = str(status.get("status", status.get("playStatus", status.get("state", "")))).lower()
            playing = status_text in ("playing", "play", "1", "true") or status.get("playing") is True or status.get("isPlaying") is True
            # 若当前曲目与点歌前不同，说明洛雪已切换到搜索结果；不把点歌前正在播放的旧歌误认为搜索成功。
            changed_from_before = bool(before_title and actual_title and actual_title != before_title)
            if playing and changed_from_before:
                saw_new_playback = True

            title_matches = bool(actual_title and requested_title) and (
                actual_title == requested_title
                or requested_title in actual_title
                or actual_title in requested_title
            )
            if playing and title_matches:
                if requested_artist:
                    artist_matches = bool(actual_artist) and (
                        requested_artist == actual_artist
                        or requested_artist in actual_artist
                        or actual_artist in requested_artist
                    )
                    if not artist_matches:
                        ok, detail = pause_lx_playback()
                        if ok:
                            return False, f"找到的歌曲歌手为“{raw_artist or '未知'}”，与要求的“{song.get('artist')}”不一致，已请求暂停。{detail}"
                        return False, f"找到的歌曲歌手为“{raw_artist or '未知'}”，与要求的“{song.get('artist')}”不一致；暂停失败：{detail}"
                    return True, f"已核对实际播放：{raw_title} - {raw_artist}"
                # 没填写歌手时，曲名匹配即可；实际歌手用于页面展示。搜索仅按歌名，尽量由洛雪默认结果选择常见/原唱版本。
                song["actual_artist"] = raw_artist
                return True, f"已核对实际播放：{raw_title}" + (f" - {raw_artist}" if raw_artist else "（接口未返回歌手）")

        time.sleep(1)

    if last_status is None:
        return False, "无法读取洛雪状态接口，不能确认歌曲是否存在，已拒绝本次点歌"

    # 仅当检测到洛雪确实切换到了错误的新曲目时才暂停；若仍是点歌前旧歌，避免误暂停观众原本播放的歌曲。
    if saw_new_playback:
        ok, detail = pause_lx_playback()
        if ok:
            return False, f"洛雪实际播放的歌曲与点歌名称“{song.get('title')}”不一致，已请求暂停。{detail}"
        return False, f"洛雪实际播放的歌曲与点歌名称“{song.get('title')}”不一致；暂停失败：{detail}"
    return False, f"洛雪未能确认存在歌曲“{song.get('title')}”，可能没有搜索结果或未成功切换播放，已拒绝本次点歌"


def pause_lx_playback() -> tuple[bool, str]:
    """仅供歌曲版本核验发现错误曲目时使用；管理员切歌流程不得调用此函数。"""
    try:
        response = requests.get(LX_API + "/pause", timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return True, f"API 已接受暂停请求（HTTP {response.status_code}）"
    except Exception as exc:
        return False, f"暂停请求失败：{type(exc).__name__}"


def skip_lx_next() -> tuple[bool, str]:
    """请求洛雪音乐切换到它自身播放列表的下一首；绝不使用 /pause 代替切歌。"""
    try:
        response = requests.get(LX_API + "/skip-next", timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        return True, f"洛雪下一曲接口已接受请求（HTTP {response.status_code}）；尚未确认下一首已开始播放"
    except Exception as exc:
        return False, f"调用洛雪下一曲接口失败：{type(exc).__name__}。请检查洛雪开放 API 是否已启用"


def try_restore_lx_state(snapshot: Optional[dict[str, Any]]) -> None:
    """尽力恢复点歌前状态；本机接口若不提供原曲/进度字段，则只记录无法完整恢复。"""
    if not snapshot:
        log.info("状态 API 不可用，无法自动恢复点歌前歌曲/进度")
        return
    # 不对未知字段做危险猜测；仅输出状态供排查。播放前的歌曲和播放列表恢复取决于本机 API 支持。
    log.info("点歌前状态已记录；当前 API 未提供可确认的通用恢复命令，原播放列表可能无法完整恢复")


class SongBot:
    def __init__(self) -> None:
        self.queue: deque[dict[str, Any]] = deque()
        self.queued_keys: set[tuple[str, str]] = set()
        self.cooldowns: dict[int, float] = {}
        self.queue_lock = asyncio.Lock()
        self.play_lock = asyncio.Lock()
        self.current: Optional[dict[str, Any]] = None
        self.room: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._skip_requested = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._medal_enabled = REQUIRE_FANS_MEDAL == "0"
        self._restore_snapshot: Optional[dict[str, Any]] = None

    def dispatch_danmu(self, event: dict[str, Any]) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self.on_danmu(event)))
        except RuntimeError:
            return

    def queue_snapshot(self) -> dict[str, Any]:
        current = None
        if self.current:
            # 若观众点歌时没有填写歌手，展示页使用洛雪状态 API 确认的实际歌手。
            current = {
                "title": self.current.get("title", ""),
                "artist": self.current.get("artist", "") or self.current.get("actual_artist", ""),
                "uname": self.current.get("uname", ""),
            }
        waiting = []
        for i, item in enumerate(list(self.queue), 1):
            waiting.append({
                "index": i,
                "title": item.get("title", ""),
                "artist": item.get("artist", ""),
                "uname": item.get("uname", ""),
            })
        return {"ok": True, "current": current, "queue": waiting, "queue_count": len(waiting), "queue_limit": QUEUE_LIMIT}

    async def panel_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        if action in ("skip", "next"):
            if self.current is None:
                return {"ok": False, "message": "当前没有正在处理的机器人点播歌曲，无法按当前点歌流程切歌"}

            # 先在队列锁内确认是否存在下一首机器人点播，避免判断过程中队列状态变化。
            async with self.queue_lock:
                has_next_request = bool(self.queue)
                current_title = str(self.current.get("title", "未知歌曲")) if self.current else "未知歌曲"
                next_title = str(self.queue[0].get("title", "未知歌曲")) if self.queue else ""

            if has_next_request:
                # 有下一首机器人点播时，只通知播放 worker 结束当前曲目等待，
                # 不调用洛雪 /pause，也不调用洛雪 /skip-next，避免跳过洛雪自己的列表歌曲。
                self._skip_requested.set()
                message = (
                    f"已提交切歌请求：准备从“{current_title}”切换到下一首点播“{next_title}”。"
                    "请求已交给机器人播放流程，尚未确认下一首已开始播放。"
                )
                log.info("控制面板提交切歌：当前=%s；下一首点播=%s", current_title, next_title)
                return {"ok": True, "message": message}

            # 机器人待播队列为空：改为请求洛雪自身播放列表的下一首。
            ok, detail = await asyncio.to_thread(skip_lx_next)
            if not ok:
                log.error("控制面板切歌失败：%s", detail)
                return {"ok": False, "message": detail}
            self._skip_requested.set()
            log.info("机器人待播队列为空，已请求洛雪下一曲：%s", detail)
            return {"ok": True, "message": detail}
        if action == "delete":
            try:
                index = int(payload.get("index", 0))
            except (TypeError, ValueError):
                return {"ok": False, "message": "队列序号无效"}
            async with self.queue_lock:
                items = list(self.queue)
                if index < 1 or index > len(items):
                    return {"ok": False, "message": "该队列序号已不存在，请刷新后重试"}
                removed = items.pop(index - 1)
                self.queue = deque(items)
                self.queued_keys.discard(removed.get("key"))
            log.info("控制页面删除待播歌曲：%s%s", removed.get("title", ""), f" - {removed.get('artist')}" if removed.get("artist") else "")
            return {"ok": True, "message": f"已删除：{removed.get('title', '')}"}
        if action == "clear":
            async with self.queue_lock:
                count = len(self.queue)
                self.queue.clear()
                self.queued_keys = {self.current.get("key")} if self.current else set()
            log.info("控制页面清空待播队列，共移除 %s 首", count)
            return {"ok": True, "message": f"已清空待播队列（{count} 首）"}
        return {"ok": False, "message": "不支持的操作"}

    async def on_danmu(self, event: dict[str, Any]) -> None:
        try:
            info = event.get("data", {}).get("info", [])
            message = str(info[1]).strip()
            user = info[2]
            uid = int(user[0])
            uname = str(user[1]) if len(user) > 1 else str(uid)
        except Exception:
            return
        if not message:
            return
        if message == "切歌":
            if uid not in ADMIN_UIDS:
                log.info("【%s】尝试切歌但不是管理员，已拒绝 (UID:%s)", uname, uid)
                return
            log.info("管理员【%s】发出切歌指令", uname)
            if self.current is None:
                log.info("当前没有机器人正在处理的点播歌曲，无需切歌")
                return
            result = await self.panel_action({"action": "skip"})
            if result.get("ok"):
                log.info("已通知点歌队列结束当前歌曲，准备处理下一首")
            else:
                log.error("切歌失败，保留当前点播流程：%s", result.get("message", "未知错误"))
            return
        parsed = parse_request(message)
        if not parsed:
            return
        title, artist = parsed
        if uid not in ADMIN_UIDS:
            now = time.monotonic()
            last = self.cooldowns.get(uid, 0.0)
            if now - last < COOLDOWN_SECONDS:
                remain = max(1, int(COOLDOWN_SECONDS - (now - last)))
                log.info("【%s】点歌被冷却限制，剩余 %s 秒 (UID:%s)", uname, remain, uid)
                return
            if self._medal_enabled:
                ok, reason = await check_fan_medal(uid)
                if not ok:
                    log.info("【%s】点歌拒绝：%s (UID:%s)", uname, reason, uid)
                    return
            self.cooldowns[uid] = now
        await self.enqueue(title, artist, uname, uid)

    async def enqueue(self, title: str, artist: str, uname: str, uid: int) -> None:
        # 先搜索并核验，只有拿到明确的歌曲 ID/音源后才入队。
        matched, reason = await asyncio.to_thread(find_matching_song, title, artist)
        if not matched:
            log.info("【%s】点歌拒绝：%s%s；%s", uname, title, f" - {artist}" if artist else "", reason)
            return

        actual_title = str(matched.get("title", "")).strip()
        actual_artist = str(matched.get("artist", "")).strip()
        key = (normalize_name(actual_title), normalize_name(actual_artist))
        item: dict[str, Any] = {
            "title": actual_title,
            "artist": actual_artist,
            "requested_title": title,
            "requested_artist": artist,
            "source": str(matched.get("source", "")),
            "songmid": str(matched.get("songmid", "")),
            "music_info": matched,
            "uname": uname,
            "uid": uid,
            "key": key,
            "added_at": time.time(),
        }
        async with self.queue_lock:
            if key in self.queued_keys or (self.current and key == self.current.get("key")):
                log.info("【%s】点歌重复，已拒绝：%s - %s", uname, actual_title, actual_artist)
                return
            if len(self.queue) >= QUEUE_LIMIT:
                log.info("【%s】点歌失败：队列已满（上限 %s 首）", uname, QUEUE_LIMIT)
                return
            self.queue.append(item)
            self.queued_keys.add(key)
            log.info("点歌匹配成功并入队：%s - %s（音源：%s；点歌人：%s，UID:%s；队列 %s/%s）",
                     actual_title, actual_artist, item["source"], uname, uid, len(self.queue), QUEUE_LIMIT)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self.play_worker())

    async def play_worker(self) -> None:
        async with self.play_lock:
            while True:
                async with self.queue_lock:
                    if not self.queue:
                        self.current = None
                        return
                    item = self.queue.popleft()
                    self.queued_keys.discard(item["key"])
                    self.current = item
                self._skip_requested.clear()
                if self._restore_snapshot is None:
                    self._restore_snapshot = await asyncio.to_thread(get_lx_status)
                # 每首歌播放前单独保存状态，便于区分“搜索失败仍停留在旧歌”和“切换到了错误歌曲”。
                before_status = await asyncio.to_thread(get_lx_status)
                log.info("开始播放点歌：%s%s", item["title"], f" - {item['artist']}" if item["artist"] else "")
                started = await asyncio.to_thread(play_song_in_lx, item)
                if not started:
                    log.error("调用音乐失败，跳过本首：%s", item["title"])
                    continue
                verified, verify_message = await asyncio.to_thread(verify_requested_song, item, before_status)
                if not verified:
                    log.error("点歌歌曲核验失败：%s%s；%s", item["title"], f" - {item['artist']}" if item.get("artist") else "", verify_message)
                    continue
                log.info("点歌歌曲核验：%s", verify_message)

                # 如果管理员在 URI Scheme 启动或核验期间切歌，wait_song_end 会立即返回，
                # 随后播放 worker 处理下一首；这里绝不补发暂停，避免把切歌变成暂停。
                if self._skip_requested.is_set():
                    log.info("检测到播放启动/核验期间收到切歌请求；将直接进入下一步播放流程，不发送暂停命令")
                # URI Scheme 发起播放后等待用户切歌或当前曲目结束。
                # status API 的字段因本机插件版本而异，采用多种常见字段判断。
                await self.wait_song_end()
                log.info("机器人点播流程结束：%s", item["title"])
            
    async def wait_song_end(self) -> None:
        start = time.monotonic()
        saw_playing = False
        unchanged = 0
        previous_signature: Optional[str] = None
        while True:
            if self._skip_requested.is_set():
                return
            await asyncio.sleep(2)
            status = await asyncio.to_thread(get_lx_status)
            if not status:
                # 没有可用状态 API 时不猜测歌曲结束，允许管理员切歌控制。
                continue
            status_text = str(status.get("status", status.get("playStatus", status.get("state", "")))).lower()
            is_playing = status.get("playing") is True or status.get("isPlaying") is True or status_text in ("playing", "play", "1", "true")
            if is_playing:
                saw_playing = True

            # 只在洛雪当前播放曲名与本次点歌匹配时，记录实际歌手；不把旧曲目的歌手显示到新曲目上。
            if self.current and not self.current.get("artist"):
                actual_title = normalize_name(get_status_text(status, STATUS_TITLE_KEYS))
                requested_title = normalize_name(str(self.current.get("title", "")))
                actual_artist = get_status_text(status, STATUS_ARTIST_KEYS)
                title_matches = bool(actual_title and requested_title) and (
                    actual_title == requested_title
                    or actual_title in requested_title
                    or requested_title in actual_title
                )
                if title_matches and actual_artist:
                    self.current["actual_artist"] = actual_artist
            # 官方状态为 paused 时代表用户主动暂停，不代表歌曲结束。
            # 暂停期间必须继续等待；即使进度已经接近总时长，也不能提前清空当前点歌。
            if status_text in ("paused", "pause"):
                continue
            progress = status.get("progress", status.get("currentTime", status.get("position", None)))
            duration = status.get("duration", status.get("totalTime", status.get("length", None)))
            try:
                progress_num = float(progress) if progress is not None else None
                duration_num = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                progress_num, duration_num = None, None
            signature = json.dumps({"status": status_text, "progress": progress, "duration": duration}, ensure_ascii=False, sort_keys=True)
            if signature == previous_signature:
                unchanged += 1
            else:
                unchanged = 0
            previous_signature = signature
            # paused/pause 已在上方单独处理。只有明确停止/结束状态才结束当前点歌流程。
            # 官方 API 的停止状态字段拼写为 stoped（单个 p），兼容该值。
            if saw_playing and not is_playing and status_text in ("stopped", "stoped", "stop", "ended", "idle"):
                return
            if saw_playing and progress_num is not None and duration_num is not None and duration_num > 0 and progress_num >= duration_num - 2:
                return
            # 若 API 有状态但没有可用播放状态字段，避免因启动前状态直接判断结束。
            if not saw_playing and time.monotonic() - start > 90:
                log.warning("状态 API 未确认播放状态；仍等待管理员发送“切歌”或状态恢复")
                start = time.monotonic()
            if unchanged >= 30 and saw_playing:
                log.warning("状态连续约 60 秒未变化；继续等待切歌指令")
                unchanged = 0

    async def run(self) -> None:
        if not ROOM_ID:
            return
        self.loop = asyncio.get_running_loop()
        credential = None
        cookie = read_cookie_string()
        cookie_values = parse_cookie(cookie)
        if cookie_values.get("SESSDATA"):
            credential = Credential(
                sessdata=cookie_values.get("SESSDATA", ""),
                bili_jct=cookie_values.get("bili_jct", ""),
                buvid3=cookie_values.get("buvid3", ""),
            )
        if not cookie_values.get("SESSDATA"):
            raise RuntimeError("未检测到有效 SESSDATA。必须先完成扫码登录，禁止匿名连接。")
        if REQUIRE_FANS_MEDAL not in ("0", "1"):
            log.warning("require_fans_medal 配置值无效，按默认 0（要求点亮粉丝牌）处理")
            self._medal_enabled = True
        if credential is None:
            raise RuntimeError("Cookie 不可用，禁止以匿名身份连接直播间")
        self.room = live.LiveDanmaku(ROOM_ID, credential=credential)
        log.info("弹幕连接：使用已验证的 bili_cookie.txt Cookie")
        # 同步事件入口：显式把异步处理提交到主事件循环，避免事件库回调调度差异。
        self.room.on("DANMU_MSG")(self.dispatch_danmu)
        log.info("B站点歌机器人启动；房间=%s；粉丝牌限制=%s；冷却=%s秒；队列上限=%s", ROOM_ID, "开启（需点亮）" if self._medal_enabled else "关闭", COOLDOWN_SECONDS, QUEUE_LIMIT)
        log.info("固定指令：点歌 歌名 / 点歌 歌名 歌手；管理员切歌指令：切歌")
        await self.room.connect()


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "BiliLXSongBotPanel/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # 避免网页每 2 秒刷新时刷屏控制台。
        return

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/api/queue":
            if bot_instance is None:
                self._send_json(503, {"ok": False, "message": "机器人尚未启动"})
            else:
                self._send_json(200, bot_instance.queue_snapshot())
            return
        if route in ("/", "/index.html", "/control", "/queue_panel.html"):
            path = PANEL_HTML_FILE
            missing = "queue_panel.html"
        elif route in ("/display", "/display_panel.html"):
            path = DISPLAY_HTML_FILE
            missing = "display_panel.html"
        else:
            self.send_error(404)
            return
        try:
            body = path.read_bytes()
        except OSError:
            body = f"找不到 {missing}，请将网页文件放在 main.py 同一目录。".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/action":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            action = payload.get("action")
        except Exception:
            self._send_json(400, {"ok": False, "message": "请求内容无效"})
            return
        if action == "shutdown":
            loop = main_async_loop
            event = shutdown_async_event
            if loop is None or loop.is_closed() or event is None:
                self._send_json(503, {"ok": False, "message": "程序当前无法安全结束"})
                return
            # 先回复网页，再通知主协程退出；结束程序时绝不向洛雪发送暂停/停止命令。
            self._send_json(200, {"ok": True, "message": "正在结束点歌机器人，洛雪音乐将继续播放。"})
            loop.call_soon_threadsafe(event.set)
            return
        loop = main_async_loop
        bot = bot_instance
        if loop is None or loop.is_closed() or bot is None:
            self._send_json(503, {"ok": False, "message": "机器人尚未运行"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(bot.panel_action(payload), loop)
            result = future.result(timeout=20)
            self._send_json(200 if result.get("ok") else 400, result)
        except Exception as exc:
            log.exception("网页控制操作失败")
            self._send_json(500, {"ok": False, "message": f"操作失败：{type(exc).__name__}"})


def start_panel_server() -> None:
    global panel_server
    if panel_server is not None:
        return
    try:
        panel_server = ThreadingHTTPServer((PANEL_HOST, PANEL_PORT), PanelHandler)
        panel_server.daemon_threads = True
        thread = threading.Thread(target=panel_server.serve_forever, name="SongBotPanelHTTP", daemon=True)
        thread.start()
        log.info("点歌控制页面：http://%s:%s/", PANEL_HOST, PANEL_PORT)
        log.info("点歌展示页面：http://%s:%s/display", PANEL_HOST, PANEL_PORT)
    except OSError as exc:
        panel_server = None
        log.exception("点歌网页服务启动失败（端口 %s 可能已被占用）：%s", PANEL_PORT, type(exc).__name__)


def stop_panel_server() -> None:
    global panel_server
    if panel_server is not None:
        try:
            panel_server.shutdown()
            panel_server.server_close()
        except Exception:
            pass
        panel_server = None


async def main() -> None:
    global main_async_loop, bot_instance, shutdown_async_event
    if not ROOM_ID:
        log.error("room_config.txt 中的 room_id 未设置，程序退出")
        return
    main_async_loop = asyncio.get_running_loop()
    shutdown_async_event = asyncio.Event()
    bot_instance = SongBot()
    start_panel_server()

    bot_task = asyncio.create_task(bot_instance.run(), name="SongBotMain")
    shutdown_task = asyncio.create_task(shutdown_async_event.wait(), name="SongBotShutdownWaiter")
    try:
        done, _ = await asyncio.wait(
            {bot_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            log.info("收到网页结束程序指令；不会暂停或停止洛雪音乐。")
            # 尝试正常断开弹幕连接；即使断开失败，也继续结束本程序。
            room = bot_instance.room
            disconnect = getattr(room, "disconnect", None) if room is not None else None
            if callable(disconnect):
                try:
                    result = disconnect()
                    if asyncio.iscoroutine(result):
                        await asyncio.wait_for(result, timeout=3)
                except Exception:
                    pass
            if not bot_task.done():
                bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        else:
            # 弹幕任务若自行结束，控制页仍保持可用，用户仍可点击结束程序按钮退出。
            try:
                await bot_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("机器人运行异常；等待网页结束程序指令")
            await shutdown_task
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
        if not bot_task.done():
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        # 先启动本地网页服务：即使正在扫码登录或 B 站连接暂时失败，页面本身仍可访问。
        start_panel_server()
        # 在进入 asyncio 弹幕循环前强制完成登录；无 Cookie / Cookie 失效时弹出二维码。
        # 登录失败、取消或二维码失效均直接退出，绝不匿名连接。
        if ensure_valid_login_cookie():
            # 扫码可能刚刚更新 Cookie，刷新全局校验用的 Cookie 和 Session。
            COOKIE = read_cookie_string()
            COOKIE_DICT = parse_cookie(COOKIE)
            SESSION.cookies.clear()
            SESSION.cookies.update(COOKIE_DICT)
            asyncio.run(main())
        else:
            raise SystemExit(1)
    except KeyboardInterrupt:
        print("机器人已停止")
    finally:
        stop_panel_server()
