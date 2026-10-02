"""Offline checks for the single-job application confirmation boundary."""

from __future__ import annotations

import sqlite3

import pytest

from bosshunter.db import get_active_platform_safety_lock, get_db
from bosshunter.executor.yingjiesheng import (
    JS_OUTCOME,
    YingjieshengActionError,
    YingjieshengApplicationService,
)


class FakeBrowser:
    def __init__(self, *, preflight: str = "ready", outcome: str = "applied") -> None:
        self.preflight = preflight
        self.outcome = outcome
        self.clicks: list[str] = []
        self.closed: list[str] = []
        self.urls: list[str] = []

    def new_tab(self, url: str, *, background: bool) -> str:
        assert url == "about:blank" and background
        return "test-tab"

    def navigate(self, target: str, url: str) -> bool:
        self.urls.append(url)
        return True

    def wait_for_load(self, target: str, *, timeout: float) -> bool:
        return True

    def evaluate(self, target: str, expression: str) -> dict[str, str]:
        if expression == JS_OUTCOME:
            return {"status": self.outcome, "job_id": "1001"}
        return {"status": self.preflight, "job_id": "1001", "title": "AI 工程师"}

    def click(self, target: str, selector: str) -> bool:
        self.clicks.append(selector)
        return True

    def close_tab(self, target: str) -> bool:
        self.closed.append(target)
        return True


@pytest.fixture
def case(tmp_path, monkeypatch):
    from bosshunter.executor import yingjiesheng as module

    monkeypatch.setattr(module.SendWindowChecker, "is_active", lambda self: True)
    conn = get_db(tmp_path / "test.db")
    conn.execute(
        """INSERT INTO jobs (id, source_platform, source_job_id, title, company, url)
        VALUES ('yingjiesheng:1001', 'yingjiesheng', '1001', 'AI 工程师', '示例公司',
                'https://q.yingjiesheng.com/jobdetail/1001.html')"""
    )
    conn.commit()
    config = {"throttle": {"send_windows": ["09:00-16:00"], "daily_limit": 5}, "safety": {"risk_lock_minutes": 10}}
    yield conn, config
    conn.close()


def job(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT * FROM jobs WHERE id='yingjiesheng:1001'").fetchone())


def test_prepare_is_read_only_and_confirm_clicks_once(case):
    conn, config = case
    browser = FakeBrowser()
    service = YingjieshengApplicationService(browser=browser, sleep=lambda _: None)
    preview = service.prepare(conn, job(conn), config)
    assert browser.clicks == []
    assert preview["title"] == "AI 工程师"
    with pytest.raises(YingjieshengActionError, match="逐岗确认"):
        service.confirm(conn, job(conn), config, token=preview["confirmation_token"], confirmed=False)
    assert browser.clicks == []
    result = service.confirm(conn, job(conn), config, token=preview["confirmation_token"], confirmed=True)
    assert result["status"] == "applied"
    assert len(browser.clicks) == 1
    assert browser.closed == ["test-tab"]
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (result["job_id"],)).fetchone()[0] == "sent"
    assert conn.execute("SELECT action FROM history").fetchone()[0] == "sent"
    with pytest.raises(YingjieshengActionError) as exc:
        service.confirm(conn, job(conn), config, token=preview["confirmation_token"], confirmed=True)
    assert exc.value.code == "confirmation_expired"
    assert len(browser.clicks) == 1


@pytest.mark.parametrize("url,source_id", [
    ("https://careers.example.org/jobs/1", "ext-1"),
    ("https://q.yingjiesheng.com/jobdetail/1001.html?next=other", "1001"),
    ("https://q.yingjiesheng.com/jobdetail/1002.html", "1001"),
])
def test_external_or_changed_url_never_opens_browser(case, url, source_id):
    conn, config = case
    browser = FakeBrowser()
    service = YingjieshengApplicationService(browser=browser)
    record = job(conn) | {"url": url, "source_job_id": source_id}
    with pytest.raises(YingjieshengActionError) as exc:
        service.prepare(conn, record, config)
    assert exc.value.code == "external_manual_only"
    assert browser.urls == browser.clicks == []


@pytest.mark.parametrize("signal", ["verification", "rate_limit"])
def test_safety_signal_locks_account_without_click(case, signal):
    conn, config = case
    browser = FakeBrowser(preflight=signal)
    service = YingjieshengApplicationService(browser=browser, sleep=lambda _: None)
    with pytest.raises(YingjieshengActionError) as exc:
        service.prepare(conn, job(conn), config)
    assert exc.value.code == signal
    assert browser.clicks == []
    assert browser.closed == ["test-tab"]
    assert get_active_platform_safety_lock(conn)["reason"] == signal


def test_uncertain_outcome_has_no_retry_or_false_sent_record(case):
    conn, config = case
    browser = FakeBrowser(outcome="not_applied")
    service = YingjieshengApplicationService(browser=browser, sleep=lambda _: None)
    token = service.prepare(conn, job(conn), config)["confirmation_token"]
    with pytest.raises(YingjieshengActionError) as exc:
        service.confirm(conn, job(conn), config, token=token, confirmed=True)
    assert exc.value.code == "not_applied"
    assert len(browser.clicks) == 1
    assert conn.execute("SELECT status FROM jobs WHERE id='yingjiesheng:1001'").fetchone()[0] == "pending"
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


def test_cooldown_and_daily_limit_include_manual_sent(case):
    conn, config = case
    conn.execute("INSERT INTO history (job_id, action) VALUES ('yingjiesheng:1001', 'manual_sent')")
    conn.commit()
    browser = FakeBrowser()
    service = YingjieshengApplicationService(browser=browser)
    with pytest.raises(YingjieshengActionError) as exc:
        service.prepare(conn, job(conn), config)
    assert exc.value.code == "cooldown"
    config["throttle"]["daily_limit"] = 1
    with pytest.raises(YingjieshengActionError) as exc:
        service.prepare(conn, job(conn), config)
    assert exc.value.code == "daily_limit"
    assert browser.urls == browser.clicks == []


def test_missing_send_window_fails_closed(case):
    conn, config = case
    config["throttle"]["send_windows"] = []
    service = YingjieshengApplicationService(browser=FakeBrowser())
    with pytest.raises(YingjieshengActionError) as exc:
        service.prepare(conn, job(conn), config)
    assert exc.value.code == "send_window_missing"
