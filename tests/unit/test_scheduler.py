"""队列、幂等与 worker 的单测（文档 §4.2 幂等键、并发限制、dead letter）。"""

from __future__ import annotations

import asyncio

import pytest

from acra.models import ReviewJob
from acra.orchestrator.scheduler import (
    ConcurrencyGuard,
    InMemoryQueue,
    enqueue,
    idempotency_key,
    run_worker,
)


def _job(head: str = "h1", pr: int | None = 42) -> ReviewJob:
    return ReviewJob(
        job_id="j1",
        repo_path="/repo/a",
        repo_full_name="owner/repo",
        pr_number=pr,
        head_sha=head,
    )


def test_idempotency_key_is_stable_and_head_sensitive() -> None:
    assert idempotency_key("repo", 42, "aaa") == idempotency_key("repo", 42, "aaa")
    assert idempotency_key("repo", 42, "aaa") != idempotency_key("repo", 42, "bbb")
    assert idempotency_key("repo", 42, "aaa") != idempotency_key("repo", 43, "aaa")
    assert len(idempotency_key("repo", None, "aaa")) == 64


async def test_repeated_event_is_deduplicated() -> None:
    """平台重投同一事件不能导致同一 PR 被审 3 次、贴 3 组重复评论。"""
    queue = InMemoryQueue()
    first, key = await enqueue(_job(), queue, repo_id="repo")
    second, _ = await enqueue(_job(), queue, repo_id="repo")
    third, _ = await enqueue(_job(head="h2"), queue, repo_id="repo")

    assert first is True
    assert second is False
    assert third is True
    assert await queue.size() == 2


async def test_dequeue_returns_fifo() -> None:
    queue = InMemoryQueue()
    await queue.enqueue(_job("a"), "k1")
    await queue.enqueue(_job("b"), "k2")
    job, key = await queue.dequeue()
    assert (job.head_sha, key) == ("a", "k1")
    assert await queue.size() == 1


async def test_worker_processes_and_dead_letters_failures() -> None:
    queue = InMemoryQueue()
    await queue.enqueue(_job("a", pr=1), "k1")
    await queue.enqueue(_job("b", pr=2), "k2")
    await queue.enqueue(_job("c", pr=3), "k3")

    seen: list[str] = []

    async def handler(job: ReviewJob) -> None:
        if job.head_sha == "b":
            raise RuntimeError("分析炸了")
        seen.append(job.head_sha)

    # 并发限制设为宽松值，本用例只验证"正常处理 + 失败进 dead letter"
    guard = ConcurrencyGuard(per_repo=10, per_pr=10)
    processed = await run_worker(queue, handler, concurrency=1, guard=guard)
    assert processed == 3
    assert sorted(seen) == ["a", "c"]
    assert len(queue.dead_letters) == 1
    assert "RuntimeError" in queue.dead_letters[0]["reason"]


async def test_concurrency_limit_requeues_instead_of_losing_jobs() -> None:
    """并发受限时任务必须退回队列重试，而不是被幂等检查当成重复事件丢掉。"""
    queue = InMemoryQueue()
    # 同一个 PR 的三个提交：单 PR 只允许 1 个并发，必然触发回队
    for sha in ("s1", "s2", "s3"):
        await queue.enqueue(_job(sha, pr=7), f"k-{sha}")

    done: list[str] = []

    async def handler(job: ReviewJob) -> None:
        await asyncio.sleep(0)
        done.append(job.head_sha)

    guard = ConcurrencyGuard(per_repo=10, per_pr=1)
    processed = await run_worker(
        queue, handler, concurrency=2, guard=guard, poll_interval=0.01
    )

    assert processed == 3
    assert sorted(done) == ["s1", "s2", "s3"]  # 一个都不能丢


async def test_worker_stops_when_empty() -> None:
    queue = InMemoryQueue()
    assert await run_worker(queue, lambda job: None) == 0


def test_concurrency_guard_limits_per_pr_and_per_repo() -> None:
    guard = ConcurrencyGuard(per_repo=2, per_pr=1)
    assert guard.try_acquire("repo", 1) is True
    assert guard.try_acquire("repo", 1) is False   # 单 PR 只允许 1 个
    assert guard.try_acquire("repo", 2) is True
    assert guard.try_acquire("repo", 3) is False   # 单仓库上限 2
    guard.release("repo", 1)
    assert guard.try_acquire("repo", 3) is True


def test_concurrency_guard_release_is_balanced() -> None:
    guard = ConcurrencyGuard(per_repo=1, per_pr=1)
    assert guard.try_acquire("r", 5) is True
    guard.release("r", 5)
    guard.release("r", 5)  # 多余的 release 不能把计数压成负数
    assert guard.try_acquire("r", 6) is True


@pytest.mark.parametrize("pr", [None, 7])
async def test_queue_handles_missing_pr_number(pr) -> None:
    queue = InMemoryQueue()
    accepted, _ = await enqueue(_job(pr=pr), queue, repo_id="repo")
    assert accepted is True
    item = await queue.dequeue()
    assert item is not None
    assert item[0].pr_number == pr
