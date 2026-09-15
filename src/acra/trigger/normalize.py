"""平台事件 → ReviewJob。

对应开发文档 §4.1。

处理函数只做三件事：验签 → 规范化 → 入队。**绝不在 webhook 请求内做克隆或 LLM 调用**，
否则会因平台 10 秒超时被重投，造成重复审查。

跳过条件集中配置在这里（文档 §4.1）：draft PR、`[skip acra]` / `acra-disabled` 标签、
机器人自己的提交、纯文档变更。纯文档变更在 pipeline 里按 `risk_rules.is_low_value_path`
过滤 —— 因为 webhook payload 不含文件清单，只有 `changed_files` 计数。
"""

from __future__ import annotations

import uuid
from typing import Any

from acra.models import ReviewJob

#: 支持的事件
SUPPORTED_EVENTS = {
    "pull_request",
    "issue_comment",
}

#: 触发审查的 PR action
TRIGGER_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

#: 跳过该 PR 的标签
SKIP_LABELS = {"skip acra", "[skip acra]", "acra-disabled", "no-acra"}

#: 机器人作者的登录名后缀（避免自触发）
BOT_LOGIN_SUFFIXES = ("[bot]", "-bot", "_bot", "acra")

MANUAL_TRIGGER = "/acra review"
IGNORE_TRIGGER = "/acra ignore"


def should_skip(payload: dict[str, Any]) -> str | None:
    """返回跳过原因；不跳过返回 None。"""
    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return "draft_pr"

    labels = {str(label.get("name", "")).strip().lower() for label in pr.get("labels") or []}
    if labels & SKIP_LABELS:
        return "skip_label"

    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author and any(author.endswith(suffix) for suffix in BOT_LOGIN_SUFFIXES):
        return "bot_author"

    if pr.get("merged") or pr.get("state") == "closed":
        return "pr_closed"

    if not pr.get("head") or not pr.get("base"):
        return "incomplete_payload"
    return None


def _extract_manual_command(payload: dict[str, Any]) -> str | None:
    comment = payload.get("comment") or {}
    body = str(comment.get("body") or "").strip().lower()
    if not body:
        return None
    if body.startswith(IGNORE_TRIGGER):
        return "ignore"
    if body.startswith(MANUAL_TRIGGER):
        return "review"
    return None


def normalize_github_event(
    event: str,
    payload: dict[str, Any],
    delivery_id: str | None = None,
) -> tuple[ReviewJob | None, str]:
    """规范化 GitHub 事件。返回 (job, 说明)；job 为 None 表示忽略。"""
    if event not in SUPPORTED_EVENTS:
        return None, f"unsupported_event:{event}"

    if event == "issue_comment":
        if "pull_request" not in (payload.get("issue") or {}):
            return None, "comment_not_on_pr"
        command = _extract_manual_command(payload)
        if command == "ignore":
            return None, "manual_ignore"
        if command != "review":
            return None, "comment_not_a_command"
        # issue_comment 的 pull_request 字段只有 url，需要靠 PR 号拉取详情；
        # 阶段一（不接 GitHub）不实现该补全，交由调用方补齐后重投。
        issue = payload.get("issue") or {}
        repo = payload.get("repository") or {}
        return (
            ReviewJob(
                job_id=str(uuid.uuid4()),
                platform="github",
                repo_full_name=repo.get("full_name") or "",
                repo_path=repo.get("clone_url") or "",
                remote_url=repo.get("clone_url"),
                pr_number=int(issue.get("number") or 0),
                trigger_source="manual",
                delivery_id=delivery_id,
                installation_id=(payload.get("installation") or {}).get("id"),
            ),
            "manual_review",
        )

    reason = should_skip(payload)
    if reason:
        return None, reason

    pr = payload["pull_request"]
    action = payload.get("action")
    if action not in TRIGGER_ACTIONS:
        return None, f"ignored_action:{action}"

    repo = payload.get("repository") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}

    return (
        ReviewJob(
            job_id=str(uuid.uuid4()),
            platform="github",
            repo_full_name=repo.get("full_name") or "",
            repo_path=repo.get("clone_url") or "",
            remote_url=repo.get("clone_url") or repo.get("html_url"),
            pr_number=int(pr.get("number") or 0),
            base_ref=base.get("ref"),
            head_ref=head.get("ref"),
            base_sha=(base.get("sha") or None),
            head_sha=(head.get("sha") or None),
            trigger_source="webhook",
            delivery_id=delivery_id,
            installation_id=(payload.get("installation") or {}).get("id"),
        ),
        f"queued:{action}",
    )


def job_from_cli(
    *,
    repo_path: str,
    base_ref: str | None = None,
    head_ref: str | None = None,
    pr_number: int | None = None,
    repo_full_name: str = "local/repo",
    force_full: bool = False,
) -> ReviewJob:
    """CLI 本地模式的任务（不依赖平台）。"""
    return ReviewJob(
        job_id=str(uuid.uuid4()),
        platform="local",
        repo_full_name=repo_full_name,
        repo_path=repo_path,
        pr_number=pr_number,
        base_ref=base_ref,
        head_ref=head_ref,
        trigger_source="cli",
        force_full=force_full,
    )
