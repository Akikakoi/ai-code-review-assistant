"""持久化层的集成测试（SQLite，离线可跑）。"""

from __future__ import annotations

import pytest

from acra.models import Finding
from acra.settings import Settings
from acra.store.db import Database
from acra.store.repository import (
    daily_cost_micros,
    finish_run,
    get_or_create_repository,
    last_merge_base,
    last_reviewed_sha,
    load_repo_config,
    metrics_summary,
    save_findings,
    save_repo_config,
    set_feedback,
    start_run,
)


def _db(tmp_path) -> Database:
    db = Database("", tmp_path / "work")
    db.create_all()
    return db


def test_sqlite_fallback_creates_all_tables(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        assert db.dialect == "sqlite"
        from sqlalchemy import inspect

        tables = set(inspect(db.engine).get_table_names())
        assert {
            "repository",
            "review_run",
            "finding",
            "tool_call",
            "repo_config",
            "eval_case",
            "eval_result",
        } <= tables
    finally:
        db.dispose()


def test_get_or_create_repository_is_idempotent(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        with db.session() as session:
            a = get_or_create_repository(session, full_name="owner/repo", platform="local")
            b = get_or_create_repository(session, full_name="owner/repo", platform="local")
            assert a.id == b.id
    finally:
        db.dispose()


def test_run_lifecycle_and_incremental_lookup(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="owner/repo")
            run = start_run(
                session,
                job_id="job-1",
                repository_id=repo.id,
                pr_number=42,
                base_sha="base",
                head_sha="head-1",
                merge_base_sha="mb-1",
                trigger_source="cli",
                mode="full",
            )
            save_findings(
                session,
                run.id,
                [
                    Finding(
                        path="src/A.java",
                        line=11,
                        category="bug",
                        severity="high",
                        confidence=0.8,
                        score=0.9,
                        title="空值校验被取反",
                        body="现象与影响",
                    )
                ],
            )
            finish_run(
                session,
                run,
                status="succeeded",
                files_analyzed=1,
                lines_changed=1,
                findings_raw=2,
                findings_kept=1,
                input_tokens=100,
                output_tokens=20,
                cost_micros=7,
                duration_ms=1234,
            )

        with db.session() as session:
            assert last_reviewed_sha(session, repo.id, 42) == "head-1"
            assert last_merge_base(session, repo.id, 42) == "mb-1"
            assert last_reviewed_sha(session, repo.id, 999) is None
            assert daily_cost_micros(session) == 7

            summary = metrics_summary(session, days=30)
            assert summary["runs"] == 1
            assert summary["failure_rate"] == 0.0
            assert summary["findings_kept"] == 1
            assert summary["avg_kept_per_run"] == 1.0
            # Verify 关闭时 raw=2 kept=1 → 0.5
            assert summary["verify_rejection_rate"] == 0.5
            assert summary["cost_micros"] == 7
    finally:
        db.dispose()


def test_repo_config_defaults_and_partial_update(tmp_path) -> None:
    db = _db(tmp_path)
    # 断言"与全局设置一致"，而不是硬编码字面值 —— 置信度门槛由评估集扫出来，
    # 会随数据更新（0.65 → 0.50 就是一次）。硬编码会让每次调参都要改测试，
    # 而且掩盖了真正要守的东西：仓库配置的默认值不能和全局设置脱节。
    expected_threshold = Settings(_env_file=None).acra_confidence_threshold
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="owner/repo")
            default = load_repo_config(session, repo.id)
            assert default.max_comments == 5
            assert default.confidence_threshold == pytest.approx(expected_threshold)

            save_repo_config(
                session,
                repo.id,
                {"max_comments": 3, "custom_conventions": "禁止在 Controller 里写业务逻辑"},
            )

        with db.session() as session:
            updated = load_repo_config(session, repo.id)
            assert updated.max_comments == 3
            # 未改的字段保持默认
            assert updated.confidence_threshold == pytest.approx(expected_threshold)
            assert "Controller" in updated.custom_conventions
    finally:
        db.dispose()


def test_feedback_round_trip(tmp_path) -> None:
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="owner/repo")
            run = start_run(
                session,
                job_id="job-fb",
                repository_id=repo.id,
                pr_number=1,
                base_sha="b",
                head_sha="h",
                merge_base_sha="m",
                trigger_source="cli",
                mode="full",
            )
            rows = save_findings(
                session,
                run.id,
                [
                    Finding(
                        path="src/A.java",
                        line=5,
                        category="style",
                        severity="nit",
                        confidence=0.4,
                        score=0.4,
                        title="t",
                        body="b",
                    )
                ],
            )
            assert set_feedback(session, rows[0].id, is_false_positive=True) is True
            assert set_feedback(session, 99999, is_false_positive=True) is False

        with db.session() as session:
            assert metrics_summary(session)["false_positive_rate"] == 1.0
    finally:
        db.dispose()


def test_postgres_url_normalization() -> None:
    from acra.store.db import normalize_url

    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"
