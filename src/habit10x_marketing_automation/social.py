"""Social media tracker: read video links from Excel, fetch views / likes / comments, store a snapshot each time.

YouTube can use the official API (set YOUTUBE_API_KEY): reliable and works from a server. Everything else, and
YouTube without a key, goes through yt-dlp, which reads the public page of the video.
"""

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import openpyxl
import yt_dlp

from .config import Settings
from .engine import Firing, Runtime, dispatch
from .mailer import send_email
from .rules import SOCIAL_RULE, Event
from .social_links import PLATFORM_NAMES, Link, find_urls, parse_link
from .store import RuleConfig, Store, parse_ts

UTC = timezone.utc
EXCEL_SUFFIXES = (".xlsx", ".xlsm")
REFRESH_HOURS = "social_refresh_hours"  # 0 = off; fractions allowed (0.5 = every 30 minutes)
METRICS = ("views", "likes", "comments")


def threshold_key(metric: str) -> str:
    """Setting holding the one threshold (for every platform) of a metric; 0 = off."""
    return f"social_{metric}_threshold"


def since_key(metric: str) -> str:
    """Setting holding when the threshold was last changed: only passes after that moment are announced."""
    return f"social_{metric}_since"


# ------------------------------------------------------------------------------------ Excel import
@dataclass
class ImportResult:
    added: int = 0
    existing: int = 0
    skipped: list[str] = field(default_factory=list)  # reasons a link was not tracked
    files: list[str] = field(default_factory=list)


def _cell_text(cell) -> str:
    text = "" if cell.value is None else str(cell.value).strip()
    if cell.hyperlink and cell.hyperlink.target and not find_urls(text):
        return str(cell.hyperlink.target)  # the cell shows a label, the link is attached to it
    return text


def read_excel(path: Path) -> tuple[list[dict], list[str]]:
    """Every video link in the workbook. Other columns of the same row are kept as `meta` (campaign, notes...).

    Works with or without a header row, with any column order, and with several links in one cell.
    """
    workbook = openpyxl.load_workbook(path, data_only=True)
    items: list[dict] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    for sheet in workbook.worksheets:
        grid = [[_cell_text(c) for c in row] for row in sheet.iter_rows()]
        if not grid:
            continue
        has_header = not any(find_urls(t) for t in grid[0]) and any(grid[0])
        header = grid[0] if has_header else []
        for offset, row in enumerate(grid[1:] if has_header else grid):
            number = offset + (2 if has_header else 1)
            urls = [(i, u) for i, text in enumerate(row) for u in find_urls(text)]
            url_cols = {i for i, _ in urls}
            meta = {(header[i] if i < len(header) and header[i] else f"column {i + 1}"): text
                    for i, text in enumerate(row) if text and i not in url_cols}
            for _, raw in urls:
                link = parse_link(raw)
                if not link.trackable:
                    why = ("not a single video link (a profile or search page?)" if link.platform != "other"
                           else "not a YouTube, Instagram, TikTok or X link")
                    skipped.append(f"{path.name} row {number}: {raw[:60]} - {why}")
                    continue
                key = (link.platform, link.external_id)
                if key in seen:
                    continue
                seen.add(key)
                items.append({"platform": link.platform, "external_id": link.external_id, "url": link.url,
                              "original": link.original, "meta": meta})
    return items, skipped


def import_paths(store: Store, paths: list[Path], now: datetime | None = None, reactivate: bool = True) -> ImportResult:
    result = ImportResult()
    for path in paths:
        items, skipped = read_excel(path)
        added, existing = store.add_social_videos(items, now or datetime.now(UTC), reactivate)
        result.added += added
        result.existing += existing
        result.skipped += skipped
        result.files.append(path.name)
    return result


def import_folder(store: Store, folder: Path, now: datetime | None = None, reactivate: bool = True) -> ImportResult:
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in EXCEL_SUFFIXES and not p.name.startswith("~$"))
    return import_paths(store, files, now, reactivate)


def add_links(store: Store, text: str, now: datetime | None = None) -> ImportResult:
    """Links typed or pasted into the dashboard (one or many, separated by spaces or lines)."""
    result, items = ImportResult(), []
    for raw in find_urls(text):
        link = parse_link(raw)
        if link.trackable:
            items.append({"platform": link.platform, "external_id": link.external_id, "url": link.url,
                          "original": link.original, "meta": {}})
        else:
            result.skipped.append(f"{raw[:60]} - not a single YouTube, Instagram, TikTok or X video link")
    result.added, result.existing = store.add_social_videos(items, now or datetime.now(UTC))
    return result


# ----------------------------------------------------------------------------------------- fetching
@dataclass
class Stats:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    title: str = ""
    author: str = ""
    posted_at: str = ""
    source: str = ""


class FetchError(Exception):
    """kind: network | blocked | unavailable | other"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def classify_error(exc: Exception, platform: str) -> FetchError:
    text = str(exc).lower()
    name = PLATFORM_NAMES.get(platform, platform)
    if any(k in text for k in ("http error 404", "not found", "private", "removed", "unavailable", "does not exist",
                               "no video could be found", "deleted", "no video formats")):
        return FetchError("unavailable", "Video unavailable (private, removed, or not a video)")
    if any(k in text for k in ("login", "log in", "sign in", "cookies", "rate-limit", "rate limit", "http error 429",
                               "http error 403", "http error 401", "confirm you")):
        return FetchError("blocked", f"{name} asked for a login or is limiting requests. Try later, or set "
                                     "SOCIAL_COOKIES_FILE")
    if any(k in text for k in ("timed out", "timeout", "connection", "unable to download webpage", "getaddrinfo",
                               "name or service", "network is unreachable", "transporterror")):
        return FetchError("network", f"Could not reach {name} from this network (blocked or offline)")
    return FetchError("other", " ".join(str(exc).split())[:160] or "Unknown error")


class _Silent:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _num(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _posted(info: dict) -> str:
    if info.get("timestamp"):
        return datetime.fromtimestamp(int(info["timestamp"]), UTC).date().isoformat()
    day = str(info.get("upload_date") or "")
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 else ""


def _title(info: dict, platform: str) -> str:
    title = (info.get("title") or "").strip()
    caption = (info.get("description") or "").strip().splitlines()[0] if info.get("description") else ""
    chosen = caption if platform in ("instagram", "x") and caption else title or caption
    return chosen[:120]


def fetch_ytdlp(link: Link, settings: Settings) -> Stats:
    options: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                               "socket_timeout": 10, "retries": 1, "extractor_retries": 1, "logger": _Silent()}
    if settings.social_cookies_file:
        options["cookiefile"] = settings.social_cookies_file
    if link.platform == "tiktok" and settings.social_proxy:
        options["proxy"] = settings.social_proxy
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(link.url, download=False)
    except Exception as exc:  # noqa: BLE001 - turned into a short, friendly message
        raise classify_error(exc, link.platform) from None
    if info.get("_type") == "playlist":  # a post with several videos: use the first
        entries = [e for e in info.get("entries") or [] if e]
        info = entries[0] if entries else {}
    return Stats(views=_num(info.get("view_count")), likes=_num(info.get("like_count")),
                 comments=_num(info.get("comment_count")), shares=_num(info.get("repost_count")),
                 title=_title(info, link.platform), author=str(info.get("uploader") or info.get("channel") or ""),
                 posted_at=_posted(info), source="yt-dlp")


def fetch_youtube_api(ids: list[str], key: str, transport: httpx.BaseTransport | None = None
                      ) -> dict[str, Stats | FetchError]:
    """Official YouTube Data API: up to 50 videos per request. The key is never put into an error message."""
    out: dict[str, Stats | FetchError] = {}
    with httpx.Client(timeout=20, transport=transport) as client:
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                response = client.get("https://www.googleapis.com/youtube/v3/videos",
                                      params={"part": "snippet,statistics", "id": ",".join(chunk), "key": key})
            except httpx.HTTPError:
                out.update({v: FetchError("network", "Could not reach the YouTube API") for v in chunk})
                continue
            if response.status_code != 200:
                try:
                    reason = response.json()["error"]["errors"][0]["reason"]
                except Exception:  # noqa: BLE001
                    reason = ""
                error = FetchError("blocked", f"YouTube API refused the request ({response.status_code}"
                                              f"{', ' + reason if reason else ''})")
                out.update({v: error for v in chunk})
                continue
            items = {it["id"]: it for it in response.json().get("items", [])}
            for video_id in chunk:
                item = items.get(video_id)
                if item is None:
                    out[video_id] = FetchError("unavailable", "Video unavailable (private, removed, or not a video)")
                    continue
                stats, snippet = item.get("statistics", {}), item.get("snippet", {})
                number = lambda k: int(stats[k]) if str(stats.get(k, "")).isdigit() else None  # noqa: E731
                out[video_id] = Stats(views=number("viewCount"), likes=number("likeCount"),
                                      comments=number("commentCount"), title=str(snippet.get("title", ""))[:120],
                                      author=str(snippet.get("channelTitle", "")),
                                      posted_at=str(snippet.get("publishedAt", ""))[:10], source="youtube-api")
    return out


# ------------------------------------------------------------------- views / likes / comments alert
@dataclass
class Crossing:
    video: dict
    metric: str
    ts: datetime  # when the check that first saw the video at/above the threshold happened
    value: int
    before: int  # the number at the check just before
    threshold: int


def get_thresholds(store: Store) -> dict[str, int]:
    return {m: int(float(store.get_setting(threshold_key(m), "0") or 0)) for m in METRICS}


def find_crossings(store: Store, thresholds: dict[str, int],
                   since: dict[str, datetime | None] | None = None) -> list[Crossing]:
    """Videos whose views / likes / comments went from below their threshold to at/above it, not announced yet.

    A video that is already above the number the first time we see it is not announced (it did not "reach" it
    while we were watching). A video is announced once per metric and threshold, even if its numbers wobble.
    """
    since = since or {}
    active = {m: t for m, t in thresholds.items() if t > 0}
    if not active:
        return []
    done = {m: store.social_alerted(m, t) for m, t in active.items()}
    found: list[Crossing] = []
    for video in store.social_videos():
        history = store.social_history(video["id"])
        for metric, threshold in active.items():
            if video["id"] in done[metric]:
                continue
            series = [h for h in history if h[metric] is not None]
            for i, h in enumerate(series):
                if h[metric] >= threshold:
                    when = parse_ts(h["fetched_at"])
                    if i > 0 and (since.get(metric) is None or when >= since[metric]):
                        found.append(Crossing(video, metric, when, h[metric], series[i - 1][metric], threshold))
                    break
    return found


def check_social_alerts(settings: Settings, store: Store, *, now: datetime | None = None, sender=send_email,
                        dry_run: bool = False) -> dict | None:
    """One email (or one History entry while emails are OFF) listing every video that just passed a threshold."""
    thresholds = get_thresholds(store)
    if not any(thresholds.values()):
        return None
    since = {m: (parse_ts(text) if (text := store.get_setting(since_key(m), "")) else None) for m in METRICS}
    crossings = find_crossings(store, thresholds, since)
    if not crossings:
        return None
    now = now or datetime.now(UTC)
    events = [Event(c.ts, "social", f"{c.video['id']}:{c.metric}", {
        "platform": PLATFORM_NAMES[c.video["platform"]], "title": c.video["title"], "video_url": c.video["url"],
        "metric": c.metric, "value": c.value, "before": c.before, "threshold": c.threshold}) for c in crossings]
    distinct = {(c.metric, c.threshold) for c in crossings}
    config = RuleConfig(SOCIAL_RULE.id, True, next(iter(distinct))[1] if len(distinct) == 1 else 0, 0, 0)
    firing = Firing(SOCIAL_RULE.id, "social", len(events), sorted(events), max(e.ts for e in events))
    sent = [store.count_sent_since(now - timedelta(hours=24))]
    row = dispatch(store, settings, Runtime.load(settings, store), SOCIAL_RULE, config, firing, now, sent, sender,
                   dry_run)
    if not dry_run and row["status"] != "failed":  # a failed email is retried at the next check
        for c in crossings:
            store.add_social_alert(c.video["id"], c.metric, c.threshold, now)
    return row


# ----------------------------------------------------------------------------------------- refresh
@dataclass
class RefreshResult:
    checked: int = 0
    ok: int = 0
    failed: int = 0
    by_platform: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # platform -> a sample error
    alert: dict | None = None  # the threshold alert, if this check made a video pass one


def refresh(settings: Settings, store: Store, *, due_hours: float | None = None, platforms: list[str] | None = None,
            limit: int | None = None, now: datetime | None = None, delay: float = 1.0,
            fetcher: Callable[[Link], Stats] | None = None,
            youtube_api: Callable[[list[str]], dict[str, Stats | FetchError]] | None = None,
            on_result: Callable[[dict, Stats | FetchError], None] | None = None,
            sender=send_email, alerts: bool = True) -> RefreshResult:
    """Fetch current numbers and store a snapshot per video. Failures are stored too, so the dashboard can say why.

    `due_hours` only checks videos not tried in that long (what the background loop uses). Videos never checked
    come first, then the stalest. A platform that cannot be reached twice in a row is skipped for this run.
    """
    now = now or datetime.now(UTC)
    rows = store.social_overview()
    if platforms:
        rows = [r for r in rows if r["platform"] in platforms]
    if due_hours is not None:
        cutoff = now - timedelta(hours=due_hours) + timedelta(minutes=2)  # tolerance: a loop that wakes a bit early
        rows = [r for r in rows if r["attempt"] is None or parse_ts(r["attempt"]["fetched_at"]) <= cutoff]
    rows.sort(key=lambda r: r["attempt"]["fetched_at"] if r["attempt"] else "")
    if limit:
        rows = rows[:limit]

    fetch = fetcher or (lambda link: fetch_ytdlp(link, settings))
    key = settings.youtube_api_key.get_secret_value() if settings.youtube_api_key else ""
    api = youtube_api or ((lambda ids: fetch_youtube_api(ids, key)) if key else None)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["platform"]].append(row)

    def work(platform: str, videos: list[dict]) -> list[tuple[dict, Stats | FetchError]]:
        done: list[tuple[dict, Stats | FetchError]] = []
        pending = videos
        if platform == "youtube" and api:  # one request for up to 50 videos; fall back to scraping on failure
            answers = api([v["external_id"] for v in videos])
            pending = []
            for v in videos:
                answer = answers.get(v["external_id"])
                if isinstance(answer, Stats) or (isinstance(answer, FetchError) and answer.kind == "unavailable"):
                    done.append((v, answer))
                    if on_result:
                        on_result(v, answer)
                else:
                    pending.append(v)
        unreachable, streak = "", 0
        for i, v in enumerate(pending):
            if unreachable:
                result: Stats | FetchError = FetchError("network", unreachable)
            else:
                if i and delay:
                    time.sleep(delay)
                try:
                    result = fetch(Link(platform, v["external_id"], v["url"], v["original_url"]))
                except FetchError as exc:
                    result = exc
                except Exception as exc:  # noqa: BLE001 - one bad video must not stop the rest
                    result = FetchError("other", f"{type(exc).__name__}: {exc}"[:160])
                streak = streak + 1 if isinstance(result, FetchError) and result.kind == "network" else 0
                if streak >= 2:
                    unreachable = result.message  # type: ignore[union-attr]
            done.append((v, result))
            if on_result:
                on_result(v, result)
        return done

    summary = RefreshResult()
    if grouped:
        with ThreadPoolExecutor(max_workers=len(grouped)) as pool:
            futures = [pool.submit(work, platform, videos) for platform, videos in grouped.items()]
            outcomes = [pair for future in futures for pair in future.result()]
        for video, result in outcomes:  # written here, on one thread, so the database is never shared
            counts = summary.by_platform.setdefault(video["platform"], {"ok": 0, "failed": 0})
            summary.checked += 1
            if isinstance(result, Stats):
                store.add_social_stat(video["id"], now, views=result.views, likes=result.likes,
                                      comments=result.comments, shares=result.shares, ok=True, source=result.source)
                store.update_social_video_info(video["id"], result.title, result.author, result.posted_at)
                summary.ok += 1
                counts["ok"] += 1
            else:
                store.add_social_stat(video["id"], now, ok=False, error=result.message)
                summary.failed += 1
                counts["failed"] += 1
                summary.errors.setdefault(video["platform"], result.message)
    if alerts and summary.ok:  # the threshold alert looks at the numbers just stored
        try:
            summary.alert = check_social_alerts(settings, store, now=now, sender=sender)
        except Exception as exc:  # noqa: BLE001 - an alert problem must never undo or hide the refresh itself
            summary.errors["alert"] = f"{type(exc).__name__}: {exc}"[:160]
    return summary
