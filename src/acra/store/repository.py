"""数据访问。

对应开发文档 §5.2 的表与 §5.1 的实体关系。所有 SQL 都集中在这里，
上层（pipeline / CLI / API）只调用这些函数，不直接写查询。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from acra.models import Finding
from acra.store.models import FindingRow, RepoConfig, Repository, ReviewRun, ToolCall


@dataclass(slots=True)
class RepoConfigView:
    """仓库配置的只读视图，作为 `repo_config` 表缺席时的默认值来源。"""

    enabled: bool = True
    max_comments: int = 5
    #: 每个仓库可覆盖的置信度门槛。默认值必须与 `Settings.acra_confidence_threshold` 一致
    #: —— 三处（设置 / 本视图 / DB 列）不一致时，DB 那条会静默盖过全局设置，
    #: 改了全局默认却"看起来没生效"。`tests/unit/test_settings_defaults.py` 会守住这一点。
    confidence_threshold: float = 0.50
    context_level_max: int = 2
    #: 留空表示"按语言自动选工具"（推荐）。只有需要刻意收窄时才写具体工具名 ——
    #: 写死名单的代价是新增语言/新增工具时配置不会自动跟上，反而悄悄少跑检查。
    enabled_linters: list[str] = field(default_factory=list)
    ignored_paths: list[str] = field(default_factory=lambda: ["**/*.md", "**/dist/**"])
    ignored_rules: list[str] = field(default_factory=list)
    custom_conventions: str = ""
    allow_run_test: bool = False
    daily_budget_micros: int = 5_000_000


def get_or_create_repository(
    session: Session,
    *,
    platform: str = "local",
    full_name: str,
    external_id: int | None = None,
    installation_id: int | None = None,
) -> Repository:
    # 本地模式没有平台侧 ID，用 full_name 的稳定哈希兜底，保证同一仓库可复用。
    # 必须在查询之前算出来：否则查询用 0、写入用哈希，第二次调用必然撞唯一约束。
    if external_id is None:
        external_id = abs(hash(full_name)) % (10**12)

    stmt = select(Repository).where(
        Repository.platform == platform, Repository.external_id == external_id
    )
    repo = session.scalar(stmt)
    if repo is not None:
        if installation_id is not None and repo.installation_id != installation_id:
            repo.installation_id = installation_id
        return repo

    repo = Repository(
        platform=platform,
        external_id=external_id,
        full_name=full_name,
        installation_id=installation_id,
    )
    session.add(repo)
    session.flush()
    return repo


def load_repo_config(session: Session, repository_id: int) -> RepoConfigView:
    cfg = session.get(RepoConfig, repository_id)
    if cfg is None:
        return RepoConfigView()
    view = RepoConfigView(
        enabled=cfg.enabled,
        max_comments=cfg.max_comments,
        confidence_threshold=cfg.confidence_threshold,
        context_level_max=cfg.context_level_max,
        enabled_linters=list(cfg.enabled_linters or []),
        ignored_paths=list(cfg.ignored_paths or []),
        ignored_rules=list(cfg.ignored_rules or []),
        custom_conventions=cfg.custom_conventions or "",
        allow_run_test=cfg.allow_run_test,
        daily_budget_micros=cfg.daily_budget_micros,
    )
    return view


def save_repo_config(session: Session, repository_id: int, values: dict) -> RepoConfig:
    """部分更新仓库配置。`custom_conventions` 只能由管理员经鉴权接口写入（文档 §12.3）。"""
    cfg = session.get(RepoConfig, repository_id)
    if cfg is None:
        cfg = RepoConfig(repository_id=repository_id)
        session.add(cfg)
    for key, value in values.items():
        if hasattr(cfg, key) and key != "repository_id":
            setattr(cfg, key, value)
    cfg.updated_at = datetime.now(UTC)
    session.flush()
    return cfg


# ---------------------------------------------------------------------------- 运行记录


def start_run(
    session: Session,
    *,
    job_id: str,
    repository_id: int,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
    trigger_source: str,
    mode: str,
) -> ReviewRun:
    run = ReviewRun(
        job_id=job_id,
        repository_id=repository_id,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        trigger_source=trigger_source,
        mode=mode,
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    return run


def finish_run(
    session: Session,
    run: ReviewRun,
    *,
    status: str,
    context_level_max: int = 2,
    files_analyzed: int = 0,
    lines_changed: int = 0,
    findings_raw: int = 0,
    findings_kept: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    duration_ms: int | None = None,
    error_message: str | None = None,
    degrade_notes: list[str] | None = None,
) -> ReviewRun:
    run.status = status
    run.context_level_max = context_level_max
    run.files_analyzed = files_analyzed
    run.lines_changed = lines_changed
    run.findings_raw = findings_raw
    run.findings_kept = findings_kept
    run.input_tokens = input_tokens
    run.output_tokens = output_tokens
    run.cost_micros = cost_micros
    run.duration_ms = duration_ms
    run.error_message = error_message
    run.degraded_notes = list(degrade_notes or [])
    run.finished_at = datetime.now(UTC)
    session.flush()
    return run


def save_findings(session: Session, run_id: int, findings: list[Finding]) -> list[FindingRow]:
    rows: list[FindingRow] = []
    for f in findings:
        row = FindingRow(
            review_run_id=run_id,
            path=f.path,
            line=f.line,
            end_line=f.end_line,
            category=f.category,
            severity=f.severity,
            confidence=f.confidence,
            score=f.score,
            title=f.title,
            body=f.body,
            suggestion=f.suggestion,
            evidence_rule_ids=list(f.evidence_rule_ids),
            verify_verdict=f.verify_verdict,
            verify_reason=f.verify_reason,
            published=f.published,
            published_comment_id=f.published_comment_id,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def mark_published(session: Session, finding_ids: list[int]) -> None:
    for fid in finding_ids:
        row = session.get(FindingRow, fid)
        if row is not None:
            row.published = True


def record_tool_call(
    session: Session,
    run_id: int,
    *,
    tool_name: str,
    arguments: dict,
    result_summary: str = "",
    duration_ms: int = 0,
    success: bool = True,
) -> ToolCall:
    row = ToolCall(
        review_run_id=run_id,
        tool_name=tool_name,
        arguments=arguments,
        result_summary=result_summary[:4000],
        duration_ms=duration_ms,
        success=success,
    )
    session.add(row)
    session.flush()
    return row


def last_reviewed_sha(session: Session, repository_id: int, pr_number: int) -> str | None:
    """增量审查依据：该 PR 上一次成功审查的 head_sha（文档 §4.2）。"""
    stmt = (
        select(ReviewRun.head_sha)
        .where(
            ReviewRun.repository_id == repository_id,
            ReviewRun.pr_number == pr_number,
            ReviewRun.merge_base_sha.is_not(None),
        )
        .order_by(ReviewRun.created_at.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def last_merge_base(session: Session, repository_id: int, pr_number: int) -> str | None:
    stmt = (
        select(ReviewRun.merge_base_sha)
        .where(ReviewRun.repository_id == repository_id, ReviewRun.pr_number == pr_number)
        .order_by(ReviewRun.created_at.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def daily_cost_micros(session: Session) -> int:
    """当日累计成本，用于日预算熔断（文档 §4.2）。"""
    since = datetime.now(UTC) - timedelta(days=1)
    stmt = select(func.coalesce(func.sum(ReviewRun.cost_micros), 0)).where(ReviewRun.created_at >= since)
    return int(session.scalar(stmt) or 0)


def set_feedback(session: Session, finding_id: int, *, is_false_positive: bool) -> bool:
    """人工反馈回流（文档 §11.1 C：回归集；§9.4：pattern 黑名单）。"""
    row = session.get(FindingRow, finding_id)
    if row is None:
        return False
    row.is_false_positive = is_false_positive
    session.flush()
    return True


def metrics_summary(session: Session, days: int = 30) -> dict:
    """质量与成本聚合指标（文档 §6.1 /metrics/summary）。"""
    since = datetime.now(UTC) - timedelta(days=days)
    base = select(
        func.count(ReviewRun.id),
        func.coalesce(func.sum(ReviewRun.findings_kept), 0),
        func.coalesce(func.sum(ReviewRun.findings_raw), 0),
        func.coalesce(func.sum(ReviewRun.input_tokens), 0),
        func.coalesce(func.sum(ReviewRun.output_tokens), 0),
        func.coalesce(func.sum(ReviewRun.cost_micros), 0),
    ).where(ReviewRun.created_at >= since)
    runs, kept, raw, in_tokens, out_tokens, cost = session.execute(base).one()

    failed = session.scalar(
        select(func.count(ReviewRun.id)).where(
            ReviewRun.created_at >= since, ReviewRun.status == "failed"
        )
    )
    degraded = session.scalar(
        select(func.count(ReviewRun.id)).where(
            ReviewRun.created_at >= since, ReviewRun.status == "degraded"
        )
    )
    false_positives = session.scalar(
        select(func.count(FindingRow.id)).where(FindingRow.is_false_positive.is_(True))
    )
    total_findings = session.scalar(select(func.count(FindingRow.id)))

    runs = int(runs or 0)
    return {
        "window_days": days,
        "runs": runs,
        "failed_runs": int(failed or 0),
        "degraded_runs": int(degraded or 0),
        "failure_rate": (int(failed or 0) / runs) if runs else 0.0,
        "findings_raw": int(raw),
        "findings_kept": int(kept),
        "avg_kept_per_run": (int(kept) / runs) if runs else 0.0,
        "verify_rejection_rate": (1 - int(kept) / int(raw)) if int(raw) else 0.0,
        "input_tokens": int(in_tokens),
        "output_tokens": int(out_tokens),
        "cost_micros": int(cost),
        "false_positive_rate": (int(false_positives or 0) / int(total_findings or 1)),
    }
