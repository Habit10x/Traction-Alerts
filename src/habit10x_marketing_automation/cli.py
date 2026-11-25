"""`uv run traction` starts everything: the alert checker, the social media refresh and the dashboard."""

import argparse
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings, get_settings
from .engine import Runtime, run_once
from .neon import Neon, describe_login
from .openpanel import OpenPanel
from .social import import_folder, refresh
from .store import Store

DASHBOARD = Path(__file__).with_name("dashboard.py")
FIRST_PORT = 8501


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:160]


# ------------------------------------------------------------------------------------ start-up summary
def startup_report(settings: Settings) -> list[str]:
    """One short line per connection, so a wrong password or a missing key is obvious the moment it starts."""
    lines = []
    if not settings.database_url:
        lines.append("Neon       MISSING  DATABASE_URL in .env")
    else:
        try:
            with Neon(settings) as db:
                role, can_write = describe_login(db)
            lines.append(f"Neon       WARNING  logged in as {role}, which can WRITE. Use a read-only role."
                         if can_write else f"Neon       OK  logged in as {role} (read-only)")
        except Exception as exc:  # noqa: BLE001 - report, never crash the start-up
            lines.append(f"Neon       FAILED  {_first_line(exc)}")

    missing = [n for n in ("OPENPANEL_CLIENT_ID", "OPENPANEL_CLIENT_SECRET", "OPENPANEL_PROJECT_ID")
               if not getattr(settings, n.lower())]
    if missing:
        lines.append(f"OpenPanel  MISSING  {', '.join(missing)} in .env")
    else:
        try:
            with OpenPanel(settings) as op:
                op.insights("live")
            lines.append("OpenPanel  OK")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"OpenPanel  FAILED  {_first_line(exc)}")

    runtime = Runtime.load(settings, Store(settings.state_path))
    if settings.resend_api_key and settings.resend_api_key.get_secret_value():
        sender = "Resend"
    elif settings.smtp_host:
        sender = f"SMTP ({settings.smtp_host})"
    else:
        sender = "no sender configured (set RESEND_API_KEY or SMTP_* in .env)"
    state = "ON" if runtime.email_enabled else "OFF - alerts are only recorded; switch it on in the dashboard"
    lines.append(f"Email      {sender}, {len(runtime.recipients)} recipient(s), emails {state}")
    return lines


# --------------------------------------------------------------------------------------- the checker
def _social_refresh_in_background(settings: Settings, hours: float) -> None:
    """Runs next to the alert checks in its own thread (and its own database connection)."""
    try:
        result = refresh(settings, Store(settings.state_path), due_hours=hours)
        if result.checked:
            print(f"[{_now()}] social: checked {result.checked} video(s), {result.ok} ok, {result.failed} failed",
                  flush=True)
        if result.alert:
            print(f"[{_now()}] social: {result.alert['subject']} ({result.alert['status']})", flush=True)
    except Exception as exc:  # noqa: BLE001 - never let scraping trouble stop the alert checks
        print(f"[{_now()}] social: refresh failed: {type(exc).__name__}: {_first_line(exc)}", flush=True)


def checker_loop(settings: Settings, stop: threading.Event, sleep=None) -> None:
    """Every check interval: pick up new Excel files, check the alerts, and refresh the social numbers when due."""
    sleep = sleep or stop.wait
    store = Store(settings.state_path)
    social: threading.Thread | None = None
    while not stop.is_set():
        try:  # new files in the input folder are tracked automatically; videos you removed stay removed
            added = import_folder(store, settings.input_path, reactivate=False).added
            if added:
                print(f"[{_now()}] social: {added} new video(s) found in the input folder", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[{_now()}] input folder: {type(exc).__name__}: {_first_line(exc)}", flush=True)
        try:
            result = run_once(settings, store)
            fired = ", ".join(f"{r['rule_id']}:{r['status']}" for r in result.fired)
            print(f"[{_now()}] alerts checked: {'ok' if result.ok else 'PROBLEM ' + str(result.source_errors)}"
                  f"{'; fired ' + fired if fired else ''}", flush=True)
        except Exception as exc:  # noqa: BLE001 - keep going; the next pass retries
            print(f"[{_now()}] alert check failed: {type(exc).__name__}: {_first_line(exc)}", flush=True)
        hours = float(store.get_setting("social_refresh_hours", "6") or 0)  # 0 = off; changed on the dashboard
        if hours > 0 and (social is None or not social.is_alive()):
            social = threading.Thread(target=_social_refresh_in_background, args=(settings, hours), daemon=True)
            social.start()
        sleep(Runtime.load(settings, store).check_interval_minutes * 60)  # re-read every time


# ------------------------------------------------------------------------------------------ the dashboard
def free_port(start: int = FIRST_PORT) -> int:
    for port in range(start, start + 50):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found for the dashboard")


def _open_when_ready(port: int, url: str, timeout: float = 60) -> None:
    if os.environ.get("TRACTION_NO_BROWSER"):  # for servers and automated runs
        return
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                webbrowser.open(url)
                return
        except OSError:
            threading.Event().wait(0.5)


def run(settings: Settings) -> int:
    print("Traction is starting: alert checker, social tracker and dashboard.")
    for line in startup_report(settings):
        print("  " + line, flush=True)
    port = free_port()
    url = f"http://localhost:{port}"
    # Local only: the dashboard lists user emails. Headless so Streamlit never stops to ask for an email address;
    # the browser is opened here instead.
    command = [sys.executable, "-m", "streamlit", "run", str(DASHBOARD), "--server.address", "localhost",
               "--server.port", str(port), "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    stop = threading.Event()
    threading.Thread(target=checker_loop, args=(settings, stop), daemon=True).start()
    dashboard = subprocess.Popen(command)
    threading.Thread(target=_open_when_ready, args=(port, url), daemon=True).start()
    print(f"Dashboard: {url}   Press Ctrl+C to stop everything.", flush=True)
    try:
        return dashboard.wait()
    except KeyboardInterrupt:
        print("Stopping...")
        return 0
    finally:
        stop.set()
        if dashboard.poll() is None:
            dashboard.terminate()
            try:
                dashboard.wait(timeout=10)
            except subprocess.TimeoutExpired:
                dashboard.kill()


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    argparse.ArgumentParser(prog="traction", description=__doc__).parse_args(argv)
    return run(get_settings())
