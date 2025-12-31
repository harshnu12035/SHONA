import asyncio
import contextlib
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple, Union

import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch

from AnnieXMedia.utils.cookie_handler import COOKIE_PATH
from AnnieXMedia.utils.database import is_on_off
from AnnieXMedia.utils.downloader import yt_dlp_download
from AnnieXMedia.utils.errors import capture_internal_err
from AnnieXMedia.utils.formatters import time_to_seconds
from AnnieXMedia.utils.tuning import YTDLP_TIMEOUT, YOUTUBE_META_MAX, YOUTUBE_META_TTL


# ================= THUMBNAIL SYSTEM =================
DEFAULT_THUMB = "https://telegra.ph/file/8c6a5b9f3b6c1e6b9c7a2.jpg"

def dual_thumb(video_id: str) -> str:
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return DEFAULT_THUMB


# ================= CACHE =================
_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_cache_lock = asyncio.Lock()


# ================= HELPERS =================
def _cookiefile_path() -> Optional[str]:
    try:
        if COOKIE_PATH and os.path.exists(COOKIE_PATH) and os.path.getsize(COOKIE_PATH) > 0:
            return str(COOKIE_PATH)
    except Exception:
        pass
    return None


def _cookies_args() -> List[str]:
    path = _cookiefile_path()
    return ["--cookies", path] if path else []


async def _exec_proc(*args: str) -> Tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return b"", b"timeout"


@capture_internal_err
async def cached_youtube_search(query: str) -> List[Dict]:
    key = f"q:{query}"
    now = time.time()

    async with _cache_lock:
        if key in _cache:
            ts, val = _cache[key]
            if now - ts < YOUTUBE_META_TTL:
                return val
            _cache.pop(key, None)

        if len(_cache) > YOUTUBE_META_MAX:
            _cache.clear()

    try:
        data = await VideosSearch(query, limit=1).next()
        result = data.get("result", [])
    except Exception:
        result = []

    if result:
        async with _cache_lock:
            _cache[key] = (now, result)

    return result


# ================= MAIN CLASS =================
class YouTubeAPI:
    def __init__(self) -> None:
        self.base_url = "https://www.youtube.com/watch?v="
        self._url_pattern = re.compile(r"(youtube\.com|youtu\.be)")

    # ---------- URL UTILS ----------
    def _prepare_link(self, text: str) -> str:
        text = text.strip()
        if "youtu.be" in text:
            return self.base_url + text.split("/")[-1].split("?")[0]
        if "youtube.com/shorts/" in text or "youtube.com/live/" in text:
            return self.base_url + text.split("/")[-1].split("?")[0]
        return text.split("&")[0]

    @capture_internal_err
    async def exists(self, link: str) -> bool:
        return bool(self._url_pattern.search(link))

    @capture_internal_err
    async def url(self, message: Message) -> Optional[str]:
        msgs = [message] + ([message.reply_to_message] if message.reply_to_message else [])
        for msg in msgs:
            text = msg.text or msg.caption or ""
            entities = (msg.entities or []) + (msg.caption_entities or [])
            for ent in entities:
                if ent.type == MessageEntityType.URL:
                    return text[ent.offset : ent.offset + ent.length]
                if ent.type == MessageEntityType.TEXT_LINK:
                    return ent.url
        return None

    # ---------- DETAILS ----------
    @capture_internal_err
    async def details(
        self, link: str, videoid: Union[str, bool, None] = None
    ) -> Tuple[str, Optional[str], int, str, str]:

        query = self._prepare_link(link)
        result = await cached_youtube_search(query)
        if not result:
            raise ValueError("No results found")

        info = result[0]
        duration = info.get("duration")
        seconds = int(time_to_seconds(duration)) if duration else 0
        vid = info.get("id", "")

        return (
            info.get("title", ""),
            duration,
            seconds,
            dual_thumb(vid),
            vid,
        )

    # ---------- TRACK ----------
    @capture_internal_err
    async def track(
        self, link: str, videoid: Union[str, bool, None] = None
    ) -> Tuple[Dict, str]:

        query = self._prepare_link(link)
        info = None

        result = await cached_youtube_search(query)
        if result:
            info = result[0]

        if not info:
            if not query.startswith("http"):
                query = f"ytsearch1:{query}"

            stdout, stderr = await _exec_proc(
                "yt-dlp",
                *(_cookies_args()),
                "--dump-json",
                "--no-warnings",
                query,
            )

            if not stdout:
                err = stderr.decode().strip() if stderr else "Unknown error"
                raise ValueError(f"yt-dlp failed: {err}")

            info = json.loads(stdout.decode())

        vid = info.get("id", "")

        details = {
            "title": info.get("title", ""),
            "link": info.get("webpage_url", self.base_url + vid),
            "vidid": vid,
            "duration_min": info.get("duration"),
            "thumb": dual_thumb(vid),
        }

        return details, vid

    # ---------- DOWNLOAD ----------
    @capture_internal_err
    async def download(
        self,
        link: str,
        mystic,
        *,
        video: Union[bool, str, None] = None,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[Optional[str], Optional[bool]]:

        link = self._prepare_link(link)

        if video:
            p = await yt_dlp_download(
                link,
                type="video",
                title="video",
            )
            return (p, True) if p else (None, None)

        p = await yt_dlp_download(
            link,
            type="audio",
            title="audio",
        )
        return (p, True) if p else (None, None)
