"""Recognise a short-video link: which platform it is, which video it points at, and a clean canonical URL.

Pure functions, no network. Tracking parameters (si=, igsh=, gwpx= ...) are dropped so the same video pasted
twice in different shapes is recognised as one.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

PLATFORMS = ("youtube", "instagram", "tiktok", "x")
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "x": "X"}

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE = re.compile(r"^(?:www\.|m\.|mobile\.)?(?:youtube\.com|youtu\.be|instagram\.com|tiktok\.com|x\.com|twitter\.com)/",
                   re.IGNORECASE)


@dataclass(frozen=True)
class Link:
    platform: str  # youtube | instagram | tiktok | x | other
    external_id: str  # "" when the URL is not a single video
    url: str  # cleaned canonical URL
    original: str

    @property
    def trackable(self) -> bool:
        return self.platform in PLATFORMS and bool(self.external_id)


def find_urls(text: str) -> list[str]:
    """URLs inside a spreadsheet cell (a cell may hold one, several, or a bare 'youtube.com/shorts/..')."""
    found = _URL.findall(text or "")
    if not found and _BARE.match((text or "").strip()):
        found = ["https://" + text.strip()]
    return [u.rstrip(".,;)") for u in found]


def _host(netloc: str) -> str:
    host = netloc.lower().split("@")[-1].split(":")[0]
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    return host


def parse_link(raw: str) -> Link:
    original = (raw or "").strip()
    parsed = urlparse(original if "://" in original else "https://" + original)
    host, parts = _host(parsed.netloc), [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"):
        video_id = ""
        if host == "youtu.be" and parts:
            video_id = parts[0]
        elif parts and parts[0] in ("shorts", "live", "embed", "v") and len(parts) > 1:
            video_id = parts[1]
        elif parts and parts[0] == "watch":
            video_id = (query.get("v") or [""])[0]
        video_id = video_id if _YT_ID.match(video_id) else ""
        return Link("youtube", video_id, f"https://www.youtube.com/shorts/{video_id}" if video_id else original, original)

    if host in ("instagram.com", "instagr.am"):
        code = ""
        for i, part in enumerate(parts):
            if part in ("reel", "reels", "p", "tv") and i + 1 < len(parts):
                code = parts[i + 1]
                break
        code = code if re.match(r"^[A-Za-z0-9_-]+$", code) else ""
        return Link("instagram", code, f"https://www.instagram.com/reel/{code}/" if code else original, original)

    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        if "video" in parts and parts.index("video") + 1 < len(parts) and parts[0].startswith("@"):
            video_id = parts[parts.index("video") + 1]
            if video_id.isdigit():
                return Link("tiktok", video_id, f"https://www.tiktok.com/{parts[0]}/video/{video_id}", original)
        short = parts[1] if parts[:1] == ["t"] and len(parts) > 1 else (parts[0] if host in ("vm.tiktok.com", "vt.tiktok.com") and parts else "")
        if short:  # short share links are resolved by the downloader; the code identifies them
            return Link("tiktok", f"short:{short}", f"https://{host}/{short}/" if host != "tiktok.com" else f"https://www.tiktok.com/t/{short}/", original)
        return Link("tiktok", "", original, original)

    if host in ("x.com", "twitter.com"):
        if "status" in parts and parts.index("status") + 1 < len(parts):
            status_id = parts[parts.index("status") + 1]
            user = parts[0] if parts[0] not in ("i", "status") and parts.index("status") > 0 else "i"
            if status_id.isdigit():
                return Link("x", status_id, f"https://x.com/{user}/status/{status_id}", original)
        return Link("x", "", original, original)

    return Link("other", "", original, original)
