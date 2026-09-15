"""入队、幂等、并发控制、worker。

对应开发文档 §4.1（webhook 只做验签 → 规范化 → 入队）、§4.2（幂等键与并发限制）、
§13.1（Redis list 队列，失败进 dead letter 队列人工可重放）。

幂等键：

```
idempotency_key = sha256(f"{repo_id}:{pr_number}:{head_sha}")
```

命中的任务直接返回既有结果。这一点在平台重投事件时至关重要，否则会出现同一 PR 被审 3 次、
贴 3 组重复评论的灾难（文档 §4.2）。

阶段一提供进程内实现（零依赖、单机 CLI）；配置 `REDIS_URL` 后切到 Redis list，
语义一致。`web` 与 `worker` 分离部署（文档 §13.1）时使用 Redis 实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from acra.errors import ConfigError
from acra.models import ReviewJob

#: 单仓库同时最多 2 个审查任务（文档 §4.2）
MAX_PER_REPO = 2
#: 单 PR 同时最多 1 个（文档 §4.2）
MAX_PER_PR = 1

DEAD_LETTER_SUFFIX = ":dead"


def idempotency_key(repo_id: str, pr_number: int | None, head_sha: str) -> str:
    return hashlib.sha256(f"{repo_id}:{pr_number or 0}:{head_sha}".encode()).hexdigest()


class JobQueue(Protocol):
    async def enqueue(self, job: ReviewJob, key: str) -> bool: ...

    async def requeue(self, job: ReviewJob, key: str) -> None:
        """把任务放回队列，**跳过幂等检查**。

        并发受限（单仓库 2 个 / 单 PR 1 个）时任务要退回队列稍后再试；若这里也走
        幂等检查，任务的 key 早已被记录，回队会被当成重复事件静默丢弃 ——
        表现为"任务凭空消失"，比排队更糟。
        """
        ...

    async def dequeue(self) -> tuple[ReviewJob, str] | None: ...

    async def size(self) -> int: ...

    async def dead_letter(self, job: ReviewJob, key: str, reason: str) -> None: ...


class InMemoryQueue:
    """进程内队列 + 幂等集合。单机 CLI / 测试用。"""

    def __init__(self) -> None:
        self._queue: deque[tuple[ReviewJob, str]] = deque()
        self._seen: set[str] = set()
        self._dead: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, job: ReviewJob, key: str) -> bool:
        async with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._queue.append((job, key))
            return True

    async def requeue(self, job: ReviewJob, key: str) -> None:
        async with self._lock:
            self._queue.append((job, key))

    async def dequeue(self) -> tuple[ReviewJob, str] | None:
        async with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    async def size(self) -> int:
        return len(self._queue)

    async def dead_letter(self, job: ReviewJob, key: str, reason: str) -> None:
        self._dead.append({"job_id": job.job_id, "key": key, "reason": reason})

    @property
    def dead_letters(self) -> list[dict[str, Any]]:
        return list(self._dead)

    def forget(self, key: str) -> None:
        """允许重放：清除幂等记录。"""
        self._seen.discard(key)


class RedisQueue:
    """Redis list 队列（`REDIS_URL` 配置后启用）。"""

    QUEUE_KEY = "acra:queue"
    IDEMPOTENT_PREFIX = "acra:idem:"

    def __init__(self, redis_url: str) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:  # pragma: no cover
            raise ConfigError(
                "已配置 REDIS_URL 但未安装 redis 包，请执行：pip install 'acra[redis]'"
            ) from exc
        self._client = aioredis.from_url(redis_url, decode_responses=True)

    async def enqueue(self, job: ReviewJob, key: str) -> bool:
        # 幂等键 TTL 24h：足够覆盖平台重投窗口，也不会无限堆积
        acquired = await self._client.set(self.IDEMPOTENT_PREFIX + key, "1", nx=True, ex=86_400)
        if not acquired:
            return False
        await self._client.rpush(self.QUEUE_KEY, json.dumps(_job_to_dict(job)))
        return True

    async def dequeue(self) -> tuple[ReviewJob, str] | None:
        item = await self._client.lpop(self.QUEUE_KEY)
        if not item:
            return None
        job = _job_from_dict(json.loads(item))
        return job, idempotency_key(job.repo_path, job.pr_number, job.head_sha or "")

    async def size(self) -> int:
        return int(await self._client.llen(self.QUEUE_KEY))

    async def dead_letter(self, job: ReviewJob, key: str, reason: str) -> None:
        await self._client.rpush(
            self.QUEUE_KEY + DEAD_LETTER_SUFFIX,
            json.dumps({"job": _job_to_dict(job), "key": key, "reason": reason}),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _job_to_dict(job: ReviewJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "platform": job.platform,
        "repo_full_name": job.repo_full_name,
        "repo_path": job.repo_path,
        "pr_number": job.pr_number,
        "base_ref": job.base_ref,
        "head_ref": job.head_ref,
        "base_sha": job.base_sha,
        "head_sha": job.head_sha,
        "trigger_source": job.trigger_source,
        "force_full": job.force_full,
        "remote_url": job.remote_url,
        "installation_id": job.installation_id,
        "delivery_id": job.delivery_id,
    }


def _job_from_dict(data: dict[str, Any]) -> ReviewJob:
    return ReviewJob(**data)


@dataclass(slots=True)
class ConcurrencyGuard:
    """单仓库 / 单 PR 并发限制（文档 §4.2）。进程内实现。

    计数容器必须是 dataclass 字段：`slots=True` 的类没有 `__dict__`，
    在 `__post_init__` 里直接赋值一个新属性会抛 AttributeError。
    """

    per_repo: int = MAX_PER_REPO
    per_pr: int = MAX_PER_PR
    _repo_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _pr_keys: set[str] = field(default_factory=set, init=False, repr=False)

    def try_acquire(self, repo_id: str, pr_number: int | None) -> bool:
        pr_key = f"{repo_id}:{pr_number or 0}"
        if self._repo_counts.get(repo_id, 0) >= self.per_repo:
            return False
        if pr_number is not None and pr_key in self._pr_keys:
            return False
        self._repo_counts[repo_id] = self._repo_counts.get(repo_id, 0) + 1
        if pr_number is not None:
            self._pr_keys.add(pr_key)
        return True

    def release(self, repo_id: str, pr_number: int | None) -> None:
        count = self._repo_counts.get(repo_id, 0)
        if count <= 1:
            self._repo_counts.pop(repo_id, None)
        else:
            self._repo_counts[repo_id] = count - 1
        if pr_number is not None:
            self._pr_keys.discard(f"{repo_id}:{pr_number}")


async def enqueue(
    job: ReviewJob,
    queue: JobQueue,
    *,
    repo_id: str | None = None,
) -> tuple[bool, str]:
    """入队。返回 (是否新任务, 幂等键)。"""
    key = idempotency_key(repo_id or job.repo_path, job.pr_number, job.head_sha or job.head_ref or "")
    accepted = await queue.enqueue(job, key)
    return accepted, key


Handler = Callable[[ReviewJob], Awaitable[None]]


async def run_worker(
    queue: JobQueue,
    handler: Handler,
    *,
    concurrency: int = 2,
    guard: ConcurrencyGuard | None = None,
    poll_interval: float = 0.5,
    stop_when_empty: bool = True,
    max_iterations: int | None = None,
) -> int:
    """消费队列。失败的任务进 dead letter 队列，人工可重放（文档 §13.1）。

    返回处理过的任务数。
    """
    guard = guard or ConcurrencyGuard()
    processed = 0
    running: set[asyncio.Task] = set()
    iterations = 0

    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break

        item = await queue.dequeue()
        if item is None:
            if stop_when_empty and not running:
                break
            if running:
                done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    _log_task_result(task)
                continue
            await asyncio.sleep(poll_interval)
            continue

        job, key = item
        repo_id = job.repo_path
        if not guard.try_acquire(repo_id, job.pr_number):
            # 并发受限：退回队列尾部，稍后重试（必须绕过幂等检查，否则任务会凭空消失）
            await queue.requeue(job, key)
            await asyncio.sleep(poll_interval)
            continue

        async def _run(job: ReviewJob = job, repo_id: str = repo_id, key: str = key) -> None:
            try:
                await handler(job)
            except Exception as exc:  # noqa: BLE001 - worker 必须吞掉所有异常，否则队列会停摆
                await queue.dead_letter(job, key, f"{type(exc).__name__}: {exc}")
                raise
            finally:
                guard.release(repo_id, job.pr_number)

        while len(running) >= max(1, concurrency):
            done, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                _log_task_result(task)
        running.add(asyncio.create_task(_run()))
        processed += 1

    if running:
        done, _ = await asyncio.wait(running)
        for task in done:
            _log_task_result(task)
    return processed


def _log_task_result(task: asyncio.Task) -> None:
    import logging

    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger("acra.worker").warning("任务失败：%s: %s", type(exc).__name__, exc)
