"""Phase 2 验证（收敛）。

对应开发文档 §4.6、§8.3 与 §16 阶段二。

```
阶段二 Verify（收敛）
  模型: 强模型（llm_verify_model）
  温度: 0.0
  输入: 单条候选 Finding + 其所在方法的完整源码 + 相关静态结论
  输出: verdict ∈ {confirmed, rejected, uncertain} + 理由 + 修正后的行号
```

为什么必须分两阶段：单 prompt 同时做"找问题"和"判断问题是否成立"会让模型倾向于
自我一致 —— 它已经写出来的结论，很难在同一个上下文里否定自己。拆成两次独立调用后，
验证阶段没有"沉没成本"，拒绝率显著提升（文档 §4.6）。

几条实现上的取舍：

- **串行逐条**（文档 §10.3）：并发验证会让多条候选出现在同一次上下文里，模型之间
  互相影响判断；而且串行才能让时间预算精确生效。
- **失败不丢结论**（fail-open）：Verify 调用出错时保留原候选。验证是"增加确定性"的
  环节，不该因为它自己出问题而让一条可能正确的结论消失。
- **行号修正只是建议**：`adjusted_line` 必须落在本次变更行白名单内才会被采纳，
  否则忽略修正并记录。锚定始终是最终权威（文档 P2）。
- **关闭状态不是降级**：它是配置事实，只写进说明，不把整次运行标记成 degraded。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from acra.engine.llm_client import LLMClient
from acra.engine.prompt_loader import build_verify_messages
from acra.errors import LLMError
from acra.models import SEVERITY_RANK, DropRecord, RawFinding, Severity

VERIFY_TEMPERATURE = 0.0

#: verdict = uncertain 时的**展示优先级**折扣。
#: 刻意不降 confidence：confidence 表达"这条结论有多可能成立"，
#: 而 uncertain 表达的是"需要人来判断" —— 后者应该影响它被展示的位置，
#: 不该影响它的真值估计。若改成降 confidence，一条置信度刚好在门槛之上的结论
#: 会直接掉到门槛之下被丢弃，等于把"不确定"当成了"丢掉"。
UNCERTAIN_SCORE_PENALTY = 0.8

#: verdict = confirmed 时，置信度的下限。
#:
#: 这条修的是一个真实缺陷：Verify 返回 `{"verdict":"confirmed","reason":...}` 时往往不给
#: confidence 字段，于是候选沿用 Scan 阶段的置信度（常在 0.65 门槛之下）→ 被第 8 步
#: 整条丢弃。实测 15 个注入用例里有 5 条真阳性就是这样"被 Verify 确认后扔掉"的。
#:
#: 语义上也说得通：一次**独立的二次确认**比初筛时的自报置信度是更强的证据，
#: 这正是两阶段设计的用意。只有在模型没给明确 confidence 时才抬到下限。
VERIFIED_CONFIDENCE_FLOOR = 0.8

VALID_SEVERITIES = {s.value for s in Severity}
VALID_VERDICTS = ("confirmed", "rejected", "uncertain")


@dataclass(slots=True)
class VerifyOutcome:
    kept: list[RawFinding] = field(default_factory=list)
    dropped: list[DropRecord] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    enabled: bool = False
    confirmed: int = 0
    rejected: int = 0
    uncertain: int = 0
    unverified: int = 0
    failed: int = 0
    spent_seconds: float = 0.0

    @property
    def rejection_rate(self) -> float:
        total = self.confirmed + self.rejected + self.uncertain
        return self.rejected / total if total else 0.0

    def summary_note(self) -> str:
        if not self.enabled:
            return "未启用两阶段 Verify（ACRA_VERIFY_ENABLED=false）"
        parts = [
            f"Verify：确认 {self.confirmed}",
            f"驳回 {self.rejected}",
            f"存疑 {self.uncertain}",
        ]
        if self.unverified:
            parts.append(f"未验证 {self.unverified}（超出预算）")
        if self.failed:
            parts.append(f"验证失败 {self.failed}（已保留原结论）")
        parts.append(f"驳回率 {self.rejection_rate:.0%}")
        return "；".join(parts)


def _candidate_key(raw: RawFinding) -> str:
    return f"{raw.path}:{raw.line}:{raw.category}"


def _pick_context(raw: RawFinding, contexts: Mapping[str, list[tuple[set[int], str]]] | None) -> str:
    """取该条结论所在的仓库内容块。

    一个文件可能有多个块（分块），优先选"行号落在块内"的那个。
    """
    if not contexts:
        return ""
    blocks = contexts.get(raw.path) or []
    for lines, block in blocks:
        if raw.line in lines:
            return block
    return blocks[0][1] if blocks else ""


def _apply_verdict(
    raw: RawFinding,
    payload: Mapping[str, object],
    *,
    allowed_lines: set[int] | None,
) -> tuple[str, list[str]]:
    """把模型返回的判断落到候选上，返回 (verdict, 备注)。"""
    notes: list[str] = []
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        # 认不出的判断不能当成"确认"，按存疑处理更保守
        notes.append(f"verdict 非法({verdict or '空'})，按 uncertain 处理")
        verdict = "uncertain"

    raw.raw["_verify_verdict"] = verdict
    reason = str(payload.get("reason") or "").strip()
    if reason:
        raw.raw["_verify_reason"] = reason[:300]

    adjusted = payload.get("adjusted_line")
    if isinstance(adjusted, int) and adjusted > 0 and adjusted != raw.line:
        if allowed_lines is None or adjusted in allowed_lines:
            notes.append(f"行号由 {raw.line} 修正为 {adjusted}")
            raw.line = adjusted
        else:
            notes.append(f"忽略行号修正 {adjusted}（不在本次变更行内）")

    severity = str(payload.get("severity") or "").strip().lower()
    if severity in VALID_SEVERITIES and severity != raw.severity:
        notes.append(f"severity 由 {raw.severity} 调整为 {severity}")
        raw.severity = severity

    confidence = payload.get("confidence")
    explicit_confidence = isinstance(confidence, int | float) and 0.0 <= float(confidence) <= 1.0
    if explicit_confidence:
        raw.confidence = round(float(confidence), 4)
    elif verdict == "confirmed" and raw.confidence < VERIFIED_CONFIDENCE_FLOOR:
        notes.append(
            f"confidence 由 {raw.confidence} 提升至 {VERIFIED_CONFIDENCE_FLOOR}（Verify 已确认）"
        )
        raw.confidence = VERIFIED_CONFIDENCE_FLOOR

    if payload.get("needs_human_judgment") is True:
        raw.needs_human_judgment = True

    if verdict == "uncertain":
        # 只降展示优先级，不动 confidence —— 理由见 UNCERTAIN_SCORE_PENALTY 的注释
        raw.raw["_score_penalty"] = UNCERTAIN_SCORE_PENALTY
        raw.needs_human_judgment = True

    if notes:
        raw.raw.setdefault("_adjustments", []).extend(f"verify:{n}" for n in notes)
    return verdict, notes


async def _verify_one(
    raw: RawFinding,
    *,
    client: LLMClient,
    block: str,
) -> Mapping[str, object]:
    messages = build_verify_messages(
        path=raw.path,
        line=raw.line,
        category=raw.category,
        title=raw.title,
        body=raw.body,
        repo_content_block=block or "(未提供仓库内容)",
    )
    payload, _ = await client.complete_json("verify", messages, temperature=VERIFY_TEMPERATURE)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, Mapping):
        raise LLMError("verify 返回的不是对象")
    return payload


async def verify_candidates(
    candidates: list[RawFinding],
    *,
    client: LLMClient | None,
    settings,
    diff_set=None,
    contexts: Mapping[str, list[tuple[set[int], str]]] | None = None,
    run_budget=None,
) -> VerifyOutcome:
    """逐条验证候选结论。"""
    outcome = VerifyOutcome()
    if not settings.acra_verify_enabled:
        outcome.kept = list(candidates)
        return outcome

    outcome.enabled = True
    if not candidates:
        return outcome

    if client is None:
        outcome.kept = list(candidates)
        outcome.degraded.append("verify_no_client")
        return outcome

    whitelist: dict[str, set[int]] = diff_set.whitelist() if diff_set is not None else {}
    limit = max(0, settings.acra_verify_max_candidates)
    budget_seconds = max(1, settings.acra_verify_budget_seconds)
    started = time.monotonic()

    # 先验证"最可能被展示"的那些：严重度高、置信度高。
    # 预算不够时，没被验证的保持原样透传（宁可少验证，不要丢结论）。
    ordered = sorted(
        candidates,
        key=lambda c: (SEVERITY_RANK.get(c.severity, 9), -c.confidence, c.path, c.line),
    )

    kept: list[RawFinding] = []
    floor = settings.acra_verify_min_confidence
    for index, raw in enumerate(ordered):
        if raw.confidence < floor:
            # 置信度已经低到不值得花一次调用：直接透传，让第 8 步门槛去决定它的去留。
            # 注意门槛不能设在 acra_confidence_threshold 之上 —— 那会掐掉"低置信但正确、
            # 本该被 Verify 救回来"的结论，而救回这类结论正是两阶段的用意。
            outcome.unverified += 1
            kept.append(raw)
            continue
        over_limit = index >= limit
        over_time = (time.monotonic() - started) > budget_seconds
        if over_limit or over_time:
            outcome.unverified += 1
            kept.append(raw)
            continue

        block = _pick_context(raw, contexts)
        try:
            payload = await _verify_one(raw, client=client, block=block)
        except LLMError as exc:
            # fail-open：验证失败不该让一条可能正确的结论消失
            outcome.failed += 1
            raw.raw["_verify_verdict"] = "unverified"
            raw.raw.setdefault("_adjustments", []).append(
                f"verify:调用失败({type(exc).__name__})，保留原结论"
            )
            kept.append(raw)
            continue

        verdict, _notes = _apply_verdict(
            raw, payload, allowed_lines=whitelist.get(raw.path)
        )
        outcome.verdicts[_candidate_key(raw)] = verdict

        if verdict == "rejected":
            outcome.rejected += 1
            reason = str(payload.get("reason") or "verify 判定不成立")
            outcome.dropped.append(DropRecord("verify", f"rejected:{reason[:120]}", raw))
            continue
        if verdict == "confirmed":
            outcome.confirmed += 1
        else:
            outcome.uncertain += 1
        kept.append(raw)

        if run_budget is not None:
            run_budget.charge(len(block) // 3 + 400)

    outcome.kept = kept
    outcome.spent_seconds = round(time.monotonic() - started, 3)

    if outcome.unverified:
        outcome.degraded.append(
            f"verify_budget_exhausted:{outcome.unverified} 条候选未验证（保持原样）"
        )
    if outcome.failed:
        outcome.degraded.append(f"verify_call_failed:{outcome.failed} 条调用失败（保留原结论）")
    return outcome
