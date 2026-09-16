"""持久化层的集成测试（SQLite，离线可跑）。"""

from __future__ import annotations

import os
import subprocess
import sys

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
    mark_published,
    metrics_summary,
    prior_comments_by_path,
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


def test_external_id_is_stable_across_processes(tmp_path) -> None:
    """本地仓库 ID 必须**跨进程**稳定：每跑一次审查就是一个新进程。

    用真实子进程 + 不同 PYTHONHASHSEED 验证。必须这样测：
    内置 `hash()` 的随机化只在跨进程时才暴露，同进程内断言会全部通过 ——
    这正是这个 bug 能长期存活的原因（repository_id 每次都变 ⇒ 增量审查静默退化成全量）。
    """
    from acra.store.repository import stable_external_id

    code = (
        "import sys; sys.path.insert(0, 'src');"
        "from acra.store.repository import stable_external_id as f;"
        "print(f('owner/repo'))"
    )
    seen = set()
    for seed in ("0", "1", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())

    assert len(seen) == 1, f"不同进程算出了不同 ID：{seen}"
    assert seen == {str(stable_external_id("owner/repo"))}


def test_external_id_separates_different_repos() -> None:
    from acra.store.repository import stable_external_id

    ids = {stable_external_id(name) for name in ("o/a", "o/b", "local/x", "local/y")}
    assert len(ids) == 4


# ---------------------------------------------------------------------------- 增量基线


def _run_with_status(db, repo_id: int, pr: int, *, head: str, mb: str, status: str) -> None:
    with db.session() as session:
        run = start_run(
            session,
            job_id=f"job-{head}-{status}",
            repository_id=repo_id,
            pr_number=pr,
            base_sha="base",
            head_sha=head,
            merge_base_sha=mb,
            trigger_source="cli",
            mode="full",
        )
        finish_run(session, run, status=status)


@pytest.mark.parametrize("status", ["failed", "queued", "degraded"])
def test_non_succeeded_runs_are_not_incremental_baselines(tmp_path, status: str) -> None:
    """没覆盖完整 diff 的运行不能当增量基线 —— 否则那段代码会被**永久静默跳过**。

    这是最难发现的一类漏审：它表现为"之后几轮都很安静"，看起来像质量变好了。
    """
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="o/r")
            repo_id = repo.id
        _run_with_status(db, repo_id, 5, head="H1", mb="MB1", status=status)

        with db.session() as session:
            assert last_reviewed_sha(session, repo_id, 5) is None
            assert last_merge_base(session, repo_id, 5) is None
    finally:
        db.dispose()


def test_only_succeeded_run_becomes_baseline_even_if_newer_runs_failed(tmp_path) -> None:
    """在成功的运行之后又发生了失败：基线仍应停在成功的那一轮。"""
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="o/r")
            repo_id = repo.id
        _run_with_status(db, repo_id, 5, head="H1", mb="MB1", status="succeeded")
        _run_with_status(db, repo_id, 5, head="H2", mb="MB2", status="failed")

        with db.session() as session:
            # 关键：head 与 merge_base 必须来自**同一次**运行，不能 H1 配 MB2
            assert last_reviewed_sha(session, repo_id, 5) == "H1"
            assert last_merge_base(session, repo_id, 5) == "MB1"
    finally:
        db.dispose()


def test_prior_comments_carry_outcome_labels(tmp_path) -> None:
    """历史评论必须带结果标签，否则两种相反的情况会被同样对待。

    团队驳回过的结论如果被裸着喂回去，等于鼓励模型复活已经被拒绝的意见 ——
    而"不要重复提出"这条提示只有在模型能区分"采纳过/驳回过"时才可判断。
    """
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo = get_or_create_repository(session, full_name="o/r")
            run = start_run(
                session,
                job_id="job-priors",
                repository_id=repo.id,
                pr_number=7,
                base_sha="b",
                head_sha="h",
                merge_base_sha="mb",
                trigger_source="cli",
                mode="full",
            )
            rows = save_findings(
                session,
                run.id,
                [
                    Finding(path="src/A.java", line=1, category="bug", severity="high",
                            confidence=0.9, score=1.0, title="已发布的", body="b1"),
                    Finding(path="src/A.java", line=2, category="bug", severity="high",
                            confidence=0.9, score=1.0, title="被驳回的", body="b2"),
                    Finding(path="src/B.java", line=3, category="style", severity="low",
                            confidence=0.6, score=0.5, title="没发布的", body="b3"),
                ],
            )
            mark_published(session, [rows[0].id])
            set_feedback(session, rows[1].id, is_false_positive=True)

        with db.session() as session:
            result = prior_comments_by_path(session, repo.id, ["src/A.java", "src/B.java"])
            bodies_a = [c.body for c in result["src/A.java"]]
            assert any(b.startswith("[已发布]") for b in bodies_a), bodies_a
            assert any(b.startswith("[已驳回（误报）]") for b in bodies_a), bodies_a
            assert all(b.startswith("[未发布]") for b in (c.body for c in result["src/B.java"]))
    finally:
        db.dispose()


def test_prior_comments_do_not_leak_across_repositories(tmp_path) -> None:
    """同一个路径在别的仓库里指的不是同一份代码，历史不能串。"""
    db = _db(tmp_path)
    try:
        with db.session() as session:
            repo_a = get_or_create_repository(session, full_name="o/a")
            repo_b = get_or_create_repository(session, full_name="o/b")
            for rid, job in ((repo_a.id, "job-a"), (repo_b.id, "job-b")):
                run = start_run(session, job_id=job, repository_id=rid, pr_number=1,
                                base_sha="b", head_sha="h", merge_base_sha="mb",
                                trigger_source="cli", mode="full")
                save_findings(session, run.id, [
                    Finding(path="app/service.py", line=5, category="bug", severity="high",
                            confidence=0.9, score=1.0, title=f"来自 {job}", body="x"),
                ])

        with db.session() as session:
            only_a = prior_comments_by_path(session, repo_a.id, ["app/service.py"])
            assert [c.body for c in only_a["app/service.py"]] == ["[未发布] 来自 job-a：x"]
    finally:
        db.dispose()
