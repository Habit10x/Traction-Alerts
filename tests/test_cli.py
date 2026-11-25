import socket
import threading
from datetime import datetime, timedelta, timezone

import openpyxl

from habit10x_marketing_automation import cli
from habit10x_marketing_automation.config import Settings
from habit10x_marketing_automation.engine import RunResult
from habit10x_marketing_automation.store import Store

UTC = timezone.utc


def make_xlsx(path, urls):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["URL"])
    for url in urls:
        sheet.append([url])
    workbook.save(path)


# ---------------------------------------------------------------------------------------- start-up summary
def test_startup_summary_says_exactly_what_is_missing():
    lines = cli.startup_report(Settings())
    assert "Neon       MISSING  DATABASE_URL in .env" in lines
    assert any(l.startswith("OpenPanel  MISSING  OPENPANEL_CLIENT_ID, OPENPANEL_CLIENT_SECRET, OPENPANEL_PROJECT_ID") for l in lines)
    email = next(l for l in lines if l.startswith("Email"))
    assert "no sender configured" in email and "0 recipient(s)" in email and "emails OFF" in email


class FakeNeon:
    def __init__(self, settings):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_startup_summary_reports_the_database_login_and_the_other_connections(monkeypatch):
    settings = Settings(database_url="postgresql://x", openpanel_client_id="a", openpanel_client_secret="b",
                        openpanel_project_id="p", smtp_host="smtp.gmail.com", alert_recipients="o@x.com, t@x.com")
    monkeypatch.setattr(cli, "Neon", FakeNeon)

    class FakeOpenPanel:
        fail = False

        def __init__(self, settings):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def insights(self, name):
            if FakeOpenPanel.fail:
                raise RuntimeError("401 from OpenPanel: check the client")
            return {"visitors": 0}

    monkeypatch.setattr(cli, "OpenPanel", FakeOpenPanel)
    monkeypatch.setattr(cli, "describe_login", lambda db: ("traction_reader", False))
    lines = cli.startup_report(settings)
    assert "Neon       OK  logged in as traction_reader (read-only)" in lines and "OpenPanel  OK" in lines
    assert any("SMTP (smtp.gmail.com), 2 recipient(s)" in l for l in lines)

    monkeypatch.setattr(cli, "describe_login", lambda db: ("neondb_owner", True))
    FakeOpenPanel.fail = True
    lines = cli.startup_report(settings)
    assert any(l.startswith("Neon       WARNING") and "can WRITE" in l for l in lines)
    assert any(l.startswith("OpenPanel  FAILED  401 from OpenPanel") for l in lines)


# ------------------------------------------------------------------------------------------- the dashboard port
def test_free_port_skips_a_port_that_is_in_use():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        busy = taken.getsockname()[1]
        assert cli.free_port(busy) != busy


def test_the_browser_opens_once_the_dashboard_answers_and_never_when_told_not_to(monkeypatch):
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        cli._open_when_ready(port, f"http://localhost:{port}", timeout=5)
        assert opened == [f"http://localhost:{port}"]
        monkeypatch.setenv("TRACTION_NO_BROWSER", "1")
        cli._open_when_ready(port, "http://localhost:1", timeout=5)
        assert len(opened) == 1


# ------------------------------------------------------------------------------------------ the checker loop
def run_loop(settings, cycles, monkeypatch, run_once=None):
    """Run the loop for a fixed number of cycles, with no real waiting. Returns (sleep seconds, run_once calls)."""
    stop, sleeps, calls = threading.Event(), [], []

    def fake_run_once(settings, store):
        calls.append(1)
        if len(calls) >= cycles:
            stop.set()
        if run_once:
            return run_once(settings, store)
        return RunResult(started=datetime.now(UTC), dry_run=False)

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    cli.checker_loop(settings, stop, sleep=sleeps.append)
    return sleeps, calls


def test_the_loop_checks_every_interval_set_in_the_dashboard(monkeypatch):
    monkeypatch.setattr(cli, "_social_refresh_in_background", lambda *a: None)
    settings = Settings()
    assert run_loop(settings, 2, monkeypatch)[0] == [900, 900]  # 15 minutes by default
    Store(settings.state_path).set_setting("check_interval_minutes", "5")
    assert run_loop(settings, 1, monkeypatch)[0] == [300]


def test_a_failing_check_or_input_folder_never_stops_the_loop(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_social_refresh_in_background", lambda *a: None)
    monkeypatch.setattr(cli, "import_folder", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("folder unreadable")))

    def broken(settings, store):
        raise RuntimeError("database down")

    sleeps, calls = run_loop(Settings(), 3, monkeypatch, run_once=broken)
    out = capsys.readouterr().out
    assert len(calls) == 3 and len(sleeps) == 3
    assert "input folder: RuntimeError: folder unreadable" in out and "alert check failed: RuntimeError: database down" in out


def test_the_social_refresh_runs_beside_the_alerts_only_when_turned_on(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "_social_refresh_in_background", lambda settings, hours: started.append(hours))
    settings = Settings()
    Store(settings.state_path).set_setting("social_refresh_hours", "0")
    run_loop(settings, 1, monkeypatch)
    assert started == []
    Store(settings.state_path).set_setting("social_refresh_hours", "2.5")
    run_loop(settings, 1, monkeypatch)
    for thread in threading.enumerate():
        if thread is not threading.current_thread() and thread.daemon:
            thread.join(timeout=2)
    assert started == [2.5]


def test_new_excel_files_are_tracked_automatically_and_removed_videos_stay_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_social_refresh_in_background", lambda *a: None)
    folder = tmp_path / "in"
    folder.mkdir()
    settings = Settings(social_input_dir=str(folder))
    make_xlsx(folder / "raw.xlsx", ["https://youtu.be/IPGz8mYuFi0", "https://www.instagram.com/reel/DgIIAmMxKd8/"])
    run_loop(settings, 1, monkeypatch)
    store = Store(settings.state_path)
    assert len(store.social_videos()) == 2

    store.remove_social_video(store.social_videos()[0]["id"])  # the user stops tracking one
    make_xlsx(folder / "more.xlsx", ["https://x.com/a/status/1"])  # and drops a new file in
    run_loop(settings, 1, monkeypatch)
    assert sorted(v["platform"] for v in Store(settings.state_path).social_videos()) == ["instagram", "x"]


def test_background_refresh_checks_only_due_videos_and_never_stops_the_alert_loop(monkeypatch, capsys):
    from habit10x_marketing_automation import social

    calls = []
    monkeypatch.setattr(cli, "refresh", lambda settings, store, due_hours: calls.append(due_hours)
                        or social.RefreshResult(checked=2, ok=2, alert={"subject": "2 video milestones", "status": "sent"}))
    cli._social_refresh_in_background(Settings(), 6)
    out = capsys.readouterr().out
    assert calls == [6] and "checked 2 video(s)" in out and "2 video milestones (sent)" in out

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "refresh", boom)
    cli._social_refresh_in_background(Settings(), 6)  # must swallow the error
    assert "refresh failed" in capsys.readouterr().out


# ----------------------------------------------------------------------------------------------- one command
class FakeProcess:
    def __init__(self, command, interrupt=False):
        self.command, self.interrupt, self.terminated = command, interrupt, False

    def wait(self, timeout=None):
        if self.interrupt and not self.terminated:
            raise KeyboardInterrupt
        return 0

    def poll(self):
        return 0 if (self.terminated or not self.interrupt) else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def start_everything(monkeypatch, interrupt):
    made, loops = [], []
    monkeypatch.setattr(cli, "startup_report", lambda settings: ["Neon       OK"])
    monkeypatch.setattr(cli, "checker_loop", lambda settings, stop: loops.append(stop))
    monkeypatch.setattr(cli, "_open_when_ready", lambda port, url: None)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda command: made.append(FakeProcess(command, interrupt)) or made[-1])
    return made, loops


def test_traction_starts_the_checker_and_a_local_only_dashboard_in_one_go(monkeypatch, capsys):
    made, loops = start_everything(monkeypatch, interrupt=False)
    assert cli.run(Settings()) == 0
    command = made[0].command
    assert command[1:4] == ["-m", "streamlit", "run"] and command[4].endswith("dashboard.py")
    assert command[command.index("--server.address") + 1] == "localhost"  # the dashboard lists user emails
    assert command[command.index("--server.headless") + 1] == "true"
    assert len(loops) == 1 and "Dashboard: http://localhost:" in capsys.readouterr().out


def test_ctrl_c_stops_the_dashboard_and_the_checker_together(monkeypatch, capsys):
    made, loops = start_everything(monkeypatch, interrupt=True)
    assert cli.run(Settings()) == 0
    assert made[0].terminated is True and loops[0].is_set() is True
    assert "Stopping" in capsys.readouterr().out


def test_main_takes_no_options_and_starts_everything(monkeypatch):
    monkeypatch.setattr(cli, "run", lambda settings: 7)
    assert cli.main([]) == 7
