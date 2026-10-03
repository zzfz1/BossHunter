"""Read-only YingJieSheng collector using the site's rendered search results."""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlparse

from bosshunter.browser import click, close_tab, evaluate, navigate, new_tab, solve_yingjiesheng_slider_once, wait_for_load
from bosshunter.collection.base import CollectionBlockedError, CollectorHooks
from bosshunter.collection.models import JobCandidate, PlatformCollectionRequest, PlatformCollectionResult


SEARCH_ROOT = "https://q.yingjiesheng.com/jobs/search/"
PAGE_LIMIT = 3
PAGE_DELAY = (15.0, 25.0)
DETAIL_DELAY = (6.0, 12.0)

# The search API uses dynamic request signing and account headers. Let the site
# request its own JSON data, then read the public job objects already rendered
# on the page. The visible cards have no job link or ID in their HTML.
JS_LIST = r"""
(() => {
  const body = document.body?.innerText || '';
  const signal = `${document.title} ${body.slice(0, 1500)}`;
  if (/Access Verification|滑块验证|安全验证|人机验证|验证码|slide to complete/i.test(signal))
    return JSON.stringify({status:'verification'});
  if (/访问过于频繁|请求过于频繁|访问受限|rate limit|too many requests/i.test(signal))
    return JSON.stringify({status:'rate_limit'});
  if (/请登录|登录后查看|扫码登录|立即登录/.test(signal) && !document.querySelector('.search-list-item-wrapper'))
    return JSON.stringify({status:'login_required'});
  const cards = [...document.querySelectorAll('.search-list-item-wrapper')];
  if (!cards.length) {
    return JSON.stringify({status: /暂无符合条件的职位|没有找到相关职位|暂无职位/.test(body) ? 'empty' : (document.querySelector('.search-list') || body.length < 300) ? 'waiting' : 'selector_changed'});
  }
  const jobButton = card => [...card.querySelectorAll('.btn')].find(el => el.__vue__?._props?.jobinfo);
  const first = jobButton(cards[0])?.__vue__;
  if (!first) return JSON.stringify({status:'waiting'});
  if (first?.$parent?._props?.loading) return JSON.stringify({status:'waiting'});
  const jobs = cards.map(card => {
    const job = jobButton(card)?.__vue__?._props?.jobinfo;
    if (!job) return null;
    const id = String(job.jobid || '');
    const external = String(job.isJump) === '1' || String(job.isReprintJob) === '1';
    const url = external
      ? (job.reprintFromUrl || job.jumpUrlHttp || '')
      : /^\d+$/.test(id) ? `https://q.yingjiesheng.com/jobdetail/${id}.html` : '';
    return {
      source_job_id: id,
      title: String(job.jobname || ''),
      company: String(job.coname || ''),
      city: String(job.jobarea || ''),
      salary: String(job.providesalary || ''),
      education: String(job.degree || ''),
      experience: String(job.workyear || ''),
      recruitment_type: String(job.jobterm || ''),
      url
    };
  });
  const active = document.querySelector('.el-pagination .el-pager .number.active');
  const next = document.querySelector('.el-pagination .btn-next');
  return JSON.stringify({status:'ready', jobs, page_index:active ? Number(active.innerText) : 1, has_next:!!next && !next.disabled});
})()
"""

JS_DETAIL = r"""
(() => {
  if (location.hostname !== 'q.yingjiesheng.com')
    return JSON.stringify({status:'external', url:location.href});
  const body = document.body?.innerText || '';
  const signal = `${document.title} ${body.slice(0, 1500)}`;
  if (/Access Verification|滑块验证|安全验证|人机验证|验证码|slide to complete/i.test(signal))
    return JSON.stringify({status:'verification'});
  if (/访问过于频繁|请求过于频繁|访问受限|rate limit|too many requests/i.test(signal))
    return JSON.stringify({status:'rate_limit'});
  if (/请登录|登录后查看|扫码登录|立即登录/.test(signal) && !document.querySelector('.detail-content'))
    return JSON.stringify({status:'login_required'});
  const content = document.querySelector('.detail-content-common.jobinfo .text') || document.querySelector('.detail-content');
  if (!content) return JSON.stringify({status:body.length < 300 ? 'waiting' : 'selector_changed'});
  const read = selectors => selectors.map(s => document.querySelector(s)?.innerText?.trim()).find(Boolean) || '';
  return JSON.stringify({
    status:'ready',
    title:read(['.detail-title-left-top .job', '.detail-title-left-name', 'h1']),
    company:read(['.detail-content-compnav-center']),
    city:read(['.detail-title-left-center .item:first-child span']),
    salary:read(['.detail-title-left-top .salary']),
    jd:content.innerText.trim(),
    url:location.href
  });
})()
"""


@dataclass
class YingjieshengBrowser:
    new_tab: Callable[..., str | None] = new_tab
    close_tab: Callable[[str], bool] = close_tab
    navigate: Callable[[str, str], bool] = navigate
    click: Callable[[str, str], bool] = click
    evaluate: Callable[..., Any] = evaluate
    wait_for_load: Callable[..., bool] = wait_for_load
    solve_slider_once: Callable[[str], bool] = solve_yingjiesheng_slider_once


def _payload(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_url(value: str, *, search: bool = False) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if search and (parsed.hostname != "q.yingjiesheng.com" or not parsed.path.startswith("/jobs/search/")):
        return ""
    return value


def candidate_from_list(raw: Any, keyword: str) -> JobCandidate | None:
    if not isinstance(raw, dict):
        return None
    url = _safe_url(str(raw.get("url") or "").strip())
    title = str(raw.get("title") or "").strip()
    company = str(raw.get("company") or "").strip()
    if not url or not title or not company:
        return None
    parsed = urlparse(url)
    match = re.search(r"/jobdetail/(\d+)\.html$", parsed.path)
    raw_id = str(raw.get("source_job_id") or "").strip()
    source_id = (
        match.group(1) if match else raw_id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", raw_id)
        else "external-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    )
    return JobCandidate(
        platform="yingjiesheng", source_job_id=source_id, title=title, company=company,
        city=str(raw.get("city") or "").strip(), salary=str(raw.get("salary") or "").strip(),
        education=str(raw.get("education") or "").strip(), experience=str(raw.get("experience") or "").strip(),
        url=url, source_keyword=keyword,
        recruitment_type="campus" if str(raw.get("recruitment_type") or "").strip() == "实习" else "unknown",
    )


def _city_matches(actual: str, wanted: str) -> bool:
    actual = actual.strip().removesuffix("市")
    wanted = wanted.strip().removesuffix("市")
    return bool(actual and wanted and (actual == wanted or actual.startswith(wanted + "-")))


class YingjieshengCollector:
    platform = "yingjiesheng"

    def __init__(
        self, *, browser: YingjieshengBrowser | None = None,
        uniform: Callable[[float, float], float] = random.SystemRandom().uniform,
        sleep: Callable[[float], None] = time.sleep,
        page_delay: tuple[float, float] = PAGE_DELAY,
        detail_delay: tuple[float, float] = DETAIL_DELAY,
        auto_verify_slider: bool = False,
    ):
        self.browser = browser or YingjieshengBrowser()
        self.uniform = uniform
        self.sleep = sleep
        self.page_delay = page_delay
        self.detail_delay = detail_delay
        self.auto_verify_slider = auto_verify_slider
        self._verification_attempted = False

    @staticmethod
    def _check_status(payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "selector_changed")
        messages = {
            "verification": ("verification_required", "应届生求职出现验证码或滑块验证，请人工处理后重新采集"),
            "login_required": ("login_required", "应届生求职登录已失效，请人工登录后重新采集"),
            "rate_limit": ("rate_limit", "应届生求职限制访问，已停止采集"),
            "selector_changed": ("selector_changed", "应届生求职页面结构变化或异常空结果，已停止采集"),
        }
        if status in messages:
            raise CollectionBlockedError(*messages[status])
        if status not in {"ready", "empty", "external"}:
            raise CollectionBlockedError("unexpected_response", "应届生求职返回异常响应，已停止采集")
        return status

    def _wait(self, hooks: CollectorHooks, delay: float) -> bool:
        if hooks.stop_event is not None:
            return hooks.stop_event.wait(delay)
        self.sleep(delay)
        return False

    def _read(self, tab: str, expression: str, hooks: CollectorHooks, *, page: int | None = None) -> dict[str, Any]:
        for attempt in range(8):
            result = _payload(self.browser.evaluate(tab, expression))
            if result.get("status") == "ready" and page is not None:
                actual = result.get("page_index")
                if actual is not None and actual != page:
                    result = {"status": "waiting"}
            if result.get("status") != "waiting":
                return result
            if attempt < 7 and self._wait(hooks, 1.0):
                return {"status": "stopped"}
        raise CollectionBlockedError("render_timeout", "应届生求职页面未完成加载，已停止采集")

    def _read_with_verification(
        self, tab: str, expression: str, hooks: CollectorHooks, *, page: int | None = None,
    ) -> dict[str, Any]:
        result = self._read(tab, expression, hooks, page=page)
        if result.get("status") != "verification" or not self.auto_verify_slider:
            return result
        if self._verification_attempted:
            raise CollectionBlockedError("verification_required", "应届生求职再次要求验证，已停止采集")
        if hooks.stop_event is not None and hooks.stop_event.is_set():
            return {"status": "stopped"}
        self._verification_attempted = True
        hooks.on_event(phase="verification", message="应届生求职滑块验证：仅尝试一次标准鼠标拖动")
        if not self.browser.solve_slider_once(tab):
            raise CollectionBlockedError("verification_failed", "应届生求职滑块验证未通过，请人工处理后重新采集")
        return self._read(tab, expression, hooks, page=page)

    def collect(self, request: PlatformCollectionRequest, hooks: CollectorHooks) -> PlatformCollectionResult:
        if request.max_pages > PAGE_LIMIT:
            raise CollectionBlockedError("page_limit", f"应届生求职最多采集 {PAGE_LIMIT} 页")
        seen: set[str] = set()
        for keyword in request.keywords:
            search_url = SEARCH_ROOT + quote(keyword, safe="")
            target = self.browser.new_tab("about:blank", background=True)
            if not target:
                raise CollectionBlockedError("browser_disconnected", "无法打开应届生求职搜索页")
            try:
                for page in range(1, request.max_pages + 1):
                    if hooks.stop_event is not None and hooks.stop_event.is_set():
                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                    if page > 1 and self._wait(hooks, self.uniform(*self.page_delay)):
                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                    if page == 1:
                        if not _safe_url(search_url, search=True) or not self.browser.navigate(target, search_url):
                            raise CollectionBlockedError("navigation_failed", "应届生求职搜索页无法安全打开")
                        self.browser.wait_for_load(target, timeout=15)
                    elif not self.browser.click(target, ".el-pagination .btn-next:not([disabled])"):
                        raise CollectionBlockedError("navigation_failed", "应届生求职无法安全翻页")
                    hooks.on_event(phase="loading_list", keyword=keyword, page=page, message="只读采集；城市按岗位实际地点过滤")
                    payload = self._read_with_verification(target, JS_LIST, hooks, page=page)
                    if payload.get("status") == "stopped":
                        return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                    status = self._check_status(payload)
                    if status == "empty":
                        break
                    jobs = payload.get("jobs")
                    if not isinstance(jobs, list) or not jobs:
                        raise CollectionBlockedError("unexpected_response", "应届生求职岗位列表响应异常，已停止采集")
                    parsed_any = False
                    for raw in jobs:
                        if hooks.stop_event is not None and hooks.stop_event.is_set():
                            return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                        candidate = candidate_from_list(raw, keyword)
                        if candidate is None:
                            hooks.on_parse_failed("应届生求职岗位缺少职位、公司或安全链接")
                            continue
                        parsed_any = True
                        if candidate.source_job_id in seen:
                            continue
                        seen.add(candidate.source_job_id)
                        if candidate.city and not any(_city_matches(candidate.city, city) for city in request.cities):
                            continue
                        if not hooks.on_list_candidate(candidate):
                            continue
                        # Only first-party detail pages are read. External application
                        # links are stored unchanged and never opened by the collector.
                        if urlparse(candidate.url).hostname == "q.yingjiesheng.com" and "/jobdetail/" in candidate.url:
                            if self._wait(hooks, self.uniform(*self.detail_delay)):
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                            detail_tab = self.browser.new_tab("about:blank", background=True)
                            if not detail_tab:
                                raise CollectionBlockedError("browser_disconnected", "无法打开应届生求职详情页")
                            try:
                                if not self.browser.navigate(detail_tab, candidate.url):
                                    raise CollectionBlockedError("navigation_failed", "应届生求职详情页无法安全打开")
                                self.browser.wait_for_load(detail_tab, timeout=15)
                                detail = self._read_with_verification(detail_tab, JS_DETAIL, hooks)
                            finally:
                                self.browser.close_tab(detail_tab)
                            if detail.get("status") == "stopped":
                                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
                            detail_status = self._check_status(detail)
                            if detail_status == "external":
                                destination = _safe_url(str(detail.get("url") or ""))
                                if not destination:
                                    raise CollectionBlockedError("unsafe_external_url", "站外岗位链接不安全，已停止采集")
                                candidate.url = destination
                            else:
                                candidate.jd = str(detail.get("jd") or "").strip()
                                candidate.city = candidate.city or str(detail.get("city") or "").strip()
                                candidate.education = candidate.education or str(detail.get("education") or "").strip()
                                candidate.experience = candidate.experience or str(detail.get("experience") or "").strip()
                        if not any(_city_matches(candidate.city, city) for city in request.cities):
                            continue
                        if not hooks.on_candidate(candidate):
                            return PlatformCollectionResult(self.platform, "completed", "callback_stopped", "采集回调已停止")
                    if not parsed_any:
                        raise CollectionBlockedError("selector_changed", "应届生求职岗位字段无法解析，已停止采集")
                    if not hooks.can_checkpoint():
                        return PlatformCollectionResult(self.platform, "failed", "save_failed", "岗位保存失败，已停止")
                    if not payload.get("has_next"):
                        break
            finally:
                self.browser.close_tab(target)
        return PlatformCollectionResult(self.platform, "completed", "search_exhausted", "应届生求职只读采集完成")
