from datetime import datetime, timedelta, timezone

import httpx
import openpyxl
import pytest

from habit10x_marketing_automation import social
from habit10x_marketing_automation.config import Settings
from habit10x_marketing_automation.social import (FetchError, Stats, add_links, classify_error, fetch_youtube_api,
                                                  import_folder, read_excel, refresh)
from habit10x_marketing_automation.social_links import find_urls, parse_link
from habit10x_marketing_automation.store import Store

UTC = timezone.utc
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def make_xlsx(path, rows, header=("URL",)):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if header:
        sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    return path


# ------------------------------------------------------------------------------------ link parsing
@pytest.mark.parametrize("raw, platform, external_id, url", [
    ("https://youtube.com/shorts/IPGz8mYuFi0?si=Wub6iUJDB92GR_Z8", "youtube", "IPGz8mYuFi0", "https://www.youtube.com/shorts/IPGz8mYuFi0"),
    ("https://youtu.be/IPGz8mYuFi0?t=3", "youtube", "IPGz8mYuFi0", "https://www.youtube.com/shorts/IPGz8mYuFi0"),
    ("https://www.youtube.com/watch?v=IPGz8mYuFi0&feature=share", "youtube", "IPGz8mYuFi0", "https://www.youtube.com/shorts/IPGz8mYuFi0"),
    ("https://m.youtube.com/shorts/IPGz8mYuFi0", "youtube", "IPGz8mYuFi0", "https://www.youtube.com/shorts/IPGz8mYuFi0"),
    ("https://www.instagram.com/reel/DgIIAmMxKd8/?gwpx=MTM0MXozcHlqd21ocg==", "instagram", "DgIIAmMxKd8", "https://www.instagram.com/reel/DgIIAmMxKd8/"),
    ("https://www.instagram.com/someone/reel/DgIIAmMxKd8/?igsh=abc", "instagram", "DgIIAmMxKd8", "https://www.instagram.com/reel/DgIIAmMxKd8/"),
    ("https://instagram.com/p/DgIIAmMxKd8", "instagram", "DgIIAmMxKd8", "https://www.instagram.com/reel/DgIIAmMxKd8/"),
    ("https://www.tiktok.com/@some.user/video/7059698374567611694?lang=en", "tiktok", "7059698374567611694", "https://www.tiktok.com/@some.user/video/7059698374567611694"),
    ("https://vm.tiktok.com/ZMabc123/", "tiktok", "short:ZMabc123", "https://vm.tiktok.com/ZMabc123/"),
    ("https://x.com/jack/status/20?s=46", "x", "20", "https://x.com/jack/status/20"),
    ("https://twitter.com/jack/status/20/video/1", "x", "20", "https://x.com/jack/status/20"),
    ("https://mobile.twitter.com/jack/status/20", "x", "20", "https://x.com/jack/status/20"),
])
def test_every_link_shape_resolves_to_one_canonical_video(raw, platform, external_id, url):
    link = parse_link(raw)
    assert (link.platform, link.external_id, link.url, link.trackable) == (platform, external_id, url, True)


@pytest.mark.parametrize("raw", [
    "https://www.instagram.com/someone/", "https://www.youtube.com/@channel", "https://www.youtube.com/watch?v=short",
    "https://x.com/jack", "https://www.tiktok.com/@someone", "https://example.com/video/1", "not a link",
])
def test_profile_pages_and_other_sites_are_not_trackable(raw):
    assert parse_link(raw).trackable is False


def test_find_urls_handles_several_links_bare_domains_and_trailing_punctuation():
    assert find_urls("see https://x.com/a/status/1, and https://youtu.be/IPGz8mYuFi0.") == [
        "https://x.com/a/status/1", "https://youtu.be/IPGz8mYuFi0"]
    assert find_urls("youtube.com/shorts/IPGz8mYuFi0") == ["https://youtube.com/shorts/IPGz8mYuFi0"]
    assert find_urls("just words") == []


# ------------------------------------------------------------------------------------ Excel import
def test_excel_import_dedupes_keeps_other_columns_and_reports_skipped(tmp_path):
    path = make_xlsx(tmp_path / "raw.xlsx", [
        ("https://youtube.com/shorts/IPGz8mYuFi0?si=x", "Diwali"),
        ("https://youtu.be/IPGz8mYuFi0", "same video, other shape"),
        ("https://www.instagram.com/reel/DgIIAmMxKd8/", "Launch"),
        ("https://www.tiktok.com/@a/video/123", ""),
        ("https://x.com/jack/status/20", ""),
        ("https://www.instagram.com/someone/", "profile"),
        ("https://example.com/watch", ""),
        (None, None),
    ], header=("URL", "Campaign"))
    items, skipped = read_excel(path)
    assert [(i["platform"], i["external_id"]) for i in items] == [
        ("youtube", "IPGz8mYuFi0"), ("instagram", "DgIIAmMxKd8"), ("tiktok", "123"), ("x", "20")]
    assert items[0]["meta"] == {"Campaign": "Diwali"}
    assert len(skipped) == 2 and "row 7" in skipped[0] and "single video" in skipped[0] and "not a YouTube" in skipped[1]


def test_excel_without_header_with_embedded_hyperlinks_and_many_links_in_a_cell(tmp_path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet["A1"] = "watch this"
    sheet["A1"].hyperlink = "https://youtu.be/IPGz8mYuFi0"
    sheet["A2"] = "https://x.com/a/status/1  https://x.com/b/status/2"
    workbook.save(tmp_path / "plain.xlsx")
    items, skipped = read_excel(tmp_path / "plain.xlsx")
    assert [i["external_id"] for i in items] == ["IPGz8mYuFi0", "1", "2"] and skipped == []


def test_importing_twice_adds_nothing_new_and_a_removed_video_comes_back(tmp_path):
    store = Store(":memory:")
    make_xlsx(tmp_path / "a.xlsx", [("https://youtu.be/IPGz8mYuFi0",)])
    first, second = import_folder(store, tmp_path), import_folder(store, tmp_path)
    assert (first.added, second.added, second.existing) == (1, 0, 1)
    video = store.social_videos()[0]
    store.remove_social_video(video["id"])
    assert store.social_videos() == []
    assert import_folder(store, tmp_path).added == 1 and len(store.social_videos()) == 1


def test_folder_import_ignores_excel_lock_files_and_other_files(tmp_path):
    store = Store(":memory:")
    make_xlsx(tmp_path / "raw.xlsx", [("https://youtu.be/IPGz8mYuFi0",)])
    (tmp_path / "~$raw.xlsx").write_bytes(b"lock")
    (tmp_path / "notes.txt").write_text("https://youtu.be/EG6MSPZGTiY")
    result = import_folder(store, tmp_path)
    assert result.files == ["raw.xlsx"] and result.added == 1


def test_pasted_links_are_added_and_bad_ones_reported():
    store = Store(":memory:")
    result = add_links(store, "https://x.com/a/status/1\nhttps://www.instagram.com/someone/")
    assert (result.added, len(result.skipped)) == (1, 1)


# ----------------------------------------------------------------------------------- error wording
@pytest.mark.parametrize("message, kind", [
    ("[TikTok] 1: Unable to download webpage: Connection to www.tiktok.com timed out", "network"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "blocked"),
    ("HTTP Error 429: Too Many Requests", "blocked"),
    ("[twitter] 1: No video could be found in this tweet", "unavailable"),
    ("Video unavailable. This video is private", "unavailable"),
    ("something odd happened", "other"),
])
def test_errors_become_short_friendly_messages(message, kind):
    error = classify_error(Exception(message), "tiktok")
    assert error.kind == kind and len(error.message) < 200


# ----------------------------------------------------------------------------------- yt-dlp mapping
class FakeYDL:
    info: dict = {}
    raises: Exception | None = None

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if FakeYDL.raises:
            raise FakeYDL.raises
        return FakeYDL.info


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.info, FakeYDL.raises = {}, None
    monkeypatch.setattr(social.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_ytdlp_numbers_are_mapped_and_missing_ones_stay_none(fake_ydl):
    fake_ydl.info = {"view_count": None, "like_count": 930697, "comment_count": 1785, "title": "Video by someone",
                     "description": "Great goal!\nsecond line", "uploader": "Some Club", "upload_date": "20261124"}
    stats = social.fetch_ytdlp(parse_link("https://www.instagram.com/reel/DgIIAmMxKd8/"), Settings())
    assert (stats.views, stats.likes, stats.comments) == (None, 930697, 1785)
    assert stats.title == "Great goal!" and stats.author == "Some Club" and stats.posted_at == "2026-11-24"


def test_ytdlp_playlist_posts_use_the_first_video_and_options_carry_proxy_only_for_tiktok(fake_ydl):
    fake_ydl.info = {"_type": "playlist", "entries": [{"view_count": 5, "like_count": 2}, {"view_count": 9}]}
    seen = []
    original_init = FakeYDL.__init__
    FakeYDL.__init__ = lambda self, options: (seen.append(options), original_init(self, options))[1]
    settings = Settings(social_proxy="http://proxy:8080", social_cookies_file="c.txt")
    assert social.fetch_ytdlp(parse_link("https://x.com/a/status/1"), settings).views == 5
    social.fetch_ytdlp(parse_link("https://www.tiktok.com/@a/video/1"), settings)
    FakeYDL.__init__ = original_init
    assert "proxy" not in seen[0] and seen[1]["proxy"] == "http://proxy:8080" and seen[0]["cookiefile"] == "c.txt"


def test_ytdlp_failure_raises_a_classified_error(fake_ydl):
    fake_ydl.raises = Exception("Connection to www.tiktok.com timed out")
    with pytest.raises(FetchError) as caught:
        social.fetch_ytdlp(parse_link("https://www.tiktok.com/@a/video/1"), Settings())
    assert caught.value.kind == "network" and "TikTok" in caught.value.message


# ----------------------------------------------------------------------------------- YouTube API
def api_transport(handler):
    return httpx.MockTransport(handler)


def test_youtube_api_reads_stats_hidden_numbers_and_missing_videos():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "KEY" and request.url.params["id"] == "aaaaaaaaaaa,bbbbbbbbbbb,ccccccccccc"
        return httpx.Response(200, json={"items": [
            {"id": "aaaaaaaaaaa", "snippet": {"title": "A", "channelTitle": "Chan", "publishedAt": "2026-11-01T10:00:00Z"},
             "statistics": {"viewCount": "1200", "likeCount": "30", "commentCount": "4"}},
            {"id": "bbbbbbbbbbb", "snippet": {"title": "B"}, "statistics": {"viewCount": "7"}}]})  # likes hidden

    result = fetch_youtube_api(["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"], "KEY", api_transport(handler))
    assert (result["aaaaaaaaaaa"].views, result["aaaaaaaaaaa"].likes, result["aaaaaaaaaaa"].comments) == (1200, 30, 4)
    assert result["aaaaaaaaaaa"].posted_at == "2026-11-01" and result["aaaaaaaaaaa"].source == "youtube-api"
    assert result["bbbbbbbbbbb"].likes is None and result["bbbbbbbbbbb"].views == 7
    assert result["ccccccccccc"].kind == "unavailable"


def test_youtube_api_errors_never_leak_the_key():
    refused = fetch_youtube_api(["aaaaaaaaaaa"], "SECRETKEY", api_transport(
        lambda r: httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})))
    assert refused["aaaaaaaaaaa"].kind == "blocked" and "quotaExceeded" in refused["aaaaaaaaaaa"].message
    assert "SECRETKEY" not in refused["aaaaaaaaaaa"].message

    def boom(request):
        raise httpx.ConnectError("failed for " + str(request.url))

    down = fetch_youtube_api(["aaaaaaaaaaa"], "SECRETKEY", api_transport(boom))
    assert down["aaaaaaaaaaa"].kind == "network" and "SECRETKEY" not in down["aaaaaaaaaaa"].message


# --------------------------------------------------------------------------------------- refresh
def seeded(store: Store, *links: str) -> None:
    items = [{"platform": (l := parse_link(u)).platform, "external_id": l.external_id, "url": l.url, "original": u,
              "meta": {}} for u in links]
    store.add_social_videos(items, T0 - timedelta(days=1))


def test_refresh_stores_a_snapshot_and_fills_in_title_and_author():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1")
    result = refresh(Settings(), store, now=T0, delay=0,
                     fetcher=lambda link: Stats(views=10, likes=2, comments=1, title="Hello", author="Ann", source="t"))
    video = store.social_overview()[0]
    assert (result.ok, result.failed) == (1, 0)
    assert (video["latest"]["views"], video["latest"]["likes"], video["title"], video["author"]) == (10, 2, "Hello", "Ann")


def test_a_second_check_gives_a_previous_value_and_a_history():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1")
    for i, views in enumerate((100, 160)):
        refresh(Settings(), store, now=T0 + timedelta(hours=i), delay=0, fetcher=lambda l, v=views: Stats(views=v))
    video = store.social_overview()[0]
    assert (video["latest"]["views"], video["previous"]["views"]) == (160, 100)
    assert [h["views"] for h in store.social_history(video["id"])] == [100, 160]


def test_a_failed_check_is_kept_with_its_reason_and_does_not_hide_the_last_good_numbers():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1")
    refresh(Settings(), store, now=T0, delay=0, fetcher=lambda l: Stats(views=50))

    def fail(link):
        raise FetchError("blocked", "X asked for a login")

    result = refresh(Settings(), store, now=T0 + timedelta(hours=1), delay=0, fetcher=fail)
    video = store.social_overview()[0]
    assert result.failed == 1 and result.errors == {"x": "X asked for a login"}
    assert video["latest"]["views"] == 50 and video["attempt"]["ok"] == 0 and video["attempt"]["error"] == "X asked for a login"


def test_an_unreachable_platform_is_skipped_after_two_failures_instead_of_waiting_for_each_video():
    store = Store(":memory:")
    seeded(store, *[f"https://www.tiktok.com/@a/video/{i}" for i in range(1, 7)], "https://x.com/a/status/1")
    calls = []

    def fetch(link):
        calls.append(link.platform)
        if link.platform == "tiktok":
            raise FetchError("network", "Could not reach TikTok from this network (blocked or offline)")
        return Stats(views=1)

    result = refresh(Settings(), store, now=T0, delay=0, fetcher=fetch)
    assert calls.count("tiktok") == 2  # then the other four were skipped, not tried
    assert result.by_platform == {"tiktok": {"ok": 0, "failed": 6}, "x": {"ok": 1, "failed": 0}}
    assert all(v["attempt"]["error"].startswith("Could not reach TikTok") for v in store.social_overview() if v["platform"] == "tiktok")


def test_due_hours_only_checks_stale_videos_and_never_checked_ones_go_first():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1", "https://x.com/a/status/2", "https://x.com/a/status/3")
    ids = {v["external_id"]: v["id"] for v in store.social_videos()}
    store.add_social_stat(ids["1"], T0 - timedelta(hours=1), views=1)  # fresh
    store.add_social_stat(ids["2"], T0 - timedelta(hours=9), views=1)  # stale
    order = []
    refresh(Settings(), store, now=T0, due_hours=6, delay=0, fetcher=lambda l: order.append(l.external_id) or Stats(views=2))
    assert order == ["3", "2"]  # never-checked first, then the stalest; the fresh one is left alone


def test_limit_and_platform_filters():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1", "https://x.com/a/status/2", "https://youtu.be/IPGz8mYuFi0")
    seen = []
    refresh(Settings(), store, platforms=["x"], limit=1, now=T0, delay=0, fetcher=lambda l: seen.append(l.url) or Stats())
    assert len(seen) == 1 and "x.com" in seen[0]


def test_youtube_uses_the_api_in_one_batch_and_falls_back_to_scraping_when_the_api_fails():
    store = Store(":memory:")
    seeded(store, "https://youtu.be/aaaaaaaaaaa", "https://youtu.be/bbbbbbbbbbb", "https://youtu.be/ccccccccccc")
    scraped = []

    def api(ids):
        assert ids == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]  # a single call for all of them
        return {"aaaaaaaaaaa": Stats(views=5, source="youtube-api"),
                "bbbbbbbbbbb": FetchError("blocked", "quota"),
                "ccccccccccc": FetchError("unavailable", "Video unavailable (private, removed, or not a video)")}

    result = refresh(Settings(), store, now=T0, delay=0, youtube_api=api,
                     fetcher=lambda l: scraped.append(l.external_id) or Stats(views=9, source="yt-dlp"))
    assert scraped == ["bbbbbbbbbbb"]  # only the one the API could not answer; the removed one is not retried
    by_id = {v["external_id"]: v for v in store.social_overview()}
    assert by_id["aaaaaaaaaaa"]["latest"]["views"] == 5 and by_id["bbbbbbbbbbb"]["latest"]["views"] == 9
    assert by_id["ccccccccccc"]["attempt"]["ok"] == 0
    assert (result.ok, result.failed) == (2, 1)


def test_an_unexpected_crash_on_one_video_does_not_stop_the_others():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1", "https://x.com/a/status/2")

    def fetch(link):
        if link.external_id == "1":
            raise RuntimeError("boom")
        return Stats(views=3)

    result = refresh(Settings(), store, now=T0, delay=0, fetcher=fetch)
    assert (result.ok, result.failed) == (1, 1)


# --------------------------------------------------------------------------- thresholds (views/likes/comments)
from habit10x_marketing_automation.emails import render_alert  # noqa: E402
from habit10x_marketing_automation.engine import Firing  # noqa: E402
from habit10x_marketing_automation.mailer import MailError  # noqa: E402
from habit10x_marketing_automation.rules import SOCIAL_RULE, Event  # noqa: E402
from habit10x_marketing_automation.social import check_social_alerts, find_crossings, since_key, threshold_key  # noqa: E402
from habit10x_marketing_automation.store import RuleConfig  # noqa: E402

ALERT_SETTINGS = Settings(alert_recipients="owner@x.com", alert_from_email="a@x.com")


def tracked(store: Store, url: str = "https://youtu.be/IPGz8mYuFi0", **series) -> int:
    """Track a video and record one successful check per hour for the given numbers, e.g. likes=[40, 90, 130]."""
    seeded(store, url)
    video_id = store.social_videos()[-1]["id"]
    length = max(len(v) for v in series.values())
    for hour in range(length):
        numbers = {m: (series[m][hour] if m in series else None) for m in ("views", "likes", "comments")}
        store.add_social_stat(video_id, T0 + timedelta(hours=hour), **numbers)
    return video_id


class Sender:
    def __init__(self, fail: bool = False):
        self.calls, self.fail = [], fail

    def __call__(self, settings, subject, html, text, to=None, **kw):
        if self.fail:
            raise MailError("535 bad credentials")
        self.calls.append({"subject": subject, "to": to, "text": text})


def test_a_video_is_announced_when_its_likes_pass_the_threshold_between_two_checks():
    store = Store(":memory:")
    tracked(store, likes=[40, 90, 130])
    found = find_crossings(store, {"likes": 100})
    assert [(c.metric, c.value, c.before, c.threshold) for c in found] == [("likes", 130, 90, 100)]
    assert found[0].ts == T0 + timedelta(hours=2)


def test_a_video_already_above_the_number_when_first_seen_is_not_announced():
    store = Store(":memory:")
    tracked(store, likes=[500, 600, 900])
    assert find_crossings(store, {"likes": 100}) == []


def test_views_likes_and_comments_each_have_their_own_threshold():
    store = Store(":memory:")
    tracked(store, views=[10, 2000], likes=[5, 50], comments=[1, 20])
    found = find_crossings(store, {"views": 1000, "likes": 100, "comments": 10})
    assert sorted(c.metric for c in found) == ["comments", "views"]  # likes only reached 50
    assert find_crossings(store, {"views": 0, "likes": 0, "comments": 0}) == []  # 0 = off


def test_numbers_a_platform_does_not_show_never_trigger_and_gaps_are_skipped():
    store = Store(":memory:")
    tracked(store, "https://www.instagram.com/reel/DgIIAmMxKd8/", likes=[None, 40, 150])  # no views at all
    assert find_crossings(store, {"views": 1}) == []
    found = find_crossings(store, {"likes": 100})
    assert [(c.before, c.value) for c in found] == [(40, 150)]  # the blank check is ignored, not counted as 0


def test_changing_a_threshold_only_counts_passes_after_the_change():
    store = Store(":memory:")
    tracked(store, likes=[40, 130])  # passed 100 at T0 + 1h
    assert find_crossings(store, {"likes": 100}, {"likes": T0 + timedelta(hours=2)}) == []
    assert len(find_crossings(store, {"likes": 100}, {"likes": T0})) == 1


def test_a_video_is_announced_once_per_threshold_and_again_when_a_new_threshold_is_passed():
    store = Store(":memory:")
    video_id = tracked(store, likes=[40, 130, 90, 140, 1500])
    store.add_social_alert(video_id, "likes", 100, T0)
    assert find_crossings(store, {"likes": 100}) == []  # wobbling around 100 does not repeat
    assert [c.value for c in find_crossings(store, {"likes": 1000})] == [1500]  # a higher threshold is new


def test_the_alert_is_recorded_while_emails_are_off_and_not_repeated():
    store = Store(":memory:")
    tracked(store, likes=[40, 130])
    store.set_setting(threshold_key("likes"), "100")
    sender = Sender()
    row = check_social_alerts(ALERT_SETTINGS, store, now=T0 + timedelta(hours=3), sender=sender)
    assert row["status"] == "recorded" and sender.calls == []
    assert store.alerts(1)[0]["rule_id"] == "social_traction"
    assert check_social_alerts(ALERT_SETTINGS, store, now=T0 + timedelta(hours=4), sender=sender) is None


def test_the_alert_emails_one_message_listing_every_video_and_metric_that_passed():
    store = Store(":memory:")
    tracked(store, "https://youtu.be/IPGz8mYuFi0", views=[100, 5000], likes=[10, 20])
    tracked(store, "https://www.instagram.com/reel/DgIIAmMxKd8/", likes=[90, 400])
    for metric, value in (("views", 1000), ("likes", 100)):
        store.set_setting(threshold_key(metric), str(value))
    store.set_setting("email_enabled", "1")
    sender = Sender()
    row = check_social_alerts(ALERT_SETTINGS, store, now=T0 + timedelta(hours=3), sender=sender)
    assert row["status"] == "sent" and len(sender.calls) == 1  # one email, not one per video
    assert sender.calls[0]["to"] == ["owner@x.com"] and "2 video milestones (likes, views)" in sender.calls[0]["subject"]
    assert "5,000 views" in sender.calls[0]["text"] and "400 likes" in sender.calls[0]["text"]


def test_a_failed_email_is_retried_at_the_next_check_and_the_cap_still_applies():
    store = Store(":memory:")
    tracked(store, likes=[40, 130])
    store.set_setting(threshold_key("likes"), "100")
    store.set_setting("email_enabled", "1")
    assert check_social_alerts(ALERT_SETTINGS, store, now=T0 + timedelta(hours=3), sender=Sender(fail=True))["status"] == "failed"
    good = Sender()
    assert check_social_alerts(ALERT_SETTINGS, store, now=T0 + timedelta(hours=4), sender=good)["status"] == "sent"
    other = Store(":memory:")
    tracked(other, likes=[40, 130])
    other.set_setting(threshold_key("likes"), "100")
    other.set_setting("email_enabled", "1")
    other.set_setting("daily_cap", "1")
    other.add_alert(fired_at=T0 + timedelta(hours=3), rule_id="x", entity="", count=1, threshold=1, window_minutes=1,
                    status="sent", subject="earlier email")
    assert check_social_alerts(ALERT_SETTINGS, other, now=T0 + timedelta(hours=3), sender=Sender())["status"] == "capped"


def test_a_refresh_that_makes_a_video_pass_the_threshold_raises_the_alert():
    store = Store(":memory:")
    seeded(store, "https://x.com/a/status/1")
    store.set_setting(threshold_key("likes"), "100")
    refresh(ALERT_SETTINGS, store, now=T0, delay=0, fetcher=lambda l: Stats(likes=60))
    first = refresh(ALERT_SETTINGS, store, now=T0 + timedelta(hours=1), delay=0, fetcher=lambda l: Stats(likes=60))
    assert first.alert is None
    second = refresh(ALERT_SETTINGS, store, now=T0 + timedelta(hours=2), delay=0, fetcher=lambda l: Stats(likes=140))
    assert second.alert["status"] == "recorded" and "100 likes" in second.alert["subject"]


def test_the_alert_email_names_the_video_the_metric_and_both_numbers():
    info = {"platform": "Instagram", "title": "<b>Big</b> goal <script>x</script>", "video_url": "https://www.instagram.com/reel/A/",
            "metric": "likes", "value": 1200, "before": 950, "threshold": 1000}
    firing = Firing("social_traction", "social", 1, [Event(T0, "social", "1:likes", info)], T0)
    subject, html_body, text = render_alert(SOCIAL_RULE, RuleConfig("social_traction", True, 1000, 0, 0), firing, Settings())
    assert subject.startswith("Video reached 1,000 likes: ") and "(Instagram)" in subject
    assert "1,200 likes (threshold 1,000; was 950" in text and "Open video: https://www.instagram.com/reel/A/" in text
    assert "<script>" not in html_body and 'href="https://www.instagram.com/reel/A/"' in html_body


# ----------------------------------------------------------------------------------- dashboard view
from pathlib import Path  # noqa: E402

DASHBOARD = Path(__file__).resolve().parents[1] / "src" / "habit10x_marketing_automation" / "dashboard.py"


def open_tracker():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(DASHBOARD), default_timeout=60).run()
    at.radio(key="view").set_value("Social tracker").run()
    assert not at.exception, at.exception
    return at


def press(at, label: str):
    next(b for b in at.button if b.label == label).click().run()


def seed_dashboard_state() -> Store:
    store = Store(Settings().state_path)
    seeded(store, "https://youtu.be/IPGz8mYuFi0", "https://www.instagram.com/reel/DgIIAmMxKd8/", "https://x.com/a/status/1")
    ids = {v["platform"]: v["id"] for v in store.social_videos()}
    store.add_social_stat(ids["youtube"], T0, views=1000, likes=50, comments=5)
    store.add_social_stat(ids["youtube"], T0 + timedelta(hours=6), views=1500, likes=80, comments=9)
    store.add_social_stat(ids["instagram"], T0, views=None, likes=900, comments=40)
    store.add_social_stat(ids["x"], T0, ok=False, error="X asked for a login or is limiting requests.")
    store.update_social_video_info(ids["youtube"], "Great short", "Chan", "2026-11-01")
    return store


def test_tracker_empty_state_points_to_the_input_folder_and_cannot_refresh():
    at = open_tracker()
    assert any("No videos yet" in i.value and "input" in i.value for i in at.info)
    assert next(b for b in at.button if b.label == "Refresh now").disabled


def test_tracker_imports_the_input_folder_and_lists_the_videos(tmp_path, monkeypatch):
    folder = tmp_path / "in"
    folder.mkdir()
    make_xlsx(folder / "raw.xlsx", [("https://youtu.be/IPGz8mYuFi0",), ("https://www.instagram.com/reel/DgIIAmMxKd8/",),
                                    ("https://www.instagram.com/someone/",)])
    monkeypatch.setenv("SOCIAL_INPUT_DIR", str(folder))
    at = open_tracker()
    press(at, "Import from input folder")
    assert not at.exception, at.exception
    assert any("Added 2" in w.value and "Skipped 1" in w.value for w in at.warning)
    table = at.dataframe[0].value
    assert sorted(table["Platform"]) == ["Instagram", "YouTube"] and set(table["Status"]) == {"Not checked yet"}
    assert next(b for b in at.button if b.label == "Refresh now").disabled is False


def test_tracker_shows_numbers_totals_and_failures_without_a_per_video_detail_section():
    seed_dashboard_state()
    at = open_tracker()
    assert not at.exception, at.exception
    totals = {m.label: m.value for m in at.metric}
    assert totals == {"Videos": "3", "Views": "1,500", "Likes": "980", "Comments": "49"}  # latest ok numbers only
    table = at.dataframe[0].value.set_index("Platform")
    assert (table.loc["YouTube", "Views"], table.loc["YouTube", "Likes"]) == (1500, 80)
    assert table.loc["Instagram", "Views"] != table.loc["Instagram", "Views"]  # blank (NaN): Instagram hides views
    assert table.loc["X", "Status"].startswith("Failed: X asked for a login")
    assert not any("Look at one video" in s.label for s in at.selectbox)  # that section is gone


def test_tracker_platform_filter_changes_the_table_and_totals():
    seed_dashboard_state()
    at = open_tracker()
    at.multiselect[0].set_value(["YouTube"]).run()
    assert list(at.dataframe[0].value["Platform"]) == ["YouTube"]
    assert at.metric[0].value == "1" and at.metric[1].value == "1,500"


def test_stop_tracking_removes_the_chosen_video():
    store = seed_dashboard_state()
    at = open_tracker()
    press(at, "Stop tracking")  # the first video (YouTube) is preselected
    assert not at.exception, at.exception
    assert [v["platform"] for v in store.social_videos()] == ["instagram", "x"]


def test_refresh_interval_is_an_editable_field_on_the_tracker_page_and_saves_by_itself():
    at = open_tracker()
    assert at.number_input(key="social_hours").value == 6.0  # default
    at.number_input(key="social_hours").set_value(2.0).run()
    assert Store(Settings().state_path).get_setting("social_refresh_hours") == "2.0"
    at.number_input(key="social_hours").set_value(0.5).run()
    assert Store(Settings().state_path).get_setting("social_refresh_hours") == "0.5"  # fractions work too


def test_one_threshold_field_each_for_views_likes_and_comments_saves_by_itself():
    at = open_tracker()
    assert [at.number_input(key=f"social_{m}").value for m in ("views", "likes", "comments")] == [0, 0, 0]  # off
    at.number_input(key="social_likes").set_value(1000).run()
    at.number_input(key="social_views").set_value(50000).run()
    at.number_input(key="social_comments").set_value(100).run()
    store = Store(Settings().state_path)
    assert [store.get_setting(threshold_key(m)) for m in ("views", "likes", "comments")] == ["50000", "1000", "100"]
    assert all(store.get_setting(since_key(m)) for m in ("views", "likes", "comments"))  # only count passes from now on
    first_since = store.get_setting(since_key("likes"))
    at.number_input(key="social_likes").set_value(1000).run()  # unchanged: the starting point must not move
    assert Store(Settings().state_path).get_setting(since_key("likes")) == first_since


def test_the_page_warns_when_the_checker_that_does_the_automatic_work_is_not_running():
    at = open_tracker()
    assert any("only run while the checker is running" in w.value for w in at.warning)


def test_history_names_the_social_alert():
    store = Store(Settings().state_path)
    store.add_alert(fired_at=T0, rule_id="social_traction", entity="social", count=1, threshold=100,
                    window_minutes=0, status="recorded", subject="Video reached 100 likes: x")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(DASHBOARD), default_timeout=60).run()
    at.radio(key="view").set_value("History").run()
    assert not at.exception, at.exception
    assert at.dataframe[0].value["alert"].iloc[0] == "Video reached a threshold"


def test_the_automatic_import_never_brings_back_a_video_the_user_removed(tmp_path):
    store = Store(":memory:")
    make_xlsx(tmp_path / "a.xlsx", [("https://youtu.be/IPGz8mYuFi0",)])
    import_folder(store, tmp_path)
    store.remove_social_video(store.social_videos()[0]["id"])
    assert import_folder(store, tmp_path, reactivate=False).added == 0 and store.social_videos() == []
    assert import_folder(store, tmp_path).added == 1  # the Import button does bring it back
