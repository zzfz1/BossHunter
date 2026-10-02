"""Manual progress tracking never contacts a recruitment platform."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from bosshunter.db import get_db
from bosshunter.yingjiesheng_progress import ProgressError, get_manual_progress, record_manual_progress


def test_manual_progress_requires_prior_application_and_confirmation(tmp_path):
    conn = get_db(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO jobs (id, source_platform, title, company, status) VALUES ('yingjiesheng:1001', 'yingjiesheng', 'AI 工程师', '示例公司', 'pending')"
    )
    conn.commit()
    with pytest.raises(ProgressError) as exc:
        record_manual_progress(conn, "yingjiesheng:1001", "waiting", "", confirmed=True)
    assert exc.value.code == "not_applied"
    conn.execute("UPDATE jobs SET status='sent' WHERE id='yingjiesheng:1001'")
    conn.commit()
    with pytest.raises(ProgressError) as exc:
        record_manual_progress(conn, "yingjiesheng:1001", "waiting", "", confirmed=False)
    assert exc.value.code == "confirmation_required"
    assert get_manual_progress(conn, "yingjiesheng:1001")["events"] == []
    conn.close()


def test_progress_and_reminder_are_append_only(tmp_path):
    conn = get_db(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO jobs (id, source_platform, title, company, status) VALUES ('yingjiesheng:1001', 'yingjiesheng', 'AI 工程师', '示例公司', 'sent')"
    )
    conn.commit()
    check = (date.today() + timedelta(days=7)).isoformat()
    first = record_manual_progress(conn, "yingjiesheng:1001", "waiting", check, confirmed=True)
    assert first["source"] == "manual" and first["check_due"] is False
    second = record_manual_progress(conn, "yingjiesheng:1001", "interview", "", confirmed=True)
    assert [event["status"] for event in second["events"]] == ["interview", "waiting"]
    assert conn.execute("SELECT status FROM jobs WHERE id='yingjiesheng:1001'").fetchone()[0] == "sent"
    conn.close()


def test_invalid_manual_progress_rejected(tmp_path):
    conn = get_db(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO jobs (id, source_platform, title, company, status) VALUES ('yingjiesheng:1001', 'yingjiesheng', 'AI 工程师', '示例公司', 'sent')"
    )
    conn.commit()
    for status, next_date, code in [
        ("platform_claimed_status", "", "invalid_status"),
        ("waiting", "not-a-date", "invalid_date"),
        ("waiting", (date.today() - timedelta(days=1)).isoformat(), "invalid_date"),
    ]:
        with pytest.raises(ProgressError) as exc:
            record_manual_progress(conn, "yingjiesheng:1001", status, next_date, confirmed=True)
        assert exc.value.code == code
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0
    conn.close()
