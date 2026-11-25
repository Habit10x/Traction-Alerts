"""Streamlit dashboard: watch the checker, set thresholds from real data, read the alert history.

Started by `uv run traction` (do not run this file by hand).
"""

import hmac
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from habit10x_marketing_automation.charts import alert_chart, history_chart
from habit10x_marketing_automation.config import get_settings
from habit10x_marketing_automation.emails import entity_label
from habit10x_marketing_automation.engine import (Runtime, effective_config, live_counts, load_configs, peak,
                                                  rolling_counts, run_once, seed_defaults, simulate)
from habit10x_marketing_automation.mailer import send_email
from habit10x_marketing_automation.rules import GROUP_LIST, RULE_LIST, RULE_NAMES, RULES, SOCIAL_RULE, Context
from habit10x_marketing_automation.social import (METRICS, REFRESH_HOURS, add_links, import_folder, import_paths,
                                                  refresh, since_key, threshold_key)
from habit10x_marketing_automation.social_links import PLATFORM_NAMES
from habit10x_marketing_automation.store import GroupConfig, RuleConfig, Store, parse_ts

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
HISTORY_DAYS = 14
INTERVAL_CHOICES = [5, 10, 15]
NAV = ["Overview", "Alert thresholds", "Social tracker", "History", "Settings"]

st.set_page_config(page_title="Traction Alerts", page_icon=None, layout="wide")
settings = get_settings()


# ------------------------------------------------------------------------------------- helpers
def when(ts: datetime | str | None) -> str:
    if not ts:
        return "never"
    ts = parse_ts(ts) if isinstance(ts, str) else ts
    return ts.astimezone(IST).strftime("%d %b %H:%M")


def ago(ts: str | None) -> str:
    if not ts:
        return "never"
    minutes = int((datetime.now(UTC) - parse_ts(ts)).total_seconds() // 60)
    return "just now" if minutes < 1 else f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"


def parse_csv(raw: str) -> list[str]:
    return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]


def csv_text(items: list[str]) -> str:
    return ", ".join(items)


def email_provider() -> str:
    sender = settings.alert_from_email or "ALERT_FROM_EMAIL not set"
    if settings.resend_api_key and settings.resend_api_key.get_secret_value():
        return f"Resend, from {sender}"
    if settings.smtp_host:
        return f"SMTP ({settings.smtp_host}), from {sender}"
    return "Not configured (set RESEND_API_KEY or SMTP_* in .env)"


def seed(key: str, value) -> None:
    """Give a widget its starting value through session state (avoids Streamlit's default-vs-state warning)."""
    if key not in st.session_state:
        st.session_state[key] = value


@st.cache_data(ttl=300, show_spinner=False)
def load_all_history(params_json: str, test_domains: tuple, team: tuple, days: int):
    """Last N days of events for every rule: (read_at, {rule_id: (events, error)}).

    One thread per rule so the slow network round-trips overlap. Cached 5 minutes ("Refresh data" clears it).
    """
    params, now = json.loads(params_json), datetime.now(UTC)

    def one(rule):
        ctx = Context(settings, list(test_domains), list(team))
        try:
            return rule.id, (rule.collect(ctx, now - timedelta(days=days), now, params.get(rule.id, {})), "")
        except Exception as exc:  # noqa: BLE001 - one unreachable source must not hide the other alerts
            return rule.id, (None, f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
        finally:
            ctx.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        return now, dict(pool.map(one, [r for r in RULE_LIST if r.collect]))


# --------------------------------------------------------------------------------- password gate
password = settings.dashboard_password.get_secret_value() if settings.dashboard_password else ""
if password and not st.session_state.get("authed"):
    st.title("Traction Alerts")
    entered = st.text_input("Dashboard password", type="password")
    if entered and hmac.compare_digest(entered, password):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    st.stop()

store = Store(settings.state_path)
seed_defaults(store, RULES)
rt = Runtime.load(settings, store)
interval = rt.check_interval_minutes

st.title("Traction Alerts")
st.caption(f"An email goes out the moment an alert reaches its threshold. The checker looks every {interval} minutes. "
           "Nothing is sent on a schedule. Times are IST.")
if "flash" in st.session_state:
    kind, message = st.session_state.pop("flash")
    getattr(st, kind)(message)
view = st.radio("View", NAV, horizontal=True, key="view", label_visibility="collapsed")

# ------------------------------------------------------------------------------------- overview
if view == "Overview":
    last = (store.recent_runs(1) or [None])[0]
    sent_24h = store.count_sent_since(datetime.now(UTC) - timedelta(hours=24))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last check", ago(last["finished_at"]) if last else "never")
    c2.metric("Data sources", "OK" if last and last["ok"] else ("Problem" if last else "-"))
    c3.metric("Emails sent (24 h)", f"{sent_24h} / {rt.daily_cap}")
    c4.metric("Emails", "ON" if rt.email_enabled else "OFF")

    if last is None or datetime.now(UTC) - parse_ts(last["finished_at"]) > timedelta(minutes=2 * interval + 5):
        st.warning("The checker is not running (no check recently). Alerts only fire while it runs. "
                   "Start everything with: `uv run traction`")

    def _toggle_emails() -> None:
        store.set_setting("email_enabled", "1" if st.session_state["email_switch"] else "0")

    st.toggle("Send emails when a threshold is reached", value=rt.email_enabled, key="email_switch",
              on_change=_toggle_emails,
              help="OFF: alerts are still detected and listed under History, but nobody is emailed.")
    if not rt.email_enabled:
        st.info("Emails are OFF. Alerts are recorded in History as 'recorded' and nobody is emailed.")
    elif not rt.recipients:
        st.error("Emails are ON but there are no recipients. Add them in Settings.")
    if last:
        st.write("**Sources in the last check:** " + " | ".join(
            f"{k}: {v if v == 'ok' else 'ERROR - ' + v}" for k, v in last["sources"].items()))
    st.write(f"**Email sender:** {email_provider()}")

    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button("Run check now", type="primary", help="Checks every alert once. Emails only if the switch is ON."):
        with st.spinner("Checking..."):
            result = run_once(settings, store)
        st.session_state["flash"] = (
            "success" if result.ok else "warning",
            f"Check finished: {len(result.fired)} alert(s) fired."
            + (f" Problems: {result.source_errors}" if result.source_errors else ""))
        st.rerun()
    if b2.button("Preview (dry run)", help="Shows what would fire now. Records and sends nothing."):
        with st.spinner("Checking..."):
            result = run_once(settings, store, dry_run=True)
        st.write(pd.DataFrame(result.fired) if result.fired else "Nothing would fire right now.")

    st.subheader("Latest alerts")
    latest = store.alerts(10)
    if latest:
        st.dataframe(pd.DataFrame([{"time": when(a["fired_at"]),
                                    "alert": RULE_NAMES.get(a["rule_id"], a["rule_id"]),
                                    "status": a["status"], "subject": a["subject"]} for a in latest]),
                     hide_index=True, width="stretch")
    else:
        st.caption("No alert has fired yet.")

# ------------------------------------------------------------------------------ alert thresholds
elif view == "Alert thresholds":
    configs = load_configs(store, RULES)  # each alert: its own threshold, plus its group's window and cooldown
    saved_groups = store.get_groups()
    now = datetime.now(UTC)
    start = now - timedelta(days=HISTORY_DAYS)
    step = timedelta(minutes=interval)
    last_run = (store.recent_runs(1) or [None])[0]

    if st.button("Refresh data", help=f"Re-reads the last {HISTORY_DAYS} days from Neon and OpenPanel."):
        st.cache_data.clear()
        st.rerun()
    with st.spinner(f"Reading the last {HISTORY_DAYS} days..."):
        _, results = load_all_history(
            json.dumps({r.id: {k: float(v) for k, v in configs[r.id].params.items()} for r in RULE_LIST if r.collect},
                       sort_keys=True), tuple(rt.test_domains), tuple(rt.team_emails), HISTORY_DAYS)

    for group in GROUP_LIST:
        members = [r for r in RULE_LIST if r.group == group.id]
        saved = saved_groups[group.id]
        is_system = group.id == "system"
        saved_window = 0 if is_system else max(saved.window_minutes, interval)
        seed(f"{group.id}_cd", saved.cooldown_minutes)
        if not is_system:
            seed(f"{group.id}_wi", saved_window)
        for rule in members:
            seed(f"{rule.id}_en", configs[rule.id].enabled)
            seed(f"{rule.id}_th", configs[rule.id].threshold)

        with st.container(border=True):
            st.subheader(group.name)
            st.caption(group.description)
            c_wi, c_cd, c_save, _ = st.columns([1, 1, 1, 2], vertical_alignment="bottom")
            window = 0 if is_system else c_wi.number_input("Window (minutes)", interval, 100000, key=f"{group.id}_wi")
            cooldown = c_cd.number_input("Cooldown (minutes)", 0, 100000, key=f"{group.id}_cd")
            changed = ((window, cooldown) != (saved_window, saved.cooldown_minutes)
                       or any((st.session_state[f"{r.id}_en"], st.session_state[f"{r.id}_th"])
                              != (configs[r.id].enabled, configs[r.id].threshold) for r in members))
            if c_save.button("Save", key=f"{group.id}_save", type="primary", disabled=not changed):
                store.save_group(GroupConfig(group.id, int(window), int(cooldown)))
                stored = store.get_rules()
                for rule in members:
                    stored[rule.id].enabled = bool(st.session_state[f"{rule.id}_en"])
                    stored[rule.id].threshold = int(st.session_state[f"{rule.id}_th"])
                    store.save_rule(stored[rule.id])
                st.session_state["flash"] = ("success", f"{group.name} saved. The checker uses it from its next "
                                                        f"check, within {interval} minutes.")
                st.rerun()

            # one row per alert: on/off, what it means, its own threshold, where it stands right now
            widths = [0.5, 3, 1.2, 2.6]
            for col, head in zip(st.columns(widths), ["", "Alert", "Threshold", "Now"]):
                col.caption(head)
            shown: dict[str, dict] = {}
            for rule in members:
                c_on, c_name, c_th, c_now = st.columns(widths, vertical_alignment="center")
                c_on.toggle("On", key=f"{rule.id}_en", label_visibility="collapsed")
                c_name.markdown(f"**{rule.name}**")
                c_name.caption(rule.description)
                threshold = int(c_th.number_input("Threshold", 1, 100000, key=f"{rule.id}_th",
                                                  label_visibility="collapsed"))
                if is_system:
                    c_now.write("Data sources OK" if last_run and last_run["ok"] else
                                ("Problem reading data" if last_run else "No check yet"))
                    continue
                events, error = results[rule.id]
                if error:
                    c_now.error(f"Could not read data: {error}")
                    continue
                edit = RuleConfig(rule.id, True, threshold, int(window), int(cooldown), "", configs[rule.id].params)
                eff = effective_config(edit, interval)
                counts = live_counts(rule, eff, events, now)
                now_value = max(counts.values(), default=0)
                where = max(counts, key=counts.get) if counts else ""
                sample = next((e.info for e in events if (e.entity if rule.per_entity else "") == where), {})
                label = entity_label(sample, where) if now_value else ""
                c_now.progress(min(now_value / threshold, 1.0),
                               text=f"{now_value} of {threshold}" + (f" - {label}" if label else ""))
                shown[rule.id] = {"events": events, "eff": eff, "now": now_value, "threshold": threshold}

            # the same graph for every alert: what the count did, its own threshold, where an email would have gone
            if shown:
                tabs = st.tabs([r.name for r in members])
                for tab, rule in zip(tabs, members):
                    if rule.id not in shown:
                        continue
                    data = shown[rule.id]
                    with tab:
                        series = rolling_counts(rule, data["eff"].window_minutes, data["events"], start, now, step)
                        fired = simulate(rule, data["eff"], data["events"], start, now, step)
                        highest, _ = peak(rule, data["eff"].window_minutes, data["events"])
                        m1, m2, m3 = st.columns(3)
                        m1.metric("Right now", data["now"])
                        m2.metric(f"Highest in {HISTORY_DAYS} days", highest)
                        m3.metric("Emails it would have sent", len(fired))
                        st.altair_chart(alert_chart(series, fired, data["threshold"]), width="stretch")
                        if fired:
                            with st.expander("When it would have emailed"):
                                st.dataframe(pd.DataFrame([{"time": when(f.newest), "count": f.count,
                                                            "on": entity_label(f.events[0].info, f.entity) or "-"}
                                                           for f in fired][-30:]), hide_index=True, width="stretch")

# ------------------------------------------------------------------------------------ social tracker
elif view == "Social tracker":
    folder = settings.input_path
    overview = store.social_overview()
    seed("social_hours", float(store.get_setting(REFRESH_HOURS, "6") or 0))
    for metric in METRICS:
        seed(f"social_{metric}", int(float(store.get_setting(threshold_key(metric), "0") or 0)))

    def _save_hours() -> None:
        store.set_setting(REFRESH_HOURS, str(float(st.session_state["social_hours"])))

    def _save_threshold(metric: str) -> None:
        value = int(st.session_state[f"social_{metric}"])
        if value != int(float(store.get_setting(threshold_key(metric), "0") or 0)):
            store.set_setting(threshold_key(metric), str(value))
            store.set_setting(since_key(metric), datetime.now(UTC).isoformat())  # only count passes from now on

    f1, f2, f3, f4 = st.columns(4)
    f1.number_input("Refresh every (hours, 0 = off)", 0.0, 168.0, step=0.5, format="%g", key="social_hours",
                    on_change=_save_hours, help="Numbers are re-read automatically at this interval, e.g. 1, 2 or 3.")
    for column, metric in zip((f2, f3, f4), METRICS):
        column.number_input(f"{metric.title()} alert at (0 = off)", 0, 1_000_000_000, step=10, key=f"social_{metric}",
                            on_change=_save_threshold, args=(metric,),
                            help=f"Email me when any video, on any platform, reaches this many {metric}. One number "
                                 f"for all platforms; each video is announced once, when it passes the number "
                                 "between two checks.")

    last_check = max((v["attempt"]["fetched_at"] for v in overview if v["attempt"]), default=None)
    last_run = (store.recent_runs(1) or [None])[0]
    checker_up = last_run is not None and datetime.now(UTC) - parse_ts(last_run["finished_at"]) <= timedelta(
        minutes=2 * interval + 5)
    hours = float(st.session_state["social_hours"])
    if hours > 0 and not checker_up:
        st.warning("Automatic refresh and the threshold alerts only run while the checker is running: "
                   "`uv run traction`")
    elif hours > 0 and overview:
        due = parse_ts(last_check) + timedelta(hours=hours) if last_check else datetime.now(UTC)
        st.caption(f"Last refreshed {when(last_check)}. Next automatic refresh around "
                   f"{when(max(due, datetime.now(UTC)))}.")
    elif hours == 0:
        st.caption("Automatic refresh is off.")

    def import_summary(result) -> tuple[str, str]:
        text = f"Added {result.added} video(s); {result.existing} already tracked."
        if result.skipped:
            text += f" Skipped {len(result.skipped)}: " + "; ".join(result.skipped[:3])
        return ("warning" if result.skipped else "success"), text

    b1, b2, _ = st.columns([1.4, 1, 3])
    if b1.button("Import from input folder", help=f"Reads every Excel file in {folder}"):
        st.session_state["flash"] = import_summary(import_folder(store, folder))
        st.rerun()
    if b2.button("Refresh now", disabled=not overview, help="Reads the current numbers of every video right away."):
        with st.spinner(f"Checking {len(overview)} video(s)..."):
            outcome = refresh(settings, store)
        problems = "; ".join(f"{PLATFORM_NAMES.get(p, p)}: {e}" for p, e in outcome.errors.items())
        text = f"Checked {outcome.checked}: {outcome.ok} ok, {outcome.failed} failed."
        if outcome.alert:
            text += (" Alert: emailed." if outcome.alert["status"] == "sent" else
                     f" Alert {outcome.alert['status']}: {outcome.alert['subject']}")
        st.session_state["flash"] = ("warning" if outcome.failed else "success", text + (f" {problems}" if problems else ""))
        st.rerun()

    with st.expander("Add or remove videos"):
        upload = st.file_uploader("Excel file with a column of links", type=["xlsx"])
        if upload is not None and st.button("Import this file"):
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
                handle.write(upload.getvalue())
            try:
                st.session_state["flash"] = import_summary(import_paths(store, [Path(handle.name)]))
            finally:
                Path(handle.name).unlink(missing_ok=True)
            st.rerun()
        pasted = st.text_area("Or paste links (one per line)", key="social_paste", height=90)
        if st.button("Add links", disabled=not pasted.strip()):
            st.session_state["flash"] = import_summary(add_links(store, pasted))
            st.session_state.pop("social_paste", None)
            st.rerun()
        if overview:
            by_id = {v["id"]: v for v in overview}
            doomed = st.selectbox("Remove a video from the tracker", list(by_id), key="social_remove",
                                  format_func=lambda i: f"{PLATFORM_NAMES[by_id[i]['platform']]} - "
                                                        f"{(by_id[i]['title'] or by_id[i]['url'])[:70]}")
            if st.button("Stop tracking"):
                store.remove_social_video(doomed)
                st.session_state["flash"] = ("success", "Removed from the tracker.")
                st.rerun()

    if not overview:
        st.info(f"No videos yet. Put an Excel file with a column of links in {folder} and press Import.")
    else:
        present = sorted({PLATFORM_NAMES[v["platform"]] for v in overview})
        chosen = st.multiselect("Platform", present, default=present)
        shown = [v for v in overview if PLATFORM_NAMES[v["platform"]] in chosen]

        def total(field: str) -> int:
            return sum((v["latest"] or {}).get(field) or 0 for v in shown)

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Videos", len(shown))
        m2.metric("Views", f"{total('views'):,}")
        m3.metric("Likes", f"{total('likes'):,}")
        m4.metric("Comments", f"{total('comments'):,}")

        def status(v: dict) -> str:
            if v["attempt"] is None:
                return "Not checked yet"
            return "OK" if v["attempt"]["ok"] else "Failed: " + v["attempt"]["error"]

        table = pd.DataFrame([{
            "Platform": PLATFORM_NAMES[v["platform"]], "Video": v["title"] or v["url"],
            "Views": (v["latest"] or {}).get("views"), "Likes": (v["latest"] or {}).get("likes"),
            "Comments": (v["latest"] or {}).get("comments"),
            "Last checked": when(v["attempt"]["fetched_at"]) if v["attempt"] else "never",
            "Status": status(v), "Link": v["url"]} for v in shown])
        number = st.column_config.NumberColumn(format="localized")
        st.dataframe(table, hide_index=True, width="stretch", column_config={
            "Views": number, "Likes": number, "Comments": number,
            "Link": st.column_config.LinkColumn("Link", display_text="open")})
        st.caption("A blank (None) number is one the platform does not show publicly: Instagram hides views, and some "
                   "videos have comments turned off.")

# ---------------------------------------------------------------------------------------- history
elif view == "History":
    f1, f2, f3 = st.columns([2, 2, 1])
    rule_names = {"All alerts": None, **{r.name: r.id for r in RULE_LIST}, SOCIAL_RULE.name: SOCIAL_RULE.id}
    chosen = rule_names[f1.selectbox("Alert", list(rule_names))]
    statuses_chosen = f2.multiselect("Status", ["sent", "recorded", "capped", "failed"],
                                     default=["sent", "recorded", "capped", "failed"])
    limit = f3.number_input("Rows", 10, 1000, 100)
    rows = store.alerts(int(limit), chosen, statuses_chosen)
    st.caption("sent = emailed | recorded = detected while emails were OFF | capped = daily limit reached | "
               "failed = email could not be sent (retried every check)")
    if rows:
        st.dataframe(pd.DataFrame([{"id": a["id"], "time": when(a["fired_at"]),
                                    "alert": RULE_NAMES.get(a["rule_id"], a["rule_id"]),
                                    "status": a["status"], "count": a["count"], "threshold": a["threshold"],
                                    "subject": a["subject"], "error": a["error"]} for a in rows]),
                     hide_index=True, width="stretch")
        pick_id = st.selectbox("Show what triggered an alert", [a["id"] for a in rows[:50]],
                               format_func=lambda i: next(f"#{a['id']} {when(a['fired_at'])} {a['subject'][:70]}"
                                                          for a in rows if a["id"] == i))
        st.json(json.loads(next(a["detail"] for a in rows if a["id"] == pick_id)))
    else:
        st.caption("Nothing to show yet.")

# --------------------------------------------------------------------------------------- settings
else:
    st.write("Changes apply from the next check. Leave a box empty to send to / exclude no one.")
    options = sorted({*INTERVAL_CHOICES, interval})
    new_interval = st.selectbox("How often the checker looks (minutes)", options, index=options.index(interval),
                                help="5 = quickest alerts; 15 = lighter on the database. The running checker picks "
                                     "this up on its own after its next check.")
    rec = st.text_area("Default recipients (comma or one per line)", csv_text(rt.recipients))
    dom = st.text_area("Test email domains to ignore", csv_text(rt.test_domains),
                       help="Accounts on these domains (bulk-upload and test data) never trigger alerts.")
    team = st.text_area("Team email addresses to ignore", csv_text(rt.team_emails),
                        help="Your own accounts, so testing the site never triggers alerts.")
    cap = st.number_input("Safety limit: maximum emails in 24 hours", 1, 1000, rt.daily_cap,
                          help="Beyond this, alerts are recorded in History but not emailed.")
    if st.button("Save settings", type="primary"):
        store.set_setting("check_interval_minutes", str(int(new_interval)))
        store.set_setting("recipients", csv_text(parse_csv(rec)))
        store.set_setting("test_domains", csv_text(parse_csv(dom)))
        store.set_setting("team_emails", csv_text(parse_csv(team)))
        store.set_setting("daily_cap", str(int(cap)))
        st.session_state["flash"] = ("success", "Settings saved.")
        st.rerun()

    st.divider()
    st.write(f"**Email sender:** {email_provider()}")
    if st.button("Send a test email now"):
        targets = parse_csv(rec) or rt.recipients
        try:
            send_email(settings, "Test: Traction alerts", "<p>This is a test. Alert emails will reach you.</p>",
                       "This is a test. Alert emails will reach you.", to=targets)
            st.success(f"Sent to {', '.join(targets)}.")
        except Exception as exc:  # noqa: BLE001 - show the reason (bad password, no recipients...)
            st.error(f"{type(exc).__name__}: {exc}")
