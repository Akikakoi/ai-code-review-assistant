"""Verify（两阶段收敛）的单测。

文档 §14.1：「Verify 的 verdict 处理与工具调用预算」。

用 respx 拦截 HTTP，因为真正要验证的是"模型返回各种 verdict 时我们怎么处理"，
而不是"HTTP 通不通"。覆盖点：
  - 关闭状态不把运行标记成降级（配置事实，不是降级）
  - rejected 进 dropped 且带原因
  - uncertain 降权 + 强制标记需人工判断
  - adjusted_line 只在变更行白名单内才采纳（锚定仍是最终权威）
  - verdict 非法时按 uncertain 处理（不能默认当成确认）
  - 调用失败 fail-open：保留原结论，不因为验证环节出错而丢结论
  - 次数与时长预算：超出后原样透传而非丢弃
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from acra.engine.verify import UNCERTAIN_SCORE_PENALTY, verify_candidates
from acra.models import DiffSet, RawFinding
from acra.settings import Settings

VERIFY_URL = "https://api.test.local/v1/chat/completions"


def _raw(path: str = "src/A.java", line: int = 10, **kw) -> RawFinding:
    payload = {
        "path": path,
        "line": line,
        "end_line": None,
        "category": "bug",
        "severity": "high",
        "confidence": 0.8,
        "title": "空值校验被取反",
        "body": "现象：…；触发条件：…；影响：…；修复建议：…",
        "suggestion": None,
        "evidence": ["if (a == null && a.isEmpty())"],
        "needs_human_judgment": False,
        **kw,
    }
    return RawFinding.from_dict(payload)


def _diff_set(*paths_and_lines) -> DiffSet:
    from acra.repo.diff_parser import parse_unified_diff

    files = []
    for path, lines in paths_and_lines:
        body = "\n".join(f"+line {n}" for n in lines)
        files.extend(
            parse_unified_diff(
                f"diff --git a/{path} b/{path}\n"
                "index 111..222 100644\n"
                f"--- a/{path}\n+++ b/{path}\n"
                f"@@ -{min(lines)},0 +{min(lines)},{len(lines)} @@\n{body}\n"
            )
        )
    return DiffSet(base_sha="b" * 40, head_sha="h" * 40, merge_base_sha="m" * 40, files=files)


def _reply(payload: dict) -> dict:
    return {
        "id": "x",
        "object": "chat.completion",
        "model": "verify-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(payload)},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


async def _client(settings):
    from acra.engine.llm_client import LLMClient

    return LLMClient(settings)


# ---------------------------------------------------------------------------- 关闭与退化


@pytest.mark.asyncio
async def test_disabled_passes_through_without_degrade(settings) -> None:
    settings.acra_verify_enabled = False
    candidate = _raw()
    outcome = await verify_candidates([candidate], client=None, settings=settings)

    assert outcome.enabled is False
    assert outcome.kept == [candidate]
    assert outcome.degraded == []  # 关闭是配置事实，不是降级
    assert "未启用" in outcome.summary_note()


@pytest.mark.asyncio
async def test_enabled_without_client_degrades(settings) -> None:
    settings.acra_verify_enabled = True
    candidate = _raw()
    outcome = await verify_candidates([candidate], client=None, settings=settings)

    assert outcome.enabled is True
    assert outcome.kept == [candidate]
    assert outcome.degraded == ["verify_no_client"]


@pytest.mark.asyncio
async def test_no_candidates_is_noop(settings) -> None:
    settings.acra_verify_enabled = True
    outcome = await verify_candidates([], client=None, settings=settings)
    assert outcome.kept == []
    assert outcome.enabled is True


# ---------------------------------------------------------------------------- verdict 处理


@pytest.mark.asyncio
@respx.mock
async def test_confirmed_is_kept_and_recorded(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_reply({"verdict": "confirmed", "reason": "确实会在 null 上调用 isEmpty"}),
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert outcome.confirmed == 1
    assert len(outcome.kept) == 1
    assert outcome.kept[0].raw["_verify_verdict"] == "confirmed"
    assert "确实会" in outcome.kept[0].raw["_verify_reason"]
    assert outcome.rejection_rate == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_rejected_is_dropped_with_reason(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "rejected", "reason": "框架已在入口做了非空校验"})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert outcome.rejected == 1
    assert outcome.kept == []
    assert len(outcome.dropped) == 1
    assert outcome.dropped[0].step == "verify"
    assert outcome.dropped[0].reason.startswith("rejected:")
    assert "非空校验" in outcome.dropped[0].reason
    assert outcome.rejection_rate == 1.0


@pytest.mark.asyncio
@respx.mock
async def test_uncertain_is_penalized_and_flagged(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "uncertain", "reason": "取决于调用方"}))
    )
    client = await _client(settings)
    try:
        candidate = _raw()
        outcome = await verify_candidates(
            [candidate], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert outcome.uncertain == 1
    kept = outcome.kept[0]
    # 关键：不动 confidence（真值估计），只降展示优先级
    assert kept.confidence == pytest.approx(0.8)
    assert kept.raw["_score_penalty"] == pytest.approx(UNCERTAIN_SCORE_PENALTY)
    assert kept.needs_human_judgment is True


@pytest.mark.asyncio
@respx.mock
async def test_illegal_verdict_is_treated_as_uncertain(settings) -> None:
    """认不出的判断不能默认当成"确认" —— 那等于悄悄放行。"""
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "probably fine"}))
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert outcome.uncertain == 1
    assert outcome.confirmed == 0
    assert outcome.kept[0].raw["_verify_verdict"] == "uncertain"


@pytest.mark.asyncio
@respx.mock
async def test_missing_verdict_field_is_treated_as_uncertain(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(return_value=httpx.Response(200, json=_reply({"reason": "嗯"})))
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()
    assert outcome.uncertain == 1


# ---------------------------------------------------------------------------- 行号与字段修正


@pytest.mark.asyncio
@respx.mock
async def test_adjusted_line_inside_whitelist_is_applied(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "confirmed", "reason": "行号偏了一行", "adjusted_line": 11})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw(line=10)],
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10, 11])),
        )
    finally:
        await client.aclose()

    assert outcome.kept[0].line == 11
    assert any("修正为 11" in n for n in outcome.kept[0].raw["_adjustments"])


@pytest.mark.asyncio
@respx.mock
async def test_adjusted_line_outside_whitelist_is_ignored(settings) -> None:
    """锚定是最终权威：Verify 也不能把结论挪到没有变更的行上。"""
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "confirmed", "reason": "x", "adjusted_line": 999})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw(line=10)],
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10, 11])),
        )
    finally:
        await client.aclose()

    assert outcome.kept[0].line == 10
    assert any("忽略行号修正" in n for n in outcome.kept[0].raw["_adjustments"])


@pytest.mark.asyncio
@respx.mock
async def test_severity_and_confidence_are_updated(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200,
            json=_reply(
                {"verdict": "confirmed", "reason": "x", "severity": "medium", "confidence": 0.42}
            ),
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    kept = outcome.kept[0]
    assert kept.severity == "medium"
    assert kept.confidence == pytest.approx(0.42)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_confidence_is_ignored(settings) -> None:
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "confirmed", "reason": "x", "confidence": 1.7})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()
    assert outcome.kept[0].confidence == pytest.approx(0.8)  # 保持原值


# ---------------------------------------------------------------------------- 预算与容错


@pytest.mark.asyncio
@respx.mock
async def test_call_failure_keeps_candidate(settings) -> None:
    """fail-open：验证环节自己坏了，不该让一条可能正确的结论消失。"""
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(return_value=httpx.Response(500, text="boom"))
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw()], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert outcome.failed == 1
    assert len(outcome.kept) == 1
    assert outcome.kept[0].raw["_verify_verdict"] == "unverified"
    assert any("verify_call_failed" in n for n in outcome.degraded)


@pytest.mark.asyncio
@respx.mock
async def test_max_candidates_budget_skips_rest(settings) -> None:
    settings.acra_verify_enabled = True
    settings.acra_verify_max_candidates = 1
    route = respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))
    )
    client = await _client(settings)
    try:
        candidates = [_raw(line=10), _raw(line=11), _raw(line=12)]
        outcome = await verify_candidates(
            candidates,
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10, 11, 12])),
        )
    finally:
        await client.aclose()

    assert route.call_count == 1
    assert len(outcome.kept) == 3  # 未验证的原样透传，不是丢弃
    assert outcome.unverified == 2
    assert any("verify_budget_exhausted" in n for n in outcome.degraded)


@pytest.mark.asyncio
@respx.mock
async def test_low_confidence_candidates_skip_verify(settings) -> None:
    """置信度低到没有验证价值的候选不该占用调用次数。"""
    settings.acra_verify_enabled = True
    settings.acra_verify_min_confidence = 0.4
    route = respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))
    )
    client = await _client(settings)
    try:
        candidates = [_raw(line=10, confidence=0.20), _raw(line=11, confidence=0.85)]
        outcome = await verify_candidates(
            candidates,
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10, 11])),
        )
    finally:
        await client.aclose()

    assert route.call_count == 1  # 只有 0.85 那条被送验
    assert len(outcome.kept) == 2
    assert outcome.unverified == 1
    # 未被验证的保持原状（按行号取，不要依赖顺序 —— Verify 会按严重度/置信度重排）
    low = next(c for c in outcome.kept if c.line == 10)
    assert low.confidence == pytest.approx(0.20)
    assert low.raw.get("_verify_verdict") is None


@pytest.mark.asyncio
def test_verify_floor_must_be_below_drop_threshold() -> None:
    """门槛必须低于丢弃阈值，否则会把"低置信但正确"的结论直接吞掉。"""
    defaults = Settings(_env_file=None)
    assert defaults.acra_verify_min_confidence < defaults.acra_confidence_threshold


@pytest.mark.asyncio
@respx.mock
async def test_time_budget_stops_further_calls(settings) -> None:
    settings.acra_verify_enabled = True
    settings.acra_verify_budget_seconds = 1
    route = respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw(line=10)],
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10])),
        )
    finally:
        await client.aclose()
    # 至少保证：预算耗尽时不会丢掉结论
    assert len(outcome.kept) == 1
    assert route.call_count <= 1


@pytest.mark.asyncio
@respx.mock
async def test_verify_order_prefers_high_severity(settings) -> None:
    """预算有限时先验证"最可能被展示"的那些。"""
    settings.acra_verify_enabled = True
    settings.acra_verify_max_candidates = 1
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))

    respx.post(VERIFY_URL).mock(side_effect=handler)
    client = await _client(settings)
    try:
        candidates = [
            _raw(line=10, severity="nit"),
            _raw(line=11, severity="blocker", confidence=0.9),
        ]
        await verify_candidates(
            candidates,
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10, 11])),
        )
    finally:
        await client.aclose()

    assert len(seen) == 1
    assert "行号: 11" in seen[0]


@pytest.mark.asyncio
@respx.mock
async def test_context_block_is_injected(settings) -> None:
    settings.acra_verify_enabled = True
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))

    respx.post(VERIFY_URL).mock(side_effect=handler)
    client = await _client(settings)
    try:
        await verify_candidates(
            [_raw(line=10)],
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10])),
            contexts={"src/A.java": [({10}, "<<<UNTRUSTED_REPO_CONTENT id=abc>>>\n代码\n<<<END>>>")]},
        )
    finally:
        await client.aclose()

    assert "UNTRUSTED_REPO_CONTENT" in seen[0]


@pytest.mark.asyncio
@respx.mock
async def test_missing_context_falls_back_to_placeholder(settings) -> None:
    settings.acra_verify_enabled = True
    seen: list[str] = []

    def handler(request):
        seen.append(json.loads(request.content)["messages"][-1]["content"])
        return httpx.Response(200, json=_reply({"verdict": "confirmed", "reason": "x"}))

    respx.post(VERIFY_URL).mock(side_effect=handler)
    client = await _client(settings)
    try:
        await verify_candidates(
            [_raw(line=10)], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()
    assert "未提供仓库内容" in seen[0]


@pytest.mark.asyncio
@respx.mock
async def test_uncertain_survives_confidence_threshold(settings) -> None:
    """回归：uncertain 曾经通过降 confidence 实现，结果只要原置信度刚好在门槛之上，
    就会掉到门槛之下被丢弃 —— 那等于把"不确定"当成"丢掉"。

    现在它只降展示优先级（score_penalty），因此仍会进入输出，只是排在后面。
    """
    settings.acra_verify_enabled = True
    settings.acra_confidence_threshold = 0.65
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(200, json=_reply({"verdict": "uncertain", "reason": "看调用方"}))
    )
    client = await _client(settings)
    try:
        candidate = _raw(confidence=0.7)  # 刚好在门槛之上
        outcome = await verify_candidates(
            [candidate], client=client, settings=settings, diff_set=_diff_set(("src/A.java", [10]))
        )
    finally:
        await client.aclose()

    assert len(outcome.kept) == 1
    assert outcome.kept[0].confidence >= settings.acra_confidence_threshold


@pytest.mark.asyncio
@respx.mock
async def test_confirmed_lifts_confidence_over_threshold(settings) -> None:
    """回归：Verify 只回 verdict 不给 confidence 时，候选会沿用 Scan 的低置信度，
    被第 8 步门槛丢掉 —— 等于"二次确认过了还是扔掉"。实测 15 个注入用例里 5 条真阳性
    就是这么丢的。现在 confirmed 会把置信度抬到下限之上。"""
    settings.acra_verify_enabled = True
    settings.acra_confidence_threshold = 0.65
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "confirmed", "reason": "确实会在 null 上崩"})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw(confidence=0.5)],  # 低于门槛
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10])),
        )
    finally:
        await client.aclose()

    kept = outcome.kept[0]
    assert kept.confidence >= settings.acra_confidence_threshold
    assert any("Verify 已确认" in n for n in kept.raw["_adjustments"])


@pytest.mark.asyncio
@respx.mock
async def test_explicit_confidence_wins_over_verified_floor(settings) -> None:
    """模型明确给了 confidence 就听它的 —— 确认了但确实只是小问题，不该被抬成 0.8。"""
    settings.acra_verify_enabled = True
    respx.post(VERIFY_URL).mock(
        return_value=httpx.Response(
            200, json=_reply({"verdict": "confirmed", "reason": "x", "confidence": 0.3})
        )
    )
    client = await _client(settings)
    try:
        outcome = await verify_candidates(
            [_raw(confidence=0.5)],
            client=client,
            settings=settings,
            diff_set=_diff_set(("src/A.java", [10])),
        )
    finally:
        await client.aclose()
    assert outcome.kept[0].confidence == pytest.approx(0.3)




