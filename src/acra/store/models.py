"""SQLAlchemy 模型，逐表对应开发文档 §5.2。

字段名与文档表结构保持一致，便于对照运维；方言差异用 `with_variant` 抹平：

- `TEXT[]`（PostgreSQL）↔ `JSON`（SQLite）
- `JSONB`（PostgreSQL）↔ `JSON`（SQLite）
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

JSONType = JSON().with_variant(JSONB(), "postgresql")
StrArrayType = JSON().with_variant(ARRAY(Text()), "postgresql")


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repository"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="main")
    installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("uq_repository_platform_external", "platform", "external_id", unique=True),)


class ReviewRun(Base):
    __tablename__ = "review_run"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repository.id"), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    merge_base_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="cli")
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="full")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    context_level_max: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    files_analyzed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_raw: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings_kept: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded_notes: Mapped[list | None] = mapped_column(StrArrayType, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_review_run_repo_pr", "repository_id", "pr_number", "created_at"),
        Index("idx_review_run_status", "status"),
    )


class FindingRow(Base):
    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    review_run_id: Mapped[int] = mapped_column(
        ForeignKey("review_run.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False, default="RIGHT")
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_rule_ids: Mapped[list | None] = mapped_column(StrArrayType, nullable=True)
    verify_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verify_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_false_positive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_finding_run", "review_run_id"),
        Index("idx_finding_file", "path", "line"),
    )


class ToolCall(Base):
    __tablename__ = "tool_call"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    review_run_id: Mapped[int] = mapped_column(
        ForeignKey("review_run.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoConfig(Base):
    __tablename__ = "repo_config"

    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repository.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_comments: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    #: 与 `Settings.acra_confidence_threshold` 必须一致，否则 DB 默认值会静默盖过全局设置
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.50)
    context_level_max: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    enabled_linters: Mapped[list | None] = mapped_column(StrArrayType, nullable=True)
    ignored_paths: Mapped[list | None] = mapped_column(StrArrayType, nullable=True)
    ignored_rules: Mapped[list | None] = mapped_column(StrArrayType, nullable=True)
    custom_conventions: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_run_test: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_budget_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=5_000_000)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalCase(Base):
    __tablename__ = "eval_case"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repository.id"), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    expected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    human_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="curated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EvalResult(Base):
    __tablename__ = "eval_result"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    eval_case_id: Mapped[int] = mapped_column(
        ForeignKey("eval_case.id", ondelete="CASCADE"), nullable=False
    )
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched_finding_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
