"""Local, user-entered YingJieSheng application progress and check reminders."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any


PROGRESS_LABELS = {
    "waiting": "待反馈",
    "reviewing": "筛选中",
    "interview": "面试中",
    "offer": "已录用",
    "rejected": "未通过",
    "withdrawn": "已撤回",
}


class ProgressError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _eligible_job(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute(
        "SELECT source_platform, status, deleted_at FROM jobs WHERE id = ?", (job_id,),
    ).fetchone()
    if not row or row["source_platform"] != "yingjiesheng" or row["deleted_at"] is not None:
        raise ProgressError("job_not_found", "应届生岗位不存在或已删除")
    if row["status"] not in {"sent", "replied", "resume_sent", "needs_resume", "follow_up_sent"}:
        raise ProgressError("not_applied", "请先在平台完成投递并标记已投递")


def get_manual_progress(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    _eligible_job(conn, job_id)
    rows = conn.execute(
        "SELECT detail, created_at FROM history WHERE job_id = ? AND action = 'yingjiesheng_progress' ORDER BY id DESC LIMIT 20",
        (job_id,),
    ).fetchall()
    events = []
    for row in rows:
        try:
            detail = json.loads(row["detail"] or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("status") not in PROGRESS_LABELS:
            continue
        events.append({
            "status": detail["status"], "label": PROGRESS_LABELS[detail["status"]],
            "next_check_date": str(detail.get("next_check_date") or ""),
            "created_at": row["created_at"],
        })
    latest = events[0] if events else None
    check_date = str(latest["next_check_date"] if latest else "")
    return {
        "job_id": job_id, "source": "manual", "latest": latest, "events": events,
        "check_due": bool(check_date and check_date <= date.today().isoformat()),
    }


def record_manual_progress(
    conn: sqlite3.Connection, job_id: str, status: str, next_check_date: str,
    *, confirmed: bool,
) -> dict[str, Any]:
    if confirmed is not True:
        raise ProgressError("confirmation_required", "必须确认已在平台人工查看进度")
    _eligible_job(conn, job_id)
    if status not in PROGRESS_LABELS:
        raise ProgressError("invalid_status", "请选择有效的进度状态")
    next_check_date = str(next_check_date or "").strip()
    if next_check_date:
        try:
            check = date.fromisoformat(next_check_date)
        except ValueError:
            raise ProgressError("invalid_date", "下次检查日期无效") from None
        if not date.today() <= check <= date.today() + timedelta(days=365):
            raise ProgressError("invalid_date", "下次检查日期须在未来一年内")
    with conn:
        conn.execute(
            "INSERT INTO history (job_id, action, detail) VALUES (?, 'yingjiesheng_progress', ?)",
            (job_id, json.dumps({"status": status, "next_check_date": next_check_date}, ensure_ascii=False)),
        )
    return get_manual_progress(conn, job_id)
