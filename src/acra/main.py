"""FastAPI 入口。

对应开发文档 §6.1（外部 HTTP API）、§13.1（web 与 worker 分离部署）。

阶段一（CLI 本地模式）下这个入口也完整可用：`/webhook/github` 会验签并把任务入队，
`acra_inline_worker=true` 时进程内消费队列直接跑审查；置 false 则改用 `acra worker`
独立消费，对应文档 §13.1 的分离部署形态。

`/authorize` 之外的写接口都要求 `ACRA_ADMIN_TOKEN`（文档 §12.3：`custom_conventions`
属于系统提示的一部分，只能由管理员经鉴权接口写入）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from acra import __version__
from acra.cache.content_hash import MemoryCache
from acra.logging_setup import configure_logging
from acra.observability import metrics as metrics_mod
from acra.orchestrator.pipeline import ReviewOptions, run_review
from acra.orchestrator.scheduler import InMemoryQueue, enqueue, run_worker
from acra.settings import Settings, get_settings
from acra.store.db import Database
from acra.trigger import github_webhook, gitlab_webhook
from acra.trigger.normalize import job_from_cli

logger = logging.getLogger("acra.web")


def _build_cache(settings: Settings):
    if settings.redis_url:
        try:
            from acra.cache.redis_cache import RedisCache

            return RedisCache(settings.redis_url)
        except Exception as exc:  # noqa: BLE001 - Redis 不可用时退回内存缓存
            logger.warning("Redis 不可用，退回内存缓存：%s", exc)
    return MemoryCache()


def _build_queue(settings: Settings):
    if settings.redis_url:
        try:
            from acra.orchestrator.scheduler import RedisQueue

            return RedisQueue(settings.redis_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 队列不可用，退回内存队列：%s", exc)
    return InMemoryQueue()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.db = Database(settings.database_url or None, settings.ensure_workdir())
        # 本地 SQLite 也建表：CLI 与 API 共用一份运行记录
        app.state.db.create_all()
        app.state.cache = _build_cache(settings)
        app.state.queue = _build_queue(settings)

        worker_task: asyncio.Task | None = None
        if settings.acra_inline_worker:

            async def handler(job):
                outcome = await run_review(
                    job,
                    settings,
                    options=ReviewOptions(),
                    db=app.state.db,
                    cache=app.state.cache,
                )
                metrics_mod.observe_outcome(outcome)
                return outcome

            async def loop() -> None:
                # 常驻消费；每次取空后短暂休眠，避免空转
                while True:
                    await run_worker(
                        app.state.queue,
                        handler,
                        concurrency=2,
                        stop_when_empty=False,
                        max_iterations=1,
                        poll_interval=1.0,
                    )
                    await asyncio.sleep(1.0)

            worker_task = asyncio.create_task(loop())

        try:
            yield
        finally:
            if worker_task is not None:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task
            await _maybe_aclose(app.state.cache)
            await _maybe_aclose(app.state.queue)
            app.state.db.dispose()

    app = FastAPI(
        title="acra · AI 代码审查助手",
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(github_webhook.router)
    app.include_router(gitlab_webhook.router)

    # ---------------------------------------------------------------- 探针

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        checks: dict[str, Any] = {}
        try:
            with request.app.state.db.session() as session:
                from sqlalchemy import text

                session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["database"] = f"error:{type(exc).__name__}"

        checks["redis"] = "configured" if settings.redis_url else "not_configured"
        checks["sandbox"] = "enabled" if settings.acra_sandbox_enabled else "disabled"
        checks["llm"] = "configured" if settings.llm_configured else "missing_key"
        checks["shadow_mode"] = settings.acra_shadow_mode

        ready = checks["database"] == "ok"
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    if metrics_mod.available():
        asgi = metrics_mod.metrics_asgi_app()
        if asgi is not None:
            app.mount("/metrics", asgi)

    # ---------------------------------------------------------------- 鉴权

    async def require_admin(authorization: str = Header(default="")) -> None:
        expected = settings.acra_admin_token
        if not expected:
            raise HTTPException(status_code=503, detail="ACRA_ADMIN_TOKEN 未配置，管理接口关闭")
        if authorization != f"Bearer {expected}":
            raise HTTPException(status_code=403, detail="forbidden")

    # ---------------------------------------------------------------- 手动触发

    @app.post("/api/v1/reviews", dependencies=[Depends(require_admin)])
    async def create_review(request: Request) -> dict[str, Any]:
        body = await request.json()
        repo = str(body.get("repo") or "")
        if not repo:
            raise HTTPException(status_code=400, detail="repo 必填")
        job = job_from_cli(
            repo_path=str(body.get("path") or repo),
            base_ref=body.get("base"),
            head_ref=body.get("head"),
            pr_number=body.get("pr_number"),
            repo_full_name=repo,
            force_full=bool(body.get("force_full")),
        )
        accepted, key = await enqueue(job, request.app.state.queue, repo_id=repo)
        if not accepted:
            return {"status": "duplicate", "key": key[:12]}
        return {"job_id": job.job_id, "status": "queued"}

    @app.get("/api/v1/reviews", dependencies=[Depends(require_admin)])
    async def list_reviews(request: Request, limit: int = 20) -> dict[str, Any]:
        from sqlalchemy import select

        from acra.store.models import ReviewRun

        with request.app.state.db.session() as session:
            rows = (
                session.execute(
                    select(ReviewRun).order_by(ReviewRun.created_at.desc()).limit(min(limit, 200))
                )
                .scalars()
                .all()
            )
            return {
                "items": [
                    {
                        "run_id": r.id,
                        "job_id": r.job_id,
                        "pr_number": r.pr_number,
                        "head_sha": r.head_sha,
                        "status": r.status,
                        "mode": r.mode,
                        "findings_kept": r.findings_kept,
                        "duration_ms": r.duration_ms,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            }

    # ---------------------------------------------------------------- 仓库配置

    @app.get("/api/v1/repos/{repo_id}/config", dependencies=[Depends(require_admin)])
    async def get_config(request: Request, repo_id: int) -> dict[str, Any]:
        from dataclasses import asdict

        from acra.store.repository import load_repo_config

        with request.app.state.db.session() as session:
            return asdict(load_repo_config(session, repo_id))

    @app.put("/api/v1/repos/{repo_id}/config", dependencies=[Depends(require_admin)])
    async def put_config(request: Request, repo_id: int) -> dict[str, Any]:
        from dataclasses import asdict

        from acra.store.repository import load_repo_config, save_repo_config

        body = await request.json()
        # custom_conventions 属于系统提示的一部分，只能经此鉴权接口写入（文档 §12.3）
        with request.app.state.db.session() as session:
            save_repo_config(session, repo_id, body)
            return asdict(load_repo_config(session, repo_id))

    # ---------------------------------------------------------------- 反馈与指标

    @app.post("/api/v1/findings/{finding_id}/feedback", dependencies=[Depends(require_admin)])
    async def feedback(request: Request, finding_id: int) -> dict[str, Any]:
        from acra.store.repository import set_feedback

        body = await request.json()
        is_fp = bool(body.get("is_false_positive"))
        with request.app.state.db.session() as session:
            ok = set_feedback(session, finding_id, is_false_positive=is_fp)
        if not ok:
            raise HTTPException(status_code=404, detail="finding not found")
        metrics_mod.feedback_total.labels(verdict="false_positive" if is_fp else "valid").inc()
        return {"ok": True}

    @app.get("/api/v1/metrics/summary", dependencies=[Depends(require_admin)])
    async def summary(request: Request, days: int = 30) -> dict[str, Any]:
        from acra.store.repository import metrics_summary

        with request.app.state.db.session() as session:
            return metrics_summary(session, days=days)

    return app


async def _maybe_aclose(obj: Any) -> None:
    aclose = getattr(obj, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(Exception):
            await aclose()


app = create_app()
