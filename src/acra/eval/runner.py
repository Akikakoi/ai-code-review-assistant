"""评估执行器：把数据集跑一遍，产出可比较的指标。

对应开发文档 §11.1 / §11.4。

设计要点：

- **用真实 git 仓库跑完整链路**（`dataset.materialize`），而不是往 pipeline 里塞假 DiffSet。
  评估的意义就是量"线上会怎样"，绕过 diff 解析 / 行号映射 / 上下文构建去测，
  得到的数字不能代表线上。
- **匹配容差**：同一处问题模型可能锚到相邻行（尤其是多行改动），
  因此以期望行为中心 ±tolerance 视为命中。容差写进报告，避免读者误判口径。
- **反例按"该路径上报任何结论"判定**：反例的期望是"没有问题"，
  只要在该文件上报了结论就是误报。
- **可注入 runner / llm_client**：离线（`--no-llm` 静态基线）与真实模型共用同一条执行路径，
  这样基线对比才有意义。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from acra.eval.dataset import EvalCase, Expectation, materialize
from acra.eval.metrics import (
    RECALL_LINE_TOLERANCE,
    CaseOutcome,
    EvalMetrics,
    Reported,
    categories_are_similar,
    compute,
)
from acra.orchestrator.pipeline import ReviewOptions, run_review
from acra.trigger.normalize import job_from_cli

#: 行号匹配容差。文档 §11.2 的 Recall 口径是"行号 ±3 且类别相近视为命中"。
DEFAULT_LINE_TOLERANCE = RECALL_LINE_TOLERANCE


@dataclass(slots=True)
class MatchResult:
    """一个用例的匹配结果。

    `matched` / `spurious` / `missed` 是**逐条判定出来的**，指标层只做汇总。
    早期实现让指标层用 `min(期望数, 上报数)` 反推 TP，在"上报打偏了"时会虚高：
    3 条期望 + 3 条全打偏的上报会被算成 3 个 TP，实际是 3 FP + 3 FN。
    """

    matched: list[Reported] = field(default_factory=list)
    spurious: list[Reported] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    category_mismatches: int = 0


def match_case(
    case: EvalCase,
    *,
    expectations: list[Expectation],
    expected_lines: list[int],
    reported: list[tuple[str, int, str]],
    tolerance: int,
) -> MatchResult:
    """按文档 §11.2 的口径判定命中 / 误报 / 漏报。

    - 正例：行号落在某个期望行 ±tolerance 内、且类别与期望**同族**，才算命中；
      行号命中但类别跨族**不计命中**（该期望行仍算漏报），这条上报计误报，
      并单独计入 `category_mismatches`（用于判断这个口径是否过严）；
      其余上报算误报，没被命中的期望行算漏报。
    - 反例：该文件上的任何上报都是误报。
    - 别的文件上的上报既不算命中也不算误报（本用例只关心自己的文件）。
    """
    result = MatchResult()
    same_file = [item for item in reported if item[0] == case.path]

    if not case.is_positive:
        result.spurious = [Reported(*item) for item in same_file]
        return result

    used_expected: set[int] = set()
    for path, line, category in same_file:
        index = next(
            (
                i
                for i, expected in enumerate(expected_lines)
                if i not in used_expected and abs(expected - line) <= tolerance
            ),
            None,
        )
        if index is None:
            result.spurious.append(Reported(path, line, category))
            continue

        expected = expectations[index] if index < len(expectations) else None
        wanted = expected.category if expected is not None else ""
        if wanted and not categories_are_similar(wanted, category):
            # 位置对了但性质判断错族：作者看到的是"这是个风格问题"，而实际是正确性问题，
            # 严重度与处理方式都不一样。按 §11.2 字面口径这**不算命中**（→ 该期望行仍计漏报），
            # 同时这条上报本身计误报。两处惩罚是有意为之：它衡量的是"有没有把问题讲对"，
            # 不只是"有没有指到那一行"。effect 通过 category_mismatches 单独暴露，
            # 便于判断这个口径是否过于严苛（若占比高，应先改提示词的类别引导，而不是放宽口径）。
            result.category_mismatches += 1
            result.spurious.append(Reported(path, line, category))
            continue

        used_expected.add(index)
        result.matched.append(Reported(path, line, category))

    result.missed = [line for i, line in enumerate(expected_lines) if i not in used_expected]
    return result


@dataclass(slots=True)
class EvalRunConfig:
    workspace: Path
    level: int | None = None
    no_llm: bool = False
    tolerance: int = DEFAULT_LINE_TOLERANCE
    limit: int | None = None
    keep_repos: bool = False
    concurrency: int = 1
    extra: dict = field(default_factory=dict)


async def run_case(
    case: EvalCase,
    settings,
    config: EvalRunConfig,
    *,
    llm_client=None,
    db=None,
    cache=None,
    static_runner=None,
) -> CaseOutcome:
    expected_lines = case.expected_lines()
    outcome = CaseOutcome(
        case_id=case.case_id,
        expected=case.is_positive,
        expected_lines=expected_lines,
        source=case.source or "unknown",
    )

    try:
        repo, base, head = await asyncio.to_thread(materialize, case, config.workspace)
    except Exception as exc:  # noqa: BLE001 - 单个用例失败不该中断整轮评估
        outcome.error = f"materialize:{type(exc).__name__}: {exc}"
        return outcome

    job = job_from_cli(
        repo_path=str(repo),
        base_ref=base,
        head_ref=head,
        repo_full_name=f"eval/{case.case_id}",
    )
    started = time.monotonic()
    try:
        review = await run_review(
            job,
            settings,
            options=ReviewOptions(no_llm=config.no_llm, level=config.level, dry_run=True),
            db=db,
            cache=cache,
            llm_client=llm_client,
            static_runner=static_runner,
        )
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"review:{type(exc).__name__}: {exc}"
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    outcome.duration_ms = review.duration_ms
    if review.error:
        outcome.error = review.error
        return outcome

    reported = [(f.path, f.line, f.category) for f in review.reported]
    match = match_case(
        case,
        expectations=case.expectations,
        expected_lines=expected_lines,
        reported=reported,
        tolerance=config.tolerance,
    )
    outcome.matched = match.matched
    outcome.spurious = match.spurious
    outcome.missed = match.missed
    outcome.category_mismatches = match.category_mismatches
    outcome.raw_candidates = review.raw_count
    outcome.anchor_dropped = sum(1 for d in review.dropped if d.step == "2_anchor")
    outcome.verify_rejection_rate = review.verify_rejection_rate
    outcome.verify_enabled = any(
        (record.raw.raw.get("_verify_verdict") if record.raw is not None else None)
        for record in review.dropped
    ) or any(f.verify_verdict for f in review.reported)
    outcome.usage = {
        "calls": review.usage.calls,
        "prompt_tokens": review.usage.prompt_tokens,
        "completion_tokens": review.usage.completion_tokens,
        "cost_micros": review.usage.cost_micros,
    }
    outcome.candidate_pool = list(review.candidate_pool)
    outcome.dropped = [
        (
            record.step,
            record.reason,
            str(getattr(getattr(record, "raw", None), "raw", {}).get("_verify_verdict") or ""),
        )
        for record in review.dropped
    ]
    return outcome


async def run_dataset(
    cases: list[EvalCase],
    settings,
    config: EvalRunConfig,
    *,
    llm_client=None,
    db=None,
    cache=None,
    static_runner=None,
    progress=None,
) -> tuple[EvalMetrics, list[CaseOutcome]]:
    selected = cases[: config.limit] if config.limit else list(cases)
    outcomes: list[CaseOutcome] = []

    for index, case in enumerate(selected, start=1):
        outcome = await run_case(
            case,
            settings,
            config,
            llm_client=llm_client,
            db=db,
            cache=cache,
            static_runner=static_runner,
        )
        outcomes.append(outcome)
        if progress is not None:
            progress(index, len(selected), outcome)

    return compute(outcomes), outcomes


def outcomes_to_json(metrics: EvalMetrics, outcomes: list[CaseOutcome], *, notes=None) -> str:
    return json.dumps(
        {
            "metrics": metrics.to_dict(),
            "notes": notes or [],
            "cases": [
                {
                    "case_id": o.case_id,
                    "expected": o.expected,
                    "source": o.source,
                    "expected_lines": o.expected_lines,
                    "matched": [r.as_tuple() for r in o.matched],
                    "spurious": [r.as_tuple() for r in o.spurious],
                    "missed": o.missed,
                    "category_mismatches": o.category_mismatches,
                    "tp": o.true_positives,
                    "fp": o.false_positives,
                    "fn": o.false_negatives,
                    "raw_candidates": o.raw_candidates,
                    "anchor_dropped": o.anchor_dropped,
                    "verify_enabled": o.verify_enabled,
                    "verify_rejection_rate": o.verify_rejection_rate,
                    "duration_ms": o.duration_ms,
                    "usage": o.usage,
                    "error": o.error,
                    "dropped": [
                        {"step": s, "reason": r, "verify_verdict": v} for s, r, v in o.dropped
                    ],
                    # 过门槛之前的完整候选池：离线扫阈值（sweep-threshold）的唯一依据
                    "candidate_pool": o.candidate_pool,
                }
                for o in outcomes
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
