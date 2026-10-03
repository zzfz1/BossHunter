import json
import tempfile
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from bosshunter.collection.base import CollectionBlockedError, CollectorHooks
from bosshunter.collection.models import PlatformCollectionRequest
from bosshunter.collection.orchestrator import CollectionOrchestrator, normalize_collection_options, validate_collection_options
from bosshunter.collection.registry import CollectorRegistry
from bosshunter.collection.platforms.yingjiesheng import (
    YingjieshengCollector, candidate_from_list,
)
from bosshunter.db import get_db


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeBrowser:
    def __init__(self, lists, detail=None, solve_result=True):
        self.lists = iter(lists)
        self.detail = detail or fixture("yingjiesheng_detail.json")
        self.urls = []
        self.clicks = []
        self.closed = []
        self.next_tab = 0
        self.solve_result = solve_result
        self.solve_calls = []

    def new_tab(self, _url, background=True):
        self.next_tab += 1
        return str(self.next_tab)

    def close_tab(self, tab):
        self.closed.append(tab)
        return True

    def navigate(self, tab, url):
        self.urls.append((tab, url))
        return True

    def click(self, tab, selector):
        self.clicks.append((tab, selector))
        return True

    def wait_for_load(self, tab, timeout=15):
        return True

    def solve_slider_once(self, tab):
        self.solve_calls.append(tab)
        return self.solve_result

    def evaluate(self, tab, expression):
        if "search-list-item-wrapper" in expression:
            return next(self.lists)
        return self.detail


def request(**overrides):
    values = dict(platform="yingjiesheng", keywords=["AI"], cities=["上海"], city_codes={}, max_pages=2)
    values.update(overrides)
    return PlatformCollectionRequest(**values)


def hooks(saved, *, stop=None, existing=None):
    existing = existing or set()
    return CollectorHooks(
        stop_event=stop,
        on_list_candidate=lambda job: job.source_job_id not in existing,
        on_candidate=lambda job: saved.append(job) or True,
        on_parse_failed=lambda reason: None,
        on_event=lambda **kwargs: None,
    )


def collector(browser, *, auto_verify_slider=False):
    return YingjieshengCollector(
        browser=browser, uniform=lambda low, high: 0, sleep=lambda _: None,
        auto_verify_slider=auto_verify_slider,
    )


def test_list_detail_external_pagination_and_duplicate_filter():
    first = fixture("yingjiesheng_list.json")
    second = {"status": "ready", "jobs": [first["jobs"][0]], "page_index": 2, "has_next": False}
    browser = FakeBrowser([first, second])
    saved = []
    result = collector(browser).collect(request(), hooks(saved))
    assert result.status == "completed"
    assert [job.storage_id for job in saved] == [
        "yingjiesheng:1001", saved[1].storage_id,
    ]
    assert saved[0].jd == "参与智能体产品开发与测试。"
    assert saved[0].education == "本科"
    assert saved[1].source_job_id.startswith("external-")
    assert saved[1].url == "https://careers.example.org/jobs/abc"
    assert not any("careers.example.org" in url for _, url in browser.urls)
    assert len([url for _, url in browser.urls if "/jobs/search/" in url]) == 1
    assert browser.clicks == [("1", ".el-pagination .btn-next:not([disabled])")]


def test_missing_fields_do_not_get_invented_and_unsafe_url_is_rejected():
    raw = {"title": "岗位", "company": "示例", "city": "上海", "url": "https://q.yingjiesheng.com/jobdetail/99.html"}
    candidate = candidate_from_list(raw, "AI")
    assert candidate.salary == candidate.education == candidate.experience == candidate.jd == ""
    assert candidate_from_list({**raw, "url": "javascript:alert(1)"}, "AI") is None
    assert candidate_from_list({**raw, "company": ""}, "AI") is None
    assert candidate_from_list({**raw, "url": "https://careers.example.org/a", "source_job_id": "ext-42"}, "AI").source_job_id == "ext-42"
    assert candidate_from_list({**raw, "recruitment_type": "实习"}, "AI").recruitment_type == "campus"
    browser = FakeBrowser([{"status": "ready", "jobs": [{**raw, "company": ""}], "next_url": ""}])
    with pytest.raises(CollectionBlockedError) as captured:
        collector(browser).collect(request(), hooks([]))
    assert captured.value.code == "selector_changed"


@pytest.mark.parametrize("status,code", [
    ("verification", "verification_required"),
    ("login_required", "login_required"),
    ("rate_limit", "rate_limit"),
    ("selector_changed", "selector_changed"),
    ("unexpected", "unexpected_response"),
])
def test_blocks_without_retry(status, code):
    browser = FakeBrowser([{"status": status}])
    with pytest.raises(CollectionBlockedError) as captured:
        collector(browser).collect(request(), hooks([]))
    assert captured.value.code == code
    assert len(browser.urls) == 1
    assert browser.closed == ["1"]


def test_opted_in_slider_attempts_once_and_resumes_read_only_collection():
    ready = {"status": "ready", "jobs": [fixture("yingjiesheng_list.json")["jobs"][1]],
             "page_index": 1, "has_next": False}
    browser = FakeBrowser([{"status": "verification"}, ready])
    saved = []
    result = collector(browser, auto_verify_slider=True).collect(request(max_pages=1), hooks(saved))
    assert result.status == "completed"
    assert browser.solve_calls == ["1"]
    assert len(saved) == 1
    assert browser.closed == ["1"]


def test_slider_failure_and_repeated_challenge_fail_closed():
    failed = FakeBrowser([{"status": "verification"}], solve_result=False)
    with pytest.raises(CollectionBlockedError) as captured:
        collector(failed, auto_verify_slider=True).collect(request(max_pages=1), hooks([]))
    assert captured.value.code == "verification_failed"
    assert failed.solve_calls == ["1"]
    assert failed.closed == ["1"]

    first_party = fixture("yingjiesheng_list.json")["jobs"][0]
    ready = {"status": "ready", "jobs": [first_party], "page_index": 1, "has_next": False}
    repeated = FakeBrowser([{"status": "verification"}, ready], detail={"status": "verification"})
    with pytest.raises(CollectionBlockedError) as captured:
        collector(repeated, auto_verify_slider=True).collect(request(max_pages=1), hooks([]))
    assert captured.value.code == "verification_required"
    assert repeated.solve_calls == ["1"]


def test_malformed_response_and_stop_event():
    browser = FakeBrowser(["not-json"])
    with pytest.raises(CollectionBlockedError) as captured:
        collector(browser).collect(request(), hooks([]))
    assert captured.value.code == "selector_changed"
    stopped = Event()
    stopped.set()
    browser = FakeBrowser([])
    result = collector(browser).collect(request(), hooks([], stop=stopped))
    assert result.reason_code == "user_stopped"
    assert browser.urls == []


def test_waits_for_dynamic_list_once_then_fails_closed_on_timeout():
    ready = {"status": "ready", "jobs": [fixture("yingjiesheng_list.json")["jobs"][1]], "next_url": ""}
    saved = []
    result = collector(FakeBrowser([{"status": "waiting"}, ready])).collect(request(), hooks(saved))
    assert result.status == "completed"
    assert len(saved) == 1
    with pytest.raises(CollectionBlockedError) as captured:
        collector(FakeBrowser([{"status": "waiting"}] * 8)).collect(request(), hooks([]))
    assert captured.value.code == "render_timeout"


def test_pagination_waits_for_new_page_before_processing():
    first = fixture("yingjiesheng_list.json")
    second = {"status": "ready", "jobs": [first["jobs"][0]], "page_index": 2, "has_next": False}
    browser = FakeBrowser([first, first, second])
    saved = []
    result = collector(browser).collect(request(), hooks(saved))
    assert result.status == "completed"
    assert len(saved) == 2
    assert len(browser.clicks) == 1


def test_pagination_stops_when_site_does_not_advance():
    first = fixture("yingjiesheng_list.json")
    browser = FakeBrowser([first] * 9)
    with pytest.raises(CollectionBlockedError) as captured:
        collector(browser).collect(request(), hooks([]))
    assert captured.value.code == "render_timeout"
    assert len(browser.clicks) == 1


def test_config_has_no_foreign_city_code_and_caps_pages():
    options = normalize_collection_options(
        {}, {"platform_order": ["yingjiesheng"], "platforms": {"yingjiesheng": {
            "keywords": ["AI"], "cities": ["上海"], "city_codes": {"上海": "020000"}, "max_pages": 2,
        }}},
    )
    assert options["platforms"]["yingjiesheng"]["city_codes"] == {}
    options["platforms"]["yingjiesheng"]["max_pages"] = 4
    with pytest.raises(ValueError, match="最大页数"):
        validate_collection_options(options)


def test_orchestrator_persists_platform_identity_and_queues_scoring():
    page = fixture("yingjiesheng_list.json")
    page["has_next"] = False
    browser = FakeBrowser([page])
    registry = CollectorRegistry({"yingjiesheng": lambda: collector(browser)})
    options = {
        "platform_order": ["yingjiesheng"], "auto_score": True,
        "platforms": {"yingjiesheng": {
            "keywords": ["AI"], "cities": ["上海"], "city_codes": {},
            "max_pages": 1, "sort": "default",
        }},
    }
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "jobs.db"
        with patch("bosshunter.ai.scorer.score_jobs") as score_jobs:
            result = CollectionOrchestrator({}, db_path=path, registry=registry).run(options)
        db = get_db(path)
        rows = db.execute("SELECT id, source_platform, source_job_id FROM jobs ORDER BY id").fetchall()
        db.close()
    assert result["platforms"]["yingjiesheng"]["new"] == 2
    assert all(row["source_platform"] == "yingjiesheng" for row in rows)
    assert rows[0]["id"].startswith("yingjiesheng:")
    assert score_jobs.call_args.kwargs["scope"] == "selected"
    assert set(score_jobs.call_args.kwargs["job_ids"]) == set(result["collected_job_ids"])
