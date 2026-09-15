"""Phase 1 扫描（发散）。

对应开发文档 §4.6：

```
阶段一 Scan（发散）
  模型: 小模型（便宜、快）
  温度: 0.2
  输入: 单块 ReviewContext (L1+L2, 必要时 L3)
  输出: 候选 Finding[]（不设严格门槛，宁多勿漏）
```

阶段一只有这一遍调用，没有工具、没有验证；门槛过滤发生在 validator 与 ranker
（文档 §16 阶段一交付物）。单块失败不拖垮整次审查 —— 只有全部块都失败才升级为
static-only 降级（文档 §4.2）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from acra import PROMPT_TEMPLATE_VERSION
from acra.context.budget import DEFAULT_SHARES, TokenBudget, truncate_tokens
from acra.engine.llm_client import LLMClient, count_prompt_tokens
from acra.engine.prompt_loader import build_scan_messages
from acra.errors import LLMError
from acra.models import Chunk, RawFinding

SCAN_TEMPERATURE = 0.2

#: 单块提示词里系统指令 + 输出 Schema 的预留占比（文档 §7.3）
SYSTEM_SHARE = DEFAULT_SHARES["system"]


@dataclass(slots=True)
class ScanOutcome:
    findings: list[RawFinding] = field(default_factory=list)
    chunks_scanned: int = 0
    chunks_cached: int = 0
    failed_chunks: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    all_failed: bool = False
    cache_hits: int = 0


def chunk_cache_key(chunk: Chunk, model: str, *, version: str | None = None) -> str:
    """内容哈希缓存键（文档 §10.2）。

    必须包含 prompt_template_version —— 提示词改了，旧结果立即失效，
    否则会长期返回一份用旧标准生成的结论。
    """
    payload = "\x00".join(
        [
            model,
            version or PROMPT_TEMPLATE_VERSION,
            chunk.path,
            chunk.diff_text,
            chunk.context.enclosing_source_text,
            chunk.context.static_text,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """模型可能返回数组，也可能包一层对象，两种都接受。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("findings", "issues", "results", "comments"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        if "path" in payload and "line" in payload:
            return [payload]
    return []


def _coerce(items: list[dict[str, Any]], expected_path: str) -> list[RawFinding]:
    out: list[RawFinding] = []
    for item in items:
        try:
            raw = RawFinding.from_dict(item)
        except (TypeError, ValueError):
            continue
        if not raw.path:
            raw.path = expected_path
        if raw.line <= 0:
            continue
        out.append(raw)
    return out


def _shrink_messages_to_budget(
    messages: list[dict[str, str]],
    chunk_budget: int,
) -> list[dict[str, str]]:
    """确保"系统指令 + 输出 Schema"分区不被其它分区挤没（文档 §7.3 第一行）。"""
    system_tokens = count_prompt_tokens(messages[:1])
    allowance = int(chunk_budget * SYSTEM_SHARE * 1.2)
    if system_tokens <= allowance:
        return messages
    trimmed = list(messages)
    trimmed[0] = {
        **trimmed[0],
        "content": truncate_tokens(trimmed[0]["content"], allowance),
    }
    return trimmed


async def scan_chunk(
    chunk: Chunk,
    *,
    client: LLMClient,
    settings,
    custom_conventions: str = "",
) -> list[RawFinding]:
    messages = build_scan_messages(chunk, custom_conventions=custom_conventions)
    messages = _shrink_messages_to_budget(messages, settings.acra_chunk_input_token_budget)
    payload, _ = await client.complete_json("scan", messages, temperature=SCAN_TEMPERATURE)
    return _coerce(_extract_items(payload), chunk.path)


async def scan_chunks(
    chunks: list[Chunk],
    *,
    client: LLMClient,
    settings,
    custom_conventions: str = "",
    cache=None,
    run_budget=None,
    run_budget_cap: int | None = None,
) -> ScanOutcome:
    """并发扫描所有块（文档 §10.3：不同文件块的 Scan 并行，默认并发 4）。"""
    outcome = ScanOutcome()
    if not chunks:
        return outcome

    semaphore = asyncio.Semaphore(max(1, settings.acra_scan_concurrency))

    async def one(chunk: Chunk) -> tuple[Chunk, list[RawFinding] | None, str | None]:
        label = f"{chunk.path}#{chunk.index}"
        key = chunk_cache_key(chunk, settings.llm_scan_model)
        async with semaphore:
            if cache is not None:
                cached = await cache.get(key)
                if cached:
                    try:
                        items = _extract_items(json.loads(cached))
                        return chunk, _coerce(items, chunk.path), None
                    except (json.JSONDecodeError, TypeError):
                        pass
            try:
                found = await scan_chunk(
                    chunk, client=client, settings=settings, custom_conventions=custom_conventions
                )
            except LLMError as exc:
                return chunk, None, f"{label}: {type(exc).__name__}: {exc}"
            if cache is not None:
                await cache.set(
                    key,
                    json.dumps([f.raw or f.__dict__ for f in found], ensure_ascii=False),
                )
            return chunk, found, None

    results = await asyncio.gather(*(one(c) for c in chunks))

    for chunk, found, error in results:
        if error:
            outcome.failed_chunks.append(error)
            continue
        assert found is not None
        outcome.chunks_scanned += 1
        if run_budget is not None:
            run_budget.charge(
                chunk.token_estimate + sum(_rough_tokens(f) for f in found)
            )
        outcome.findings.extend(found)

    if cache is not None:
        outcome.cache_hits = outcome.chunks_cached

    if outcome.chunks_scanned == 0 and outcome.failed_chunks:
        outcome.all_failed = True
        outcome.degraded.append("llm_unavailable")
    elif outcome.failed_chunks:
        outcome.degraded.append(f"partial_chunk_failure:{len(outcome.failed_chunks)}")

    return outcome


def _rough_tokens(raw: RawFinding) -> int:
    return max(1, (len(raw.title) + len(raw.body)) // 3)


def prompt_budget_note(chunk: Chunk, settings) -> str:
    """调试用：查看某块在预算内的实际占用。"""
    budget = TokenBudget(settings.acra_chunk_input_token_budget)
    return (
        f"{chunk.path}#{chunk.index} est={chunk.token_estimate} "
        f"budget={budget.total} note={chunk.context.packed_note}"
    )
