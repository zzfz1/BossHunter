"""Copyable messages use existing resume facts and never send to the site."""

from __future__ import annotations

import pytest

from bosshunter.ai import yingjiesheng_drafts as drafts


JOB = {
    "id": "yingjiesheng:1001", "source_platform": "yingjiesheng",
    "title": "AI 工程师", "company": "示例公司", "jd": "参与产品开发",
    "salary": "", "education": "", "recruitment_type": "campus", "score_reason": "",
}


def test_greeting_is_returned_without_platform_interaction(monkeypatch):
    monkeypatch.setattr(drafts.greeter, "_get_resume_summary", lambda config: "已验证的简历事实")
    monkeypatch.setattr(drafts.greeter, "_generate_with_token_retry", lambda *args, **kwargs: "您好，我对该岗位感兴趣。")
    text = drafts.generate_copyable_draft(JOB, {"profile": {"ai_greeting_enabled": True}}, kind="greeting")
    assert text == "您好，我对该岗位感兴趣。"


def test_follow_up_needs_user_context_and_uses_resume(monkeypatch):
    monkeypatch.setattr(drafts.greeter, "_get_resume_summary", lambda config: "已验证的简历事实")
    prompts = []

    def fake_model(prompt, config, max_tokens):
        prompts.append(prompt)
        return "感谢回复，我想进一步了解岗位工作内容。"

    monkeypatch.setattr(drafts.greeter, "_call_claude", fake_model)
    with pytest.raises(drafts.DraftError) as exc:
        drafts.generate_copyable_draft(JOB, {}, kind="follow_up")
    assert exc.value.code == "context_required"
    text = drafts.generate_copyable_draft(JOB, {}, kind="follow_up", context="对方询问是否有相关经验")
    assert text.startswith("感谢回复")
    assert "已验证的简历事实" in prompts[0]
    assert "对方询问是否有相关经验" in prompts[0]


def test_missing_resume_or_untrusted_url_blocks_output(monkeypatch):
    monkeypatch.setattr(drafts.greeter, "_get_resume_summary", lambda config: "")
    with pytest.raises(drafts.DraftError) as exc:
        drafts.generate_copyable_draft(JOB, {}, kind="greeting")
    assert exc.value.code == "resume_missing"
    monkeypatch.setattr(drafts.greeter, "_get_resume_summary", lambda config: "简历事实")
    monkeypatch.setattr(drafts.greeter, "_generate_with_token_retry", lambda *args, **kwargs: "请访问 https://other.example.org")
    with pytest.raises(drafts.DraftError) as exc:
        drafts.generate_copyable_draft(JOB, {"profile": {}}, kind="greeting")
    assert exc.value.code == "untrusted_url"


def test_other_platform_rejected_before_reading_resume(monkeypatch):
    monkeypatch.setattr(drafts.greeter, "_get_resume_summary", lambda config: pytest.fail("must not read resume"))
    with pytest.raises(drafts.DraftError) as exc:
        drafts.generate_copyable_draft(JOB | {"source_platform": "boss"}, {}, kind="greeting")
    assert exc.value.code == "wrong_platform"
