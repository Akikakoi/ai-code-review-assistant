"""GitHub App webhook 入口。

对应开发文档 §4.1 与 §12.3：

- 校验 `X-Hub-Signature-256`（HMAC-SHA256 + app secret），失败直接 401，不进入任何处理；
- 用 `X-GitHub-Delivery` 做幂等键的第一层（平台会重投同一事件）；
- 只入队，不执行分析；
- 请求体大小上限，防止超大 payload 打满内存。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from acra.orchestrator.scheduler import enqueue
from acra.trigger.normalize import normalize_github_event

logger = logging.getLogger("acra.trigger")

router = APIRouter()

MAX_BODY_BYTES = 5 * 1024 * 1024


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """常量时间比较，避免时序侧信道。"""
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


def get_queue(request: Request):
    queue = getattr(request.app.state, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="queue unavailable")
    return queue


@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
):
    settings = request.app.state.settings
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    if not settings.github_webhook_secret:
        # 未配置 secret 时拒绝，而不是放行 —— 宁可显式失败也不留静默的鉴权缺口
        raise HTTPException(status_code=401, detail="webhook secret not configured")

    if not verify_signature(raw, x_hub_signature_256, settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc

    job, reason = normalize_github_event(x_github_event, payload, x_github_delivery or None)
    if job is None:
        return {"status": "ignored", "reason": reason}

    accepted, key = await enqueue(job, get_queue(request), repo_id=job.repo_full_name)
    if not accepted:
        return {"status": "duplicate", "key": key[:12]}
    return {"status": "queued", "job_id": job.job_id, "reason": reason}
