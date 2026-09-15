"""GitHub 输出层与 webhook 触发层的集成测试（respx 拦截，离线可跑）。

断言的是设计决策本身，而不只是"请求发出去了"：
- `event` 必须是 `COMMENT`（绝不 `REQUEST_CHANGES`，不阻塞合并）；
- 行级评论必须带 `side: RIGHT`；
- 同一 head_sha 重复发布必须幂等跳过；
- Check Run 的 conclusion 映射必须符合 §4.9 的表。
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from acra.models import Finding
from acra.publish.github import GithubError, GithubPublisher, conclusion_for
from acra.publish.renderer import (
    BOT_MARKER,
    SummaryInputs,
    render_comment_body,
    render_summary,
    review_comments_payload,
    severity_exit_hit,
)

API = "https://api.github.test"


def _finding(**kw) -> Finding:
    payload = {
        "path": "src/main/java/OrderService.java",
        "line": 142,
        "category": "security",
        "severity": "high",
        "confidence": 0.82,
        "score": 0.92,
        "title": "字符串拼接构造 SQL，存在注入风险",
        "body": "现象：sql 由字符串拼接而成。触发条件：外部传入 orderId。影响：SQL 注入。修复建议：改用参数化查询。",
        "evidence": ["sql = \"select * from t where id = \" + orderId"],
    }
    payload.update(kw)
    return Finding(**payload)


# ---------------------------------------------------------------------------- 请求体


@respx.mock
async def test_create_review_payload_follows_design_decisions() -> None:
    respx.get(f"{API}/repos/o/r/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json=[])
    )
    route = respx.post(f"{API}/repos/o/r/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 999, "html_url": "https://x/999"})
    )

    finding = _finding()
    publisher = GithubPublisher("token", api_base=API)
    try:
        result = await publisher.create_review(
            "o",
            "r",
            42,
            head_sha="head123",
            body=render_summary([finding], SummaryInputs(files_analyzed=1, lines_changed=5)),
            comments=review_comments_payload([finding]),
        )
    finally:
        await publisher.aclose()

    assert result["review_id"] == 999
    assert result["comments"] == 1

    body = json.loads(route.calls[0].request.content)
    assert body["event"] == "COMMENT"          # 绝不 REQUEST_CHANGES
    assert body["commit_id"] == "head123"
    comment = body["comments"][0]
    assert comment["side"] == "RIGHT"
    assert comment["line"] == 142
    assert comment["path"] == "src/main/java/OrderService.java"
    assert "严重度：高" in comment["body"]
    assert BOT_MARKER in comment["body"]


@respx.mock
async def test_review_is_idempotent_per_head_sha() -> None:
    respx.get(f"{API}/repos/o/r/pulls/42/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "commit_id": "other", "body": "由 acra 生成"},
                {"id": 2, "commit_id": "head123", "body": f"摘要 {BOT_MARKER}"},
            ],
        )
    )
    post = respx.post(f"{API}/repos/o/r/pulls/42/reviews").mock(
        return_value=httpx.Response(200, json={"id": 3})
    )

    publisher = GithubPublisher("token", api_base=API)
    try:
        result = await publisher.create_review(
            "o", "r", 42, head_sha="head123", body="body", comments=[]
        )
    finally:
        await publisher.aclose()

    assert result["skipped"] is True
    assert result["reason"] == "already_reviewed"
    assert not post.called


@respx.mock
async def test_empty_comments_are_omitted_from_payload() -> None:
    respx.get(f"{API}/repos/o/r/pulls/1/reviews").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        return_value=httpx.Response(200, json={"id": 5})
    )
    publisher = GithubPublisher("token", api_base=API)
    try:
        await publisher.create_review("o", "r", 1, head_sha="h", body="没问题", comments=[])
    finally:
        await publisher.aclose()
    assert "comments" not in json.loads(route.calls[0].request.content)


@respx.mock
async def test_retry_on_5xx_then_success() -> None:
    respx.get(f"{API}/repos/o/r/pulls/1/reviews").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        side_effect=[
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"id": 7}),
        ]
    )
    publisher = GithubPublisher("token", api_base=API, max_retries=2)
    try:
        result = await publisher.create_review("o", "r", 1, head_sha="h", body="b", comments=[])
    finally:
        await publisher.aclose()
    assert result["review_id"] == 7
    assert route.call_count == 2


@respx.mock
async def test_403_rate_limit_raises() -> None:
    respx.get(f"{API}/repos/o/r/pulls/1/reviews").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        return_value=httpx.Response(403, text="API rate limit exceeded")
    )
    publisher = GithubPublisher("token", api_base=API)
    try:
        with pytest.raises(GithubError, match="限流"):
            await publisher.create_review("o", "r", 1, head_sha="h", body="b", comments=[])
    finally:
        await publisher.aclose()


@respx.mock
async def test_422_raises_so_caller_can_degrade() -> None:
    respx.get(f"{API}/repos/o/r/pulls/1/reviews").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/o/r/pulls/1/reviews").mock(
        return_value=httpx.Response(422, text="line must be part of the diff")
    )
    publisher = GithubPublisher("token", api_base=API)
    try:
        with pytest.raises(GithubError, match="422"):
            await publisher.create_review("o", "r", 1, head_sha="h", body="b", comments=[{}])
    finally:
        await publisher.aclose()


def test_missing_token_raises() -> None:
    with pytest.raises(GithubError):
        GithubPublisher("")


# ---------------------------------------------------------------------------- Check Run


@respx.mock
async def test_check_run_conclusion_and_payload() -> None:
    route = respx.post(f"{API}/repos/o/r/check-runs").mock(
        return_value=httpx.Response(201, json={"id": 88})
    )
    publisher = GithubPublisher("token", api_base=API)
    try:
        check = await publisher.create_check_run(
            "o", "r", head_sha="h", conclusion="neutral", summary="- a.java:1 问题"
        )
    finally:
        await publisher.aclose()
    assert check["id"] == 88
    sent = json.loads(route.calls[0].request.content)
    assert sent["status"] == "completed"
    assert sent["conclusion"] == "neutral"


@pytest.mark.parametrize(
    ("has_high", "failed", "expected"),
    [
        (False, False, "success"),
        (True, False, "neutral"),   # 有高优先级问题也不用 failure，不阻塞合并
        (False, True, "neutral"),
        (True, True, "neutral"),
    ],
)
def test_conclusion_mapping(has_high: bool, failed: bool, expected: str) -> None:
    assert conclusion_for(has_high, failed) == expected


# ---------------------------------------------------------------------------- 渲染


def test_summary_groups_by_severity_and_has_scope_block() -> None:
    findings = [
        _finding(severity="high", line=10, title="高危问题"),
        _finding(severity="low", line=20, title="低危问题", category="maintainability"),
    ]
    text = render_summary(
        findings,
        SummaryInputs(
            files_analyzed=6,
            lines_changed=214,
            context_level_max=2,
            static_tools=[],
            input_tokens=43200,
            output_tokens=3100,
            degrade_notes=["跳过 L3"],
        ),
    )
    assert "## 代码审查摘要" in text
    assert "共发现 2 个问题" in text
    assert "### 高优先级" in text
    assert "本次分析范围与降级说明" in text
    assert "L1 + L2" in text
    assert "跳过 L3" in text
    assert "/acra ignore" in text


def test_summary_for_no_findings() -> None:
    text = render_summary([], SummaryInputs(files_analyzed=1, lines_changed=3))
    assert "未发现值得修改的问题" in text


def test_comment_body_includes_suggestion_and_evidence() -> None:
    body = render_comment_body(_finding(suggestion="jdbc.query(sql, args)"))
    assert "**严重度：高**" in body
    assert "```suggestion" in body
    assert "<details><summary>依据</summary>" in body


@pytest.mark.parametrize(
    ("severity", "threshold", "hit"),
    [
        ("high", "high", True),
        ("blocker", "high", True),
        ("medium", "high", False),
        ("medium", "medium", True),
        ("low", "medium", False),
    ],
)
def test_fail_on_gate(severity: str, threshold: str, hit: bool) -> None:
    assert severity_exit_hit([_finding(severity=severity)], threshold) is hit


# ---------------------------------------------------------------------------- webhook


def test_verify_signature() -> None:
    from acra.trigger.github_webhook import verify_signature

    secret = "s3cr3t"
    body = b'{"action":"opened"}'
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_signature(body, f"sha256={digest}", secret) is True
    assert verify_signature(body, "sha256=deadbeef", secret) is False
    assert verify_signature(body, "", secret) is False
    assert verify_signature(body, f"sha256={digest}", "") is False
    assert verify_signature(body, digest, secret) is False  # 缺少前缀


def _pr_payload(**pr_overrides) -> dict:
    action = pr_overrides.pop("_action", "opened")
    pr = {
        "number": 42,
        "draft": False,
        "state": "open",
        "merged": False,
        "labels": [],
        "user": {"login": "yuki"},
        "head": {"sha": "headsha", "ref": "feature"},
        "base": {"sha": "basesha", "ref": "main"},
    }
    pr.update(pr_overrides)
    return {
        "action": action,
        "repository": {"full_name": "Akikakoi/stellar-mall", "clone_url": "https://x/y.git"},
        "installation": {"id": 12345},
        "pull_request": pr,
    }


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_normalize_triggers_on_reviewable_actions(action: str) -> None:
    from acra.trigger.normalize import normalize_github_event

    payload = _pr_payload()
    payload["action"] = action
    job, reason = normalize_github_event("pull_request", payload, "delivery-1")
    assert job is not None
    assert reason == f"queued:{action}"
    assert job.pr_number == 42
    assert job.head_sha == "headsha"
    assert job.base_ref == "main"
    assert job.trigger_source == "webhook"
    assert job.installation_id == 12345


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"draft": True}, "draft_pr"),
        ({"labels": [{"name": "skip acra"}]}, "skip_label"),
        ({"labels": [{"name": "ACRA-Disabled"}]}, "skip_label"),
        ({"user": {"login": "dependabot[bot]"}}, "bot_author"),
        ({"state": "closed"}, "pr_closed"),
    ],
)
def test_normalize_skips(override: dict, expected: str) -> None:
    from acra.trigger.normalize import normalize_github_event

    job, reason = normalize_github_event("pull_request", _pr_payload(**override), None)
    assert job is None
    assert reason == expected


def test_normalize_ignores_unsupported_event_and_action() -> None:
    from acra.trigger.normalize import normalize_github_event

    job, reason = normalize_github_event("push", {"repository": {}}, None)
    assert job is None and reason == "unsupported_event:push"

    payload = _pr_payload()
    payload["action"] = "labeled"
    job, reason = normalize_github_event("pull_request", payload, None)
    assert job is None and reason == "ignored_action:labeled"


def test_normalize_manual_command() -> None:
    from acra.trigger.normalize import normalize_github_event

    base = {"issue": {"number": 9, "pull_request": {"url": "x"}}, "repository": {"full_name": "o/r"}}
    job, reason = normalize_github_event(
        "issue_comment", {**base, "comment": {"body": "/acra review"}}, "d1"
    )
    assert job is not None
    assert reason == "manual_review"
    assert job.pr_number == 9
    assert job.trigger_source == "manual"

    job, reason = normalize_github_event(
        "issue_comment", {**base, "comment": {"body": "/acra ignore"}}, "d1"
    )
    assert job is None and reason == "manual_ignore"

    job, reason = normalize_github_event(
        "issue_comment", {**base, "comment": {"body": "看起来不错"}}, "d1"
    )
    assert job is None and reason == "comment_not_a_command"
