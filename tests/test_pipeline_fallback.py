"""Crawl retry / notify decisions. No live DMS browser."""

from __future__ import annotations

import json

import pytest

INCIDENT = (
    "Error: Locator.scroll_into_view_if_needed: "
    "Target page, context or browser has been closed"
)


def test_page_closed_incident_is_recoverable():
    from pipeline_fallback import is_recoverable_crawl_failure

    crawl = {"status": "fatal_error", "error": INCIDENT, "fileCount": 0}
    assert is_recoverable_crawl_failure(crawl) is True


def test_cdp_and_login_failures_are_recoverable():
    from pipeline_fallback import is_recoverable_crawl_failure

    cases = [
        {"status": "fatal_error", "error": "Error: Failed to connect to Chrome over CDP on port 9222"},
        {"status": "fatal_error", "error": "RuntimeError: Browser is on the login page. Log in first."},
        {"status": "fatal_error", "error": "RuntimeError: Login page detected. Log in first."},
        {"status": "fatal_error", "error": "TargetClosedError: browser has been closed"},
        {"status": "fatal_error", "error": "Error: WebSocket connection closed"},
    ]
    assert all(is_recoverable_crawl_failure(case) for case in cases)


def test_success_partial_and_logic_errors_are_not_retried():
    from pipeline_fallback import is_recoverable_crawl_failure, will_retry_crawl

    assert is_recoverable_crawl_failure({"status": "ok", "fileCount": 7}) is False
    assert is_recoverable_crawl_failure({"status": "partial", "fileCount": 3}) is False
    assert is_recoverable_crawl_failure({"status": "dry_run"}) is False
    assert is_recoverable_crawl_failure({"status": "fatal_error", "error": "ValueError: quarter must be 1..4"}) is False
    assert will_retry_crawl(
        attempt=1,
        max_attempts=2,
        crawl={"status": "partial", "fileCount": 3},
    ) is False


def test_unknown_fatal_error_without_message_is_retried_once():
    from pipeline_fallback import is_recoverable_crawl_failure

    assert is_recoverable_crawl_failure({"status": "fatal_error", "error": ""}) is True


def test_retry_stops_at_max_attempts_and_skips_dry_run():
    from pipeline_fallback import will_retry_crawl

    crawl = {"status": "fatal_error", "error": INCIDENT}
    assert will_retry_crawl(attempt=1, max_attempts=2, crawl=crawl) is True
    assert will_retry_crawl(attempt=2, max_attempts=2, crawl=crawl) is False
    assert will_retry_crawl(attempt=1, max_attempts=2, crawl=crawl, dry_run=True) is False
    assert will_retry_crawl(attempt=1, max_attempts=2, crawl=crawl, skip_crawl=True) is False


def test_notify_only_on_final_outcome():
    from pipeline_fallback import should_notify_pipeline

    assert should_notify_pipeline(dry_run=False, skip_notify=False, will_retry=True) is False
    assert should_notify_pipeline(dry_run=True, skip_notify=False, will_retry=False) is False
    assert should_notify_pipeline(dry_run=False, skip_notify=True, will_retry=False) is False
    assert should_notify_pipeline(dry_run=False, skip_notify=False, will_retry=False) is True


def test_max_attempts_defaults_to_two_and_is_capped(monkeypatch):
    from pipeline_fallback import resolve_backoff_seconds, resolve_max_attempts

    monkeypatch.delenv("DISTRICT_CRAWL_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("DISTRICT_CRAWL_RETRY_BACKOFF_SECONDS", raising=False)
    assert resolve_max_attempts(None) == 2
    assert resolve_max_attempts(1) == 1
    assert resolve_max_attempts(99) == 3
    monkeypatch.setenv("DISTRICT_CRAWL_MAX_ATTEMPTS", "nope")
    assert resolve_max_attempts(None) == 2
    monkeypatch.setenv("DISTRICT_CRAWL_RETRY_BACKOFF_SECONDS", "8")
    assert resolve_backoff_seconds(None) == 8
    assert resolve_backoff_seconds(0) == 0


def test_format_crawl_error_redacts_password(monkeypatch):
    from pipeline_fallback import format_crawl_error, redact_text

    monkeypatch.setenv("DMS_LOGIN_PASSWORD", "s3cret-value")
    text = format_crawl_error(RuntimeError("closed s3cret-value"))
    assert "s3cret-value" not in text
    assert "RuntimeError" in text
    assert "s3cret-value" not in redact_text("detail s3cret-value")


def test_recover_refreshes_when_already_logged_in():
    from pipeline_fallback import recover_dms_session

    calls = []

    def auto_login(port, reason):
        raise AssertionError("logged_in must not call auto-login")

    deps = {
        "ensure_port": lambda: 9222,
        "inspect": lambda port: {"status": "logged_in", "hint": "ok"},
        "refresh": lambda port: calls.append(("refresh", port)) or "refreshed:ok",
        "auto_login": auto_login,
    }
    result = recover_dms_session("/tmp/unused", deps=deps)
    assert result["ok"] is True
    assert result["action"] == "refresh_dashboard"
    assert result["status"] == "logged_in"
    assert calls == [("refresh", 9222)]


def test_recover_auto_login_when_logged_out():
    from pipeline_fallback import recover_dms_session

    state = {"status": "logged_out"}

    def auto_login(port, reason):
        state["status"] = "logged_in"
        return "auto_login:already"

    deps = {
        "ensure_port": lambda: 9222,
        "inspect": lambda port: dict(state),
        "refresh": lambda port: (_ for _ in ()).throw(AssertionError("login succeeded; do not refresh")),
        "auto_login": auto_login,
    }
    result = recover_dms_session("/tmp/unused", deps=deps)
    assert result == {
        "ok": True,
        "action": "auto_login",
        "status": "logged_in",
        "detail": "auto_login:already",
    }


def test_recover_logged_out_quiet_window_does_not_retry_browser():
    from pipeline_fallback import recover_dms_session

    deps = {
        "ensure_port": lambda: 9222,
        "inspect": lambda port: {"status": "logged_out"},
        "refresh": lambda port: (_ for _ in ()).throw(AssertionError("quiet skip must not refresh")),
        "auto_login": lambda port, reason: "auto_login:skipped:quiet",
    }
    result = recover_dms_session("/tmp/unused", deps=deps)
    assert result["ok"] is False
    assert result["action"] == "auto_login"
    assert result["status"] == "logged_out"
    assert "quiet" in result["detail"]


def test_default_recovery_does_not_launch_a_second_browser(monkeypatch, tmp_path):
    import dfmc_browser_utils as bu
    import pipeline_fallback as pf

    monkeypatch.setattr(bu, "get_default_state_file", lambda root: tmp_path / "missing-state.json")

    def down(*args, **kwargs):
        raise RuntimeError("Browser process (pid=1) is not running. Restart the login browser.")

    monkeypatch.setattr(bu, "ensure_cdp_browser_running", down)
    launched = []
    monkeypatch.setattr(bu, "launch_shared_cdp_browser", lambda *a, **k: launched.append(1) or {"port": 9})
    deps = pf.default_recovery_deps(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        deps["ensure_port"]()
    assert launched == []


def test_recover_browser_down_fails_without_touching_login():
    from pipeline_fallback import recover_dms_session

    def ensure_port():
        raise RuntimeError("Browser process (pid=9) is not running")

    deps = {
        "ensure_port": ensure_port,
        "inspect": lambda port: (_ for _ in ()).throw(AssertionError("browser down")),
        "refresh": lambda port: (_ for _ in ()).throw(AssertionError("browser down")),
        "auto_login": lambda port, reason: (_ for _ in ()).throw(AssertionError("browser down")),
    }
    result = recover_dms_session("/tmp/unused", deps=deps)
    assert result["ok"] is False
    assert result["action"] == "ensure_browser"
    assert "not running" in result["detail"]


def test_pipeline_partial_still_builds_without_retry(pipeline, monkeypatch):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        return {"status": "partial", "error": "", "fileCount": 4, "files": {}}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)
    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=False,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=lambda root: (_ for _ in ()).throw(AssertionError("partial must not recover")),
    )
    assert calls["crawl"] == 1
    assert res["status"] == "ok"
    assert sent == [{"status": "ok", "error": ""}]


def test_recover_falls_back_to_refresh_when_auto_login_missing():
    from pipeline_fallback import recover_dms_session

    seen = {"status": "logged_out"}

    def refresh(port):
        seen["status"] = "logged_in"
        return "refreshed:ok"

    deps = {
        "ensure_port": lambda: 9222,
        "inspect": lambda port: {"status": seen["status"]},
        "refresh": refresh,
        "auto_login": lambda port, reason: "auto_login:unavailable",
    }
    result = recover_dms_session("/tmp/unused", deps=deps)
    assert result["ok"] is True
    assert result["action"] == "refresh_dashboard"
    assert result["status"] == "logged_in"


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    import run_pipeline

    root = tmp_path / "root"
    (root / "output").mkdir(parents=True)
    (root / "download").mkdir()
    monkeypatch.setattr(run_pipeline, "PLUGIN_ROOT", root)
    monkeypatch.setattr(
        run_pipeline,
        "build_from_download",
        lambda download_dir, output_dir: output_dir / "区域各指标情况一览1023.xlsx",
    )
    monkeypatch.delenv("MHERO_REPORT_ENGINE", raising=False)
    monkeypatch.delenv("MHERO_REPORTDB", raising=False)
    return run_pipeline, root


def _install_notify(monkeypatch):
    import feishu_webhook

    sent = []

    def fake(result):
        sent.append({"status": result.get("status"), "error": result.get("error", "")})
        return {"ok": True, "stub": True}

    monkeypatch.setattr(feishu_webhook, "notify_pipeline_result", fake)
    return sent


def test_pipeline_one_success_notify_after_retry(pipeline, monkeypatch, capsys):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        if calls["crawl"] == 1:
            return {"status": "fatal_error", "error": INCIDENT, "fileCount": 0, "files": {}}
        return {"status": "ok", "error": "", "fileCount": 7, "files": {}}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)
    recoveries = []

    def recover(plugin_root):
        recoveries.append(plugin_root)
        return {"ok": True, "action": "refresh_dashboard", "status": "logged_in", "detail": "refreshed:ok"}

    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=False,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=recover,
    )
    log = capsys.readouterr().out
    assert calls["crawl"] == 2
    assert len(recoveries) == 1
    assert res["status"] == "ok"
    assert res["error"] == ""
    assert res["attempt"] == 2
    assert sent == [{"status": "ok", "error": ""}]
    assert "[pipeline] attempt 1/2" in log
    assert "[pipeline] attempt 2/2" in log
    assert "recovery action=refresh_dashboard" in log
    assert "final status=ok" in log
    manifest = json.loads((root / "output" / "pipeline_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "ok"


def test_pipeline_notifies_once_when_retries_exhausted(pipeline, monkeypatch, capsys):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    builds = []
    monkeypatch.setattr(
        run_pipeline,
        "build_from_download",
        lambda *a, **k: builds.append(1),
    )

    def crawl(**kwargs):
        return {"status": "fatal_error", "error": INCIDENT, "fileCount": 0, "files": {}}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)
    monkeypatch.setattr(
        run_pipeline,
        "recover_dms_session",
        lambda plugin_root: {"ok": True, "action": "refresh_dashboard", "status": "logged_in", "detail": "refreshed:ok"},
    )
    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=False,
        max_attempts=2,
        retry_backoff_seconds=0,
    )
    log = capsys.readouterr().out
    assert res["status"] == "crawl_fatal_error"
    assert builds == []
    assert len(sent) == 1
    assert sent[0]["status"] == "crawl_fatal_error"
    assert "final status=crawl_fatal_error" in log
    assert "attempt 2/2" in log


def test_pipeline_does_not_retry_logic_error(pipeline, monkeypatch):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0, "recover": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        return {"status": "fatal_error", "error": "ValueError: quarter must be 1..4", "fileCount": 0}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)

    def recover(plugin_root):
        calls["recover"] += 1
        return {"ok": True, "action": "refresh_dashboard", "status": "logged_in", "detail": "refreshed:ok"}

    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=False,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=recover,
    )
    assert calls == {"crawl": 1, "recover": 0}
    assert res["status"] == "crawl_fatal_error"
    assert len(sent) == 1


def test_pipeline_unusable_session_stops_before_second_crawl(pipeline, monkeypatch, capsys):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        return {"status": "fatal_error", "error": "RuntimeError: Login page detected. Log in first.", "fileCount": 0}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)

    def recover(plugin_root):
        return {"ok": False, "action": "auto_login", "status": "logged_out", "detail": "auto_login:skipped:quiet"}

    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=False,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=recover,
    )
    log = capsys.readouterr().out
    assert calls["crawl"] == 1
    assert res["status"] == "crawl_fatal_error"
    assert "recovery unusable" in log
    assert len(sent) == 1
    assert "quiet" in sent[0]["error"]


def test_pipeline_skip_notify_still_retries_without_feishu(pipeline, monkeypatch):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        if calls["crawl"] == 1:
            return {"status": "fatal_error", "error": INCIDENT, "fileCount": 0}
        return {"status": "ok", "error": "", "fileCount": 7}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)
    res = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_notify=True,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=lambda root: {"ok": True, "action": "refresh_dashboard", "status": "logged_in", "detail": "refreshed:ok"},
    )
    assert res["status"] == "ok"
    assert calls["crawl"] == 2
    assert sent == []
    assert "feishu" not in res


def test_pipeline_dry_run_and_skip_crawl_do_not_retry(pipeline, monkeypatch):
    run_pipeline, root = pipeline
    sent = _install_notify(monkeypatch)
    calls = {"crawl": 0}

    def crawl(**kwargs):
        calls["crawl"] += 1
        assert kwargs.get("dry_run") is True
        return {"status": "dry_run", "error": "", "fileCount": 0}

    monkeypatch.setattr(run_pipeline, "crawl", crawl)
    dry = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        dry_run=True,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=lambda root: (_ for _ in ()).throw(AssertionError("dry-run must not recover")),
    )
    assert dry["status"] == "dry_run"
    assert calls["crawl"] == 1
    assert sent == []

    def boom(**kwargs):
        raise AssertionError("skip-crawl must not crawl")

    monkeypatch.setattr(run_pipeline, "crawl", boom)
    skipped = run_pipeline.run_pipeline(
        year=2026,
        quarter=4,
        skip_crawl=True,
        skip_notify=True,
        max_attempts=2,
        retry_backoff_seconds=0,
        recover=lambda root: (_ for _ in ()).throw(AssertionError("skip-crawl must not recover")),
    )
    assert skipped["status"] == "ok"
    assert sent == []
