# Traction Alerts — Plan (v3, 2026-11-23)

**Goal:** email the owner/team **the moment** something real crosses a threshold on Habit10x / Speak Sharp (an outside visitor, a signup, a challenge catching on, a visitor with buying intent), so marketing can act while it is hot.

**What changed in v3:** alerts are **threshold-only and instant**. There are **no scheduled emails at all** — no morning/evening/daily/weekly summary, no overnight batching, no quiet hours. If nothing hits a threshold, nobody gets an email. A **Streamlit dashboard** is added to watch the system and to set the thresholds without touching code.

---

## 1. How it works

Every alert has its **own threshold** and an on/off switch. Similar alerts are organised in **groups that share one window and one cooldown**:

| Setting | Meaning | Edited in |
|---|---|---|
| **Threshold** | email when the count is **at or above** this number | dashboard |
| **Window (minutes)** | the count looks at the last N minutes | dashboard |
| **Cooldown (minutes)** | minimum gap between emails for the same rule (and the same challenge/link, for per-item rules) | dashboard |
| **Enabled** | switch the rule on/off | dashboard |
| **Recipients** | the default list (Settings) | dashboard |

**Flow:** every **15 minutes** (selectable 5, 10 or 15 in the dashboard) the checker counts each rule's events in its window → if `count >= threshold` it sends **one email immediately** → otherwise it does nothing. A window is never shorter than the check interval, so no event can slip between two checks.

**Rules of the road**
- **Counting resets after an alert.** Once an email goes out, only *newer* events count toward the next one. Threshold 1 = an email for every new event; threshold 5 = an email each time 5 new ones pile up inside the window.
- **Cooldown combines, it doesn't lose.** Events that arrive during a cooldown are held and go out together in the next email ("3 new signups"), so a burst never means a flood.
- **Counts are of distinct people/visitors, not clicks**, so one person refreshing does not look like traction.
- **Daily safety cap** (default 20 emails). Beyond it, alerts are still recorded on the dashboard but not emailed.
- **Master email switch (default OFF).** With it off, the system runs normally and records "would have sent" alerts on the dashboard, so thresholds can be tuned for a few days before real emails start.
- **System-error email.** If Neon or OpenPanel cannot be read for 3 runs in a row, one email says so (then silent for 6 h). A threshold-only system must not fail silently.

---

## 2. Where we are

| Piece | Status |
|---|---|
| Neon connector (read-only) | **Built, verified.** Logs in as `traction_reader`; `login_can_write = False` on every table; every query also runs in a `READ ONLY` transaction |
| OpenPanel connector | **Built, verified.** Project `habit10x`. Server-side filter `event=<name>` works (confirmed) |
| Email sender | **Built.** Resend if `RESEND_API_KEY` is set, otherwise SMTP (Gmail app password) |
| **Release 1: rule engine + 10 rules + system-error** | **Built and verified.** Threshold, window, reset-after-alert, cooldown, daily cap, master email switch, retry on failed send. All 10 collectors run against live Neon + OpenPanel without errors; 46 automated tests pass |
| **Release 1: alert history / state** | **Built.** Local SQLite file (`TRACTION_DB_PATH`), no new database permissions needed |
| **Release 1: Streamlit dashboard** | **Built and verified** (headless tests + a real server run). 4 tabs: Overview, Alert rules, History, Settings. Binds to this computer only; `DASHBOARD_PASSWORD` for anything wider |
| Real email delivery | **Not yet confirmed end to end** — emails start OFF; use *Settings > Send a test email now*, then flip the switch |

**What a 14-day replay on today's (dev) data says** (thresholds are per alert, see section 4) — shows why thresholds must be tuned in the dashboard:

| Rule | Events | Busiest hour | Default threshold | Emails it would have sent |
|---|---|---|---|---|
| `outside_attempt` | 10 | 4 | 1 | ~7–8 |
| `new_signup` | 3 | 1 | 1 | 3 |
| `plan_started` | 2 | 1 | 1 | 2 |
| `great_response` | 1 | 1 | 1 | 1 |
| `source_visitors`, `source_signup` | 0 | 0 | 1 | 0 (no social-link traffic on dev) |
| `challenge_visitors` | 49 | 2 | 5 | 0 |
| `attempts_surge` | 19 | 2 | 3 | 0 |
| `pricing_interest` | 11 | 4 | 1 | ~9 (dev testers) |
| `site_visitors` | 1,358 | 7 | 10 | 0 |

Spike thresholds (5 / 3 / 10) are above anything dev ever reached, which is expected until production traffic is connected.

### Blockers before thresholds are final (unchanged)
1. **Both connectors still see dev, not production.** Neon role lives on branch `dev`; OpenPanel project `habit10x` only has `localhost:3000` and `6-dev.vercel.app` traffic. Needed: the production Neon branch (+ `traction_reader` role) and the production OpenPanel project ID (+ a `read` client). Switching is an `.env` change only.
2. **Plan meaning unconfirmed:** `challenge_users.plan = 'social-challenges'` on 40 of 57 users (35 `active`, 4 no status, 1 `cancelled`). Is that paid, or granted access? Decides whether "plan started" is a revenue alert.
3. **Team email list** is empty. Until it is filled in, testers' activity can trigger alerts.
4. **Search Console is empty** (not linked to this OpenPanel project). SEO alerts are out of scope for release 1.

---

## 3. What the data says (dev data — shape, not production volume)

- **Traffic is tiny:** OpenPanel 30 days = 59 unique visitors, 123 sessions, all India, all dev/localhost. Neon `attribution_event` ≈ 1–5 visitors a day. → Ratio rules ("3× normal") are meaningless; use **small absolute thresholds**.
- **Signups:** 51 real in total, 10 in 30 days. Only proven channel: `instagram / ig-humor` → 4 signups; `reddit / re-slug` → 0.
- **Attempts** (`social_challenge_responses`): `private` = logged-in use, mostly a few testers (challenge #38 = 209 rows by 7 people); **`public` real = 11 (+2 guest) — the true "outside visitor tried it" signal**; 204 rows are `@fake.com` test data (bulk upload 2026-10-25) and must be excluded.
- **Scores:** real median 50, 90th percentile 88, max 98 → "great response" starts at **90**.
- **`social_challenges.attempts_count` is unreliable** — always count rows.
- **OpenPanel:** `referrer`/`utm_source` are null on 100 % of sessions; events do not carry the page origin; challenge pages are `/social-challenges/{n}`, `/challenge/{n}`, `/solo-practice/{n}`.
- **Access gaps for `traction_reader`:** cannot read reactions, comments, battles, `battle_invites`, `free_users`, `notifications`, `paid_users` (⚑ = needs an extra `GRANT`, Appendix A).

---

## 4. Rule catalogue

### Release 1 - 3 groups, 10 alerts + 1 safety alert
Each **alert has its own threshold**. Each **group shares one window and one cooldown** across its alerts. All of it is changed in the dashboard.

| Group | Meaning | Window | Cooldown |
|---|---|---|---|
| **Activity alerts** | someone did something - every occurrence counts | 60 min | 15 min |
| **Traffic and attention alerts** | different visitors or people inside the window | 60 min | 60 min |
| **System health** | the checker could not read our data | - | 360 min |

| Group | Alert id | Alert | What it means | Default threshold | Source |
|---|---|---|---|---|---|
| Activity | `outside_attempt` | Outside visitor tried a challenge | someone outside the team answered from the public page or as a guest | 1 | Neon |
| Activity | `new_signup` | New signup | a new real user signed up (email shows where they came from) | 1 | Neon |
| Activity | `plan_started` | Plan started | a user started a plan | 1 | Neon |
| Activity | `great_response` | Great response | a response scored 90 or more | 1 | Neon |
| Activity | `source_signup` | Signup from a social link | a new user whose first visit came through one of our links | 1 | Neon |
| Activity | `pricing_interest` | Interested in paying | someone not on a paid plan clicked Upgrade | 1 | OpenPanel |
| Traffic | `source_visitors` | Visitors from a social link | different visitors through one of our links (per link) | 1 | Neon |
| Traffic | `challenge_visitors` | Challenge page spike | different visitors on one challenge (per challenge) | 5 | Neon |
| Traffic | `attempts_surge` | Many people on one challenge | different people answering the same challenge | 3 | Neon |
| Traffic | `site_visitors` | Site-wide surge | different visitors anywhere on the site | 10 | OpenPanel |
| System | `system_error` | System cannot read data | one email when Neon or OpenPanel cannot be read N checks in a row | 3 | built-in |

Notes
- The minimum score for "great response" is fixed at 90 in code (not a dashboard setting).
- Windows are free-form but never shorter than the check interval. "3 signups in a day" = `new_signup` threshold 3 and the Activity window set to 1440 (the window applies to the whole group).
- The OpenPanel alerts count whatever the connected project receives; until a production project is connected they include dev traffic.

### Later (not in release 1)
First-ever attempt on a challenge · plan **cancellations** (needs state — there is no cancel timestamp) · reactions/comments on a response ⚑ · battle invite accepted by a new user ⚑ · extra intent signals (`insights_go_premium_clicked`, repeated retries, returning visitors) · health rules (tracking silent, referrer missing, wrong-origin traffic, test-data leak, `challenge_not_found`) · Search Console rules (new queries, page enters top 10, CTR collapse — needs GSC linked) · per-Reel tracking.

### Removed in v3
Daily summary email, weekly summary email, quiet hours / overnight batching, "daily cap overflows into a digest", and the "checks stopped" email (a stopped checker cannot report its own outage — the dashboard shows the last run time, and an external heartbeat can be added later).

---

## 5. Exclusions (so test data never triggers an alert)

- **Test email domains** — default `fake.com, habit10x.internal, example.com` (editable in the dashboard).
- **Team emails** — your own addresses (editable in the dashboard; **please provide the list**).
- Distinct counting handles refresh-spam. `attribution_event` is written by an unauthenticated endpoint, so rows are never counted — only distinct visitor ids.

---

## 6. The email

One email per fired alert, written to be acted on in 10 seconds.

- **Subject** states the fact: `New signup from Instagram (ig-humor)` · `Outside visitor tried Challenge #40 "Is arriving early really better?"` · `Challenge #38 is catching on: 5 visitors in the last 60 min`.
- **Body:** what happened, the threshold that was hit (`5 visitors in 60 min — threshold 5`), the details (who/where from/score), one button (*Open challenge*).
- **Footer:** the rule name and where to change the threshold.
- Titles are rich HTML in the database → tags stripped and text escaped before rendering.
- Sender: Gmail SMTP now; Resend with a company domain later (no code change, just `.env`).

---

## 7. Dashboard (Streamlit)

Run: `uv run streamlit run src/habit10x_marketing_automation/dashboard.py`. Optional `DASHBOARD_PASSWORD` locks it (needed before any online deployment — it shows user emails).

| Tab | What you see / do |
|---|---|
| **Overview** | last check time and result · Neon / OpenPanel / email status · **emails ON/OFF switch** · emails sent today vs cap · **Run check now** · latest alerts |
| **Alert thresholds** | **one box per group** with Window / Cooldown and a Save button (grey until something changes) · under it **one row per alert**: on/off switch, what it means, **its own Threshold box**, and *X of Y* against that threshold · below, **a graph for every alert**: the count over the last 14 days, that alert's threshold as a dashed line, a dot wherever an email would have gone out, plus *Right now / Highest in 14 days / Emails it would have sent* |
| **History** | every alert with status (*sent / recorded — email off / capped / failed*), filter by rule and date, what triggered it |
| **Settings** | **check interval (5 / 10 / 15 min)** · default recipients · test-email domains · team emails · daily cap · **send test email** |

---

## 8. Architecture

```
every 15 min:  checker ──▶ collect (Neon read-only + OpenPanel API)
                         ──▶ evaluate rules (threshold, window, reset-after-alert, cooldown)
                         ──▶ send email (Gmail SMTP / Resend)  ──▶ record in state DB
state DB (SQLite file, data/traction.db) ◀──▶ Streamlit dashboard (settings, history)
```
- The app's own tables stay **read-only** (`traction_reader`). Rule settings, alert history and "already alerted" markers live in a **separate local SQLite file** (`TRACTION_DB_PATH`), so release 1 needs no new database permissions.
- **Running it: `uv run traction`** - one command starts the alert checker (every 15 minutes by default; the interval is changed in the dashboard and picked up automatically), the social media refresh (in its own thread) and the dashboard, and opens the browser. Ctrl+C stops everything.
- **Deployment note:** the checker and the dashboard must share one persistent disk. On Railway that means one service with a volume (or moving the state to a Postgres schema — decide at deployment).
- Everything else that used to be a command is now in the dashboard: connection status (Overview and the start-up summary), *Run check now* / *Preview*, *Send a test email*, *Import from input folder*, *Refresh now*.

---

## 9. Phases

| Phase | Deliverable | Status |
|---|---|---|
| 0 | connectors, read-only role, discovery, email sender | **Done** (dev) |
| **Release 1** | rule engine + 10 rules + system-error, instant email, alert history, Streamlit dashboard, tests | **Done** (see §2). Run: `uv run traction` |
| Go-live prep | connect production Neon + OpenPanel; fill team emails; confirm plan meaning | needs your input |
| Tuning | run with **emails OFF** for a few days; set thresholds from the dashboard; then switch emails ON | |
| Deploy | Railway service with volume (checker + dashboard), dashboard password | |
| Release 2 | "Later" list above, Search Console rules | |

---

## 10. Decisions and inputs needed

1. Production Neon branch (with `traction_reader`) and production OpenPanel project ID + `read` client.
2. Does `social-challenges` mean a paying plan?
3. **Team email list** (to exclude).
4. Recipients: one list for everything, or owner-only for some rules (e.g. `plan_started`, `pricing_interest`)?
5. Where to host the checker + dashboard (needs a persistent disk).
6. Link Search Console to the production OpenPanel project (release 2).

---

## 11. Social media tracker (added 2026-11-25)

A separate section, independent of the alerts: it follows the **views, likes and comments** of short videos listed in an Excel file (`input/raw.xlsx`: one column named `URL`; other columns are kept as notes).

- **Flow:** Excel in `input/` (new files are picked up automatically; removed videos stay removed) -> tracked list -> refreshed automatically at an interval you type on the page (hours, default 6, 0 = off) by `uv run traction`, or on demand (**Refresh now**) -> one stored snapshot per check -> dashboard **Social tracker** view (totals, table, status).
- **Storage:** the same local SQLite file as the alerts (`social_videos`, `social_stats`). Videos are de-duplicated by platform + video id, so the same video pasted as `youtu.be/..` and `youtube.com/shorts/..` is one video.
- **How numbers are read:** YouTube through the official API when `YOUTUBE_API_KEY` is set, otherwise (and for every other platform) by reading the public page with yt-dlp. A failed read is stored with a plain reason and never hides the last good numbers.

| Platform | Verified on 2026-11-25 from this computer | Limits |
|---|---|---|
| YouTube Shorts | **Yes** - views, likes, comments (2 real videos) | Comments blank when a video has them turned off |
| Instagram Reels | **Yes** - likes, comments (2 real reels) | **No public view count**, so views stay blank. **Throttles repeated reads**: after about 8 fetches of the same reels within an hour it answered "login or limiting requests" (seen 2026-11-25) - keep the refresh interval at an hour or more. Official view counts exist only for your own accounts through the Instagram Graph API |
| TikTok | **No** - `tiktok.com` times out from this network (blocked in India) | Needs `SOCIAL_PROXY`, or run the tracker from a server abroad. Code path is in place but untested |
| X | **Not verified** - no real X link in the file yet | Works only for posts with a video; may ask for a login (`SOCIAL_COOKIES_FILE`) |

**Traction alert (built):** three custom numbers on the page - views, likes, comments (0 = off), one number per metric for every video on every platform. A video that passes a number between two checks is announced **once** (per metric and number); a video already above it when first seen is not; a changed number only counts passes from then on. One email lists everything that passed in that refresh. It reuses the alert plumbing (master email switch, daily cap, recipients, History entry named "Video reached a threshold"). A metric a platform does not show (Instagram views) simply never fires.

Not built (possible next steps): alert on growth speed (e.g. +1,000 views in an hour), Instagram Graph API for views on your own accounts, CSV import.

---

## Appendix A — database grants

```sql
-- already applied (dev branch):
CREATE ROLE traction_reader LOGIN PASSWORD '<random>';
GRANT CONNECT ON DATABASE neondb TO traction_reader;
GRANT USAGE ON SCHEMA public TO traction_reader;
GRANT SELECT ON attribution_event, social_challenges, social_challenge_responses, source_links,
                user_attribution_summary, video_submissions, comments, challenge_users
      TO traction_reader;

-- optional, for later rules (reactions / battle invites):
GRANT SELECT ON social_challenge_response_reactions, social_challenge_response_comments, battle_invites
      TO traction_reader;
```

## Appendix B — verified OpenPanel API facts

Auth headers `openpanel-client-id` / `openpanel-client-secret`, base `https://api.openpanel.dev`, client type `read` or `root`.
- `GET /insights/{project}/live` → `{visitors}`; `…/metrics`, `…/pages`, `…/referrer`, `…/utm_source`, `…/country`, `…/device`; `…/gsc/overview|pages|queries` (200, empty until Search Console is linked).
- `GET /export/events?projectId&start&end&page&limit&includes=properties[&event=<name>]` — dates `YYYY-MM-DD` (use *tomorrow* as `end` to include today), repeat `includes`; **`event=<name>` filters server-side (confirmed)** → `{meta:{count,totalCount,pages,current}, data[]}`. Event fields: `id, name, path, sessionId, deviceId, profileId, properties, createdAt, country, os, browser, referrer`.
- `/manage/*` returns 401 for the current client (not `root`).
- Visitors are anonymous (`profileId = deviceId`) → count unique visitors by `deviceId` / `sessionId`.
