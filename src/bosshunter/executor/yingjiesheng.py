"""Single-job YingJieSheng application flow with a separate human confirmation.

This module never starts an application in ``prepare``. The site's apply button
can submit immediately, so ``confirm`` consumes a short-lived, job-bound token
before clicking it once. External destinations remain manual-only.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable
from urllib.parse import urlparse

from bosshunter.browser import click, close_tab, evaluate, navigate, new_tab, wait_for_load
from bosshunter.db import get_active_platform_safety_lock
from bosshunter.platform_safety import PlatformAccessGuard, PlatformSafetyStop
from bosshunter.throttle import SendWindowChecker


APPLY_BUTTON = ".detail-title-right .delivery-btn:not(.hasdelivery):not(.disabled)"
CONFIRM_TTL_SECONDS = 120
MIN_APPLY_INTERVAL_SECONDS = 60
MAX_DAILY_APPLICATIONS = 5

JS_PREFLIGHT = r"""
(() => {
  const text = (document.body?.innerText || '').slice(0, 3000);
  const signal = `${document.title} ${text}`;
  if (/Access Verification|滑块验证|安全验证|人机验证|验证码/i.test(signal))
    return JSON.stringify({status:'verification'});
  if (/访问过于频繁|请求过于频繁|访问受限|今日投递太多|今日申请太多|rate limit/i.test(signal))
    return JSON.stringify({status:'rate_limit'});
  const match = location.pathname.match(/^\/jobdetail\/(\d+)\.html$/);
  if (location.hostname !== 'q.yingjiesheng.com' || !match)
    return JSON.stringify({status:'unexpected_destination'});
  const title = document.querySelector('.detail-title-left-top .job')?.innerText?.trim() || '';
  const button = document.querySelector('.detail-title-right .delivery-btn');
  const safeButtons = document.querySelectorAll('.detail-title-right .delivery-btn:not(.hasdelivery):not(.disabled)');
  const label = button?.innerText?.trim() || '';
  if (!title || !button) return JSON.stringify({status:'waiting', job_id:match[1]});
  if (label === '已申请') return JSON.stringify({status:'already_applied', job_id:match[1], title});
  if (label === '前往投递') return JSON.stringify({status:'external', job_id:match[1], title});
  if (label !== '立即申请' || safeButtons.length !== 1 || safeButtons[0] !== button)
    return JSON.stringify({status:'selector_changed', job_id:match[1], title});
  const vm = [...document.querySelectorAll('.btn')].map(el => el.__vue__).find(v => v?.$store?.state?.commonStore);
  if (!vm) return JSON.stringify({status:'login_unknown', job_id:match[1], title});
  if (!vm.$store.state.commonStore.accountid)
    return JSON.stringify({status:'login_required', job_id:match[1], title});
  return JSON.stringify({status:'ready', job_id:match[1], title});
})()
"""

JS_OUTCOME = r"""
(() => {
  const match = location.pathname.match(/^\/jobdetail\/(\d+)\.html$/);
  if (location.hostname !== 'q.yingjiesheng.com' || !match)
    return JSON.stringify({status:'unexpected_destination'});
  const text = (document.body?.innerText || '').slice(0, 3000);
  const signal = `${document.title} ${text}`;
  if (/Access Verification|滑块验证|安全验证|人机验证|验证码/i.test(signal))
    return JSON.stringify({status:'verification'});
  if (/访问过于频繁|请求过于频繁|访问受限|今日投递太多|今日申请太多|rate limit/i.test(signal))
    return JSON.stringify({status:'rate_limit'});
  if (/请先完善简历|请先上传简历|简历不完整|暂无简历/.test(signal))
    return JSON.stringify({status:'resume_required'});
  if (/登录后申请|请先登录|扫码登录/.test(signal))
    return JSON.stringify({status:'login_required'});
  const label = document.querySelector('.detail-title-right .delivery-btn')?.innerText?.trim() || '';
  const toast = [...document.querySelectorAll('.el-message, .el-notification')]
    .filter(el => getComputedStyle(el).display !== 'none')
    .map(el => el.innerText).join(' ');
  if (label === '已申请')
    return JSON.stringify({status:'applied', job_id:match[1]});
  if (/申请失败|职位已过期|7天内已申请/.test(toast))
    return JSON.stringify({status:'not_applied'});
  return JSON.stringify({status:'waiting'});
})()
"""


class YingjieshengActionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class YingjieshengActionBrowser:
    new_tab: Callable[..., str | None] = new_tab
    navigate: Callable[[str, str], bool] = navigate
    wait_for_load: Callable[..., bool] = wait_for_load
    evaluate: Callable[..., Any] = evaluate
    click: Callable[[str, str], bool] = click
    close_tab: Callable[[str], bool] = close_tab


@dataclass(frozen=True)
class PendingApplication:
    job_id: str
    source_job_id: str
    target_id: str
    title: str
    created_at: float


def _page_result(raw: Any) -> dict[str, str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise YingjieshengActionError("unexpected_response", "应届生页面响应异常，已停止操作")
    return {key: str(item) for key, item in value.items() if item is not None}


def _first_party_job_url(job: dict[str, Any]) -> tuple[str, str]:
    source_id = str(job.get("source_job_id") or "")
    url = str(job.get("url") or "")
    parsed = urlparse(url)
    if str(job.get("source_platform") or "") != "yingjiesheng":
        raise YingjieshengActionError("wrong_platform", "只能处理应届生求职岗位")
    if not re.fullmatch(r"\d{1,20}", source_id):
        raise YingjieshengActionError("external_manual_only", "外链或未知岗位只能在原平台手动投递")
    if (
        parsed.scheme != "https" or parsed.hostname != "q.yingjiesheng.com"
        or parsed.username or parsed.password
        or parsed.path != f"/jobdetail/{source_id}.html"
        or parsed.query or parsed.fragment
    ):
        raise YingjieshengActionError("external_manual_only", "外链岗位只能在原平台手动投递")
    return f"https://q.yingjiesheng.com/jobdetail/{source_id}.html", source_id


def _check_send_policy(conn: sqlite3.Connection, config: dict[str, Any]) -> None:
    if get_active_platform_safety_lock(conn):
        raise YingjieshengActionError("safety_lock", "账号处于安全锁定期，已停止操作")
    throttle = config.get("throttle") if isinstance(config.get("throttle"), dict) else {}
    windows = throttle.get("send_windows")
    if not isinstance(windows, list) or not windows:
        raise YingjieshengActionError("send_window_missing", "请先配置投递时间窗口")
    checker = SendWindowChecker(windows)
    if not checker.has_valid_windows() or not checker.is_active():
        raise YingjieshengActionError("outside_send_window", "当前不在投递时间窗口")
    try:
        limit = min(max(int(throttle.get("daily_limit", MAX_DAILY_APPLICATIONS) or MAX_DAILY_APPLICATIONS), 1), MAX_DAILY_APPLICATIONS)
    except (TypeError, ValueError):
        raise YingjieshengActionError("daily_limit_invalid", "投递次数上限配置无效") from None
    row = conn.execute(
        """SELECT COUNT(*) AS count FROM history h JOIN jobs j ON j.id = h.job_id
           WHERE j.source_platform = 'yingjiesheng' AND h.action IN ('sent', 'manual_sent')
             AND date(h.created_at, 'localtime') = date('now', 'localtime')"""
    ).fetchone()
    if int(row["count"] if row else 0) >= limit:
        raise YingjieshengActionError("daily_limit", "今日应届生求职投递已达上限")
    recent = conn.execute(
        """SELECT h.created_at FROM history h JOIN jobs j ON j.id = h.job_id
           WHERE j.source_platform = 'yingjiesheng' AND h.action IN ('sent', 'manual_sent')
           ORDER BY h.id DESC LIMIT 1"""
    ).fetchone()
    if recent:
        try:
            timestamp = datetime.fromisoformat(str(recent["created_at"]))
        except (TypeError, ValueError):
            raise YingjieshengActionError("history_invalid", "近期投递记录时间无效，已停止操作") from None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - timestamp).total_seconds() < MIN_APPLY_INTERVAL_SECONDS:
            raise YingjieshengActionError("cooldown", "两次应届生求职投递间隔不足，请稍后再试")


class YingjieshengApplicationService:
    def __init__(
        self, *, browser: YingjieshengActionBrowser | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.browser = browser or YingjieshengActionBrowser()
        self.monotonic = monotonic
        self.sleep = sleep
        self._pending: dict[str, PendingApplication] = {}
        self._lock = RLock()

    def _inspect(self, target: str, expression: str) -> dict[str, str]:
        for attempt in range(8):
            result = _page_result(self.browser.evaluate(target, expression))
            if result["status"] != "waiting":
                return result
            if attempt < 7:
                self.sleep(1)
        raise YingjieshengActionError("render_timeout", "应届生页面未加载完成，已停止操作")

    def _check_page(self, result: dict[str, str], source_id: str, title: str) -> None:
        status = result["status"]
        if status in {"verification", "rate_limit"}:
            raise YingjieshengActionError(status, "应届生页面出现验证或访问限制，已停止操作")
        if status != "ready":
            raise YingjieshengActionError(status, "应届生职位目前无法安全申请")
        if result.get("job_id") != source_id or result.get("title") != title:
            raise YingjieshengActionError("job_changed", "页面职位与确认的岗位不一致")

    def prepare(self, conn: sqlite3.Connection, job: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
        url, source_id = _first_party_job_url(job)
        if job.get("deleted_at") is not None:
            raise YingjieshengActionError("job_deleted", "已删除的岗位不能申请")
        if str(job.get("status") or "") in {"sent", "replied", "resume_sent", "needs_resume", "follow_up_sent"}:
            raise YingjieshengActionError("already_sent", "该岗位已有投递记录")
        _check_send_policy(conn, config)
        try:
            PlatformAccessGuard(conn, config, "yingjiesheng_apply", "yingjiesheng").reserve("detail", daily_limit=20)
        except PlatformSafetyStop as exc:
            raise YingjieshengActionError(exc.reason, "应届生页面访问已达到安全限制") from exc
        target = self.browser.new_tab("about:blank", background=True)
        if not target:
            raise YingjieshengActionError("browser_disconnected", "无法打开应届生职位页面")
        try:
            if not self.browser.navigate(target, url):
                raise YingjieshengActionError("navigation_failed", "无法打开应届生职位页面")
            self.browser.wait_for_load(target, timeout=15)
            result = self._inspect(target, JS_PREFLIGHT)
            self._check_page(result, source_id, str(job.get("title") or ""))
            token = secrets.token_urlsafe(24)
            with self._lock:
                expired = [key for key, item in self._pending.items() if self.monotonic() - item.created_at > CONFIRM_TTL_SECONDS]
                for key in expired:
                    self.browser.close_tab(self._pending.pop(key).target_id)
                self._pending[token] = PendingApplication(str(job["id"]), source_id, target, result["title"], self.monotonic())
            return {"confirmation_token": token, "job_id": str(job["id"]), "title": result["title"], "company": str(job.get("company") or ""), "url": url}
        except YingjieshengActionError as exc:
            self.browser.close_tab(target)
            if exc.code in {"verification", "rate_limit"}:
                PlatformAccessGuard(conn, config, "yingjiesheng_apply", "yingjiesheng").lock(exc.code)
            raise
        except Exception as exc:
            self.browser.close_tab(target)
            raise YingjieshengActionError("browser_error", "浏览器检查失败，已停止操作") from exc

    def cancel(self, token: str) -> None:
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending:
            self.browser.close_tab(pending.target_id)

    def confirm(
        self, conn: sqlite3.Connection, job: dict[str, Any], config: dict[str, Any],
        *, token: str, confirmed: bool,
    ) -> dict[str, str]:
        if confirmed is not True:
            raise YingjieshengActionError("confirmation_required", "申请前必须由用户逐岗确认")
        with self._lock:
            pending = self._pending.pop(token, None)
        if pending is None:
            raise YingjieshengActionError("confirmation_expired", "申请确认已失效，请重新预览")
        clicked = False
        try:
            if self.monotonic() - pending.created_at > CONFIRM_TTL_SECONDS:
                raise YingjieshengActionError("confirmation_expired", "申请确认已过期，请重新预览")
            url, source_id = _first_party_job_url(job)
            if pending.job_id != str(job.get("id")) or pending.source_job_id != source_id or pending.title != str(job.get("title") or ""):
                raise YingjieshengActionError("job_changed", "确认岗位与当前岗位不一致")
            if job.get("deleted_at") is not None:
                raise YingjieshengActionError("job_deleted", "已删除的岗位不能申请")
            if str(job.get("status") or "") in {"sent", "replied", "resume_sent", "needs_resume", "follow_up_sent"}:
                raise YingjieshengActionError("already_sent", "该岗位已有投递记录")
            _check_send_policy(conn, config)
            before = self._inspect(pending.target_id, JS_PREFLIGHT)
            self._check_page(before, source_id, pending.title)
            if not self.browser.click(pending.target_id, APPLY_BUTTON):
                raise YingjieshengActionError("click_failed", "未能点击申请按钮，未记录为已投递")
            clicked = True
            outcome = self._inspect(pending.target_id, JS_OUTCOME)
            if outcome["status"] in {"verification", "rate_limit"}:
                raise YingjieshengActionError(outcome["status"], "申请后出现验证或访问限制，请手动检查结果")
            if outcome["status"] != "applied" or outcome.get("job_id") != source_id:
                raise YingjieshengActionError(outcome["status"], "申请结果未获确认，请打开原岗位人工检查")
            with conn:
                cursor = conn.execute(
                    "UPDATE jobs SET status='sent', updated_at=CURRENT_TIMESTAMP WHERE id=? AND source_platform='yingjiesheng' AND deleted_at IS NULL AND status NOT IN ('sent','replied','resume_sent','needs_resume','follow_up_sent')",
                    (pending.job_id,),
                )
                if cursor.rowcount != 1:
                    raise YingjieshengActionError("job_changed", "岗位状态已变化，请人工核对投递结果")
                conn.execute(
                    "INSERT INTO history (job_id, action, detail) VALUES (?, 'sent', ?)",
                    (pending.job_id, "用户逐岗确认后在应届生求职申请，页面显示已申请"),
                )
            return {"status": "applied", "job_id": pending.job_id, "url": url}
        except YingjieshengActionError as exc:
            if exc.code in {"verification", "rate_limit"}:
                PlatformAccessGuard(conn, config, "yingjiesheng_apply", "yingjiesheng").lock(exc.code)
            elif clicked and exc.code != "not_applied":
                PlatformAccessGuard(conn, config, "yingjiesheng_apply", "yingjiesheng").lock("application_outcome_unknown")
            raise
        except Exception as exc:
            if clicked:
                PlatformAccessGuard(conn, config, "yingjiesheng_apply", "yingjiesheng").lock("application_outcome_unknown")
            raise YingjieshengActionError("browser_error", "申请结果未知，请打开原岗位人工检查") from exc
        finally:
            self.browser.close_tab(pending.target_id)
