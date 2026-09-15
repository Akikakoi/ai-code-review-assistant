"""评估层的单测：匹配规则、指标计算、数据集读写、用例物化。

评估工具自己出错是最危险的 —— 它会让所有结论失真且不易察觉。
因此这里的每一条断言都对着一个文档里的**口径**：

- Recall 命中 = 行号 ±3 **且类别相近**（§11.2）
- TP/FP/FN 必须来自逐条匹配，不能用数量估算
- Noise rate 需要平台数据，**不能**用 FP/上报数冒充
- 锚定成功率的分母是候选数，不是上报数
"""

from __future__ import annotations

import json

import pytest

from acra.eval.dataset import (
    EvalCase,
    Expectation,
    builtin_cases,
    load_jsonl,
    materialize,
    save_jsonl,
)
from acra.eval.metrics import (
    RECALL_LINE_TOLERANCE,
    CaseOutcome,
    Reported,
    categories_are_similar,
    compare_baseline,
    compute,
    percentile,
    render_report,
    render_variance,
    variance_between,
)
from acra.eval.runner import DEFAULT_LINE_TOLERANCE, match_case
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set


def _case(*, kind: str = "positive", path: str = "src/A.java") -> EvalCase:
    return EvalCase(
        case_id="c1",
        path=path,
        kind=kind,
        base="line1\nline2\n",
        head="line1\nline2\nbug here\n",
        expectations=[Expectation(marker="bug here", category="bug")],
    )


def _match(case: EvalCase, expected_lines: list[int], reported, tolerance: int = 2):
    return match_case(
        case,
        expectations=case.expectations,
        expected_lines=expected_lines,
        reported=reported,
        tolerance=tolerance,
    )


# ---------------------------------------------------------------------------- 数据集


def test_recall_tolerance_matches_spec() -> None:
    """§11.2：行号 ±3 视为命中。默认容差必须就是这个值。"""
    assert RECALL_LINE_TOLERANCE == 3
    assert DEFAULT_LINE_TOLERANCE == RECALL_LINE_TOLERANCE


def test_expected_lines_locates_marker() -> None:
    case = EvalCase(
        case_id="x",
        path="a.java",
        base="a\n",
        head="a\nb\nTARGET = 1;\nc\n",
        expectations=[Expectation(marker="TARGET = 1;")],
    )
    assert case.expected_lines() == [3]


def test_expected_lines_ignores_missing_marker() -> None:
    case = EvalCase(
        case_id="x", path="a.java", base="", head="a\n", expectations=[Expectation(marker="不存在")]
    )
    assert case.expected_lines() == []


def test_jsonl_round_trip(tmp_path) -> None:
    cases = builtin_cases()
    target = tmp_path / "cases.jsonl"
    assert save_jsonl(cases, target) == len(cases)

    loaded = load_jsonl(target)
    assert [c.case_id for c in loaded] == [c.case_id for c in cases]
    assert loaded[0].expectations[0].marker == cases[0].expectations[0].marker


def test_jsonl_skips_bad_lines(tmp_path) -> None:
    target = tmp_path / "cases.jsonl"
    target.write_text(
        "\n".join(
            [
                "# 注释行",
                "这不是 JSON",
                json.dumps({"case_id": ""}),  # 缺 case_id
                json.dumps({"case_id": "ok", "path": "a.java", "base": "", "head": "x"}),
            ]
        ),
        encoding="utf-8",
    )
    assert [c.case_id for c in load_jsonl(target)] == ["ok"]


def test_builtin_cases_are_well_formed() -> None:
    cases = builtin_cases()
    assert cases
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids)), "case_id 必须唯一"

    for case in cases:
        assert case.base != case.head, f"{case.case_id} 必须有真实变更"
        if case.is_positive:
            assert case.expectations, f"{case.case_id} 是正例却没有 expectations"
            assert case.expected_lines(), (
                f"{case.case_id} 的 marker 在 head 里找不到：{case.expectations[0].marker}"
            )
        else:
            assert not case.expectations


def test_materialize_produces_real_diff(tmp_path) -> None:
    """评估必须跑真实 git 链路，否则指标不代表线上。"""
    case = _case()
    repo, base, head = materialize(case, tmp_path)

    handle = gateway.discover_local(repo)
    merge_base = handle.merge_base(base, head)
    head_sha = handle.resolve_commit(head)
    diff_set = build_diff_set(
        handle.diff(merge_base, head_sha),
        base_sha=base,
        head_sha=head_sha,
        merge_base_sha=merge_base,
    )
    assert len(diff_set.files) == 1
    assert diff_set.files[0].path == "src/A.java"
    assert case.expected_lines()[0] in diff_set.files[0].added_line_numbers


def test_materialize_returns_resolvable_shas(tmp_path) -> None:
    """返回的是 SHA 而不是分支名。

    刻意不用分支：某些环境里 `git checkout -b` 之后引用会"消失"，
    随后的 commit 变成 root commit 甚至静默不生效。评估只需要两个修订版本，
    SHA 指向 commit 对象，只要对象在就一定能解析。
    """
    case = _case()
    repo, base_sha, head_sha = materialize(case, tmp_path)

    assert len(base_sha) == 40 and len(head_sha) == 40
    assert base_sha != head_sha

    handle = gateway.discover_local(repo)
    assert handle.resolve_commit(base_sha) == base_sha
    assert handle.resolve_commit(head_sha) == head_sha


def test_materialize_reuses_directory_without_growing(tmp_path) -> None:
    """重跑同一用例必须**复用**目录，而不是另开一个。

    回归：早期实现是"冲突就换新目录、从不删旧目录"，于是跑 6 轮评估堆出 500 多个
    目录、47MB —— 无界增长。现在改为在既有仓库上追加修订，目录数收敛到用例数。
    """
    case = _case()
    first = materialize(case, tmp_path)
    second = materialize(case, tmp_path)
    third = materialize(case, tmp_path)

    assert first[0] == second[0] == third[0], "同一 case_id 必须复用同一目录"
    assert len(list(tmp_path.iterdir())) == 1, "跑三轮也只能有一个目录"

    # 每次都必须是可用的两个不同修订（复用不等于返回旧 SHA）
    for _repo, base_sha, head_sha in (first, second, third):
        assert base_sha and head_sha and base_sha != head_sha
        handle = gateway.discover_local(first[0])
        assert handle.resolve_commit(base_sha)
        assert handle.resolve_commit(head_sha)


def test_materialize_reuse_keeps_diff_correct(tmp_path) -> None:
    """复用后 diff 仍然只包含注入的那处改动 —— 否则指标会被历史污染。"""
    case = _case()
    _repo, _b, head_first = materialize(case, tmp_path)
    repo, base_second, head_second = materialize(case, tmp_path)

    handle = gateway.discover_local(repo)
    merge_base = handle.merge_base(base_second, head_second)
    diff_set = build_diff_set(
        handle.diff(merge_base, head_second),
        base_sha=base_second,
        head_sha=head_second,
        merge_base_sha=merge_base,
    )
    assert len(diff_set.files) == 1
    assert diff_set.files[0].path == "src/A.java"
    assert case.expected_lines()[0] in diff_set.files[0].added_line_numbers
    assert head_first != head_second, "复用应产生新的修订点"


def test_materialize_moves_aside_when_file_set_differs(tmp_path) -> None:
    """同一 case_id 但文件集合不同（换了数据集）时必须另开目录，不能污染 diff。"""
    case = _case()
    repo, _b, _h = materialize(case, tmp_path)

    changed = EvalCase(
        case_id=case.case_id,
        path="src/Other.java",
        base="x\n",
        head="x\nbug here\n",
        expectations=[Expectation(marker="bug here")],
    )
    other, _b2, _h2 = materialize(changed, tmp_path)

    assert other != repo, "文件集合对不上时应另开目录"
    handle = gateway.discover_local(other)
    assert handle  # 新目录本身可用


# ---------------------------------------------------------------------------- 匹配


def test_match_exact_line() -> None:
    case = _case()
    result = _match(case, [3], [("src/A.java", 3, "bug")], tolerance=0)
    assert [r.as_tuple() for r in result.matched] == [("src/A.java", 3, "bug")]
    assert result.spurious == []
    assert result.missed == []


def test_match_within_spec_tolerance() -> None:
    """±3 内算命中，±4 不算。"""
    case = _case()
    assert _match(case, [6], [("src/A.java", 9, "bug")], tolerance=3).matched
    assert _match(case, [6], [("src/A.java", 10, "bug")], tolerance=3).spurious


def test_match_outside_tolerance_is_spurious_and_missed() -> None:
    case = _case()
    result = _match(case, [6], [("src/A.java", 20, "bug")], tolerance=3)
    assert result.matched == []
    assert len(result.spurious) == 1
    assert result.missed == [6]


def test_match_finding_on_other_file_is_not_counted() -> None:
    case = _case()
    result = _match(case, [3], [("src/B.java", 3, "bug")], tolerance=3)
    assert result.matched == []
    assert result.spurious == []
    assert result.missed == [3]


def test_negative_case_any_finding_is_false_positive() -> None:
    case = _case(kind="negative")
    result = _match(case, [], [("src/A.java", 3, "style")], tolerance=3)
    assert result.matched == []
    assert len(result.spurious) == 1


def test_each_expectation_matches_at_most_once() -> None:
    case = EvalCase(
        case_id="multi",
        path="src/A.java",
        base="a\n",
        head="a\nBUG1\nBUG2\n",
        expectations=[Expectation(marker="BUG1"), Expectation(marker="BUG2")],
    )
    result = _match(
        case,
        [2, 3],
        [("src/A.java", 2, "bug"), ("src/A.java", 2, "bug"), ("src/A.java", 2, "bug")],
        tolerance=0,
    )
    assert len(result.matched) == 1
    assert len(result.spurious) == 2
    assert result.missed == [3]


def test_category_mismatch_is_not_a_hit() -> None:
    """§11.2 要求"类别相近"才算命中：位置对了但把正确性问题说成风格问题，
    作者看到的东西完全不一样。该期望行因此计漏报，这条上报计误报。"""
    case = _case()  # 期望 category=bug
    result = _match(case, [3], [("src/A.java", 3, "style")], tolerance=0)
    assert result.matched == []
    assert result.category_mismatches == 1
    assert result.missed == [3]
    assert len(result.spurious) == 1


def test_same_family_category_counts_as_hit() -> None:
    """bug / security / concurrency 同属正确性族，视为相近。"""
    case = EvalCase(
        case_id="c",
        path="src/A.java",
        base="a\n",
        head="a\nBUG\n",
        expectations=[Expectation(marker="BUG", category="security")],
    )
    result = _match(case, [2], [("src/A.java", 2, "concurrency")], tolerance=0)
    assert len(result.matched) == 1
    assert result.category_mismatches == 0


def test_cross_family_categories_are_not_similar() -> None:
    assert categories_are_similar("security", "bug") is True
    assert categories_are_similar("performance", "maintainability") is True
    assert categories_are_similar("bug", "style") is False
    assert categories_are_similar("test", "bug") is False
    # 任一方缺失时不做判断
    assert categories_are_similar("", "style") is True
    assert categories_are_similar("bug", "") is True


def test_expectation_without_category_accepts_any() -> None:
    case = EvalCase(
        case_id="c",
        path="src/A.java",
        base="a\n",
        head="a\nBUG\n",
        expectations=[Expectation(marker="BUG")],  # 没写 category
    )
    assert len(_match(case, [2], [("src/A.java", 2, "style")], tolerance=0).matched) == 1


# ---------------------------------------------------------------------------- 指标


def _outcome(
    case_id: str,
    *,
    expected: bool = True,
    matched=0,
    spurious=0,
    missed=0,
    **kw,
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        expected=expected,
        matched=[Reported("a", i, "bug") for i in range(matched)],
        spurious=[Reported("a", 90 + i, "style") for i in range(spurious)],
        missed=list(range(missed)),
        **kw,
    )


def test_metrics_precision_recall_fp_rate() -> None:
    metrics = compute(
        [
            _outcome("p1", matched=1),
            _outcome("p2", matched=1, spurious=1),
            _outcome("p3", missed=1),
            _outcome("n1", expected=False, spurious=2),
        ]
    )
    assert metrics.true_positives == 2
    assert metrics.false_positives == 3
    assert metrics.false_negatives == 1
    assert metrics.reported_total == 5
    assert metrics.precision == pytest.approx(2 / 5)
    assert metrics.recall == pytest.approx(2 / 3)
    assert metrics.false_positive_rate == pytest.approx(3 / 5)


def test_tp_is_not_estimated_from_counts() -> None:
    """回归：早期实现用 min(期望数, 上报数) 当 TP，
    于是"3 条期望 + 3 条全打偏的上报"会被算成 3 个 TP。"""
    outcome = _outcome("p", matched=0, spurious=3, missed=3)
    metrics = compute([outcome])
    assert metrics.true_positives == 0
    assert metrics.false_positives == 3
    assert metrics.false_negatives == 3
    assert metrics.precision == 0.0


def test_noise_rate_is_not_faked() -> None:
    """Noise rate 需要平台交互数据，不能用 FP/上报数冒充（Precision=1 时会假装达标）。"""
    metrics = compute([_outcome("p", matched=1)])
    data = metrics.to_dict()
    assert data["noise_rate"] is None
    assert "noise_rate" not in metrics.targets()
    assert "平台" in data["noise_rate_note"]
    text = render_report(metrics)
    assert "不可计算" in text


def test_anchor_validity_uses_candidate_denominator() -> None:
    metrics = compute(
        [
            _outcome("a", matched=1, raw_candidates=4, anchor_dropped=1),
            _outcome("b", matched=1, raw_candidates=6, anchor_dropped=0),
        ]
    )
    assert metrics.anchor_candidates == 10
    assert metrics.anchor_dropped == 1
    assert metrics.anchor_validity == pytest.approx(9 / 10)


def test_anchor_validity_defaults_to_one_without_candidates() -> None:
    assert compute([_outcome("a")]).anchor_validity == 1.0


def test_p95_latency() -> None:
    metrics = compute(
        [_outcome(f"c{i}", duration_ms=ms) for i, ms in enumerate([100, 200, 300, 400, 10_000])]
    )
    assert metrics.p95_latency_ms > 400
    assert "p95_latency_ms" in metrics.targets()


def test_p95_target_is_150_seconds() -> None:
    """§11.2 的 p95 目标是 150 秒。"""
    from acra.eval import P95_LATENCY_TARGET

    assert P95_LATENCY_TARGET == 150_000


def test_percentile_interpolates() -> None:
    assert percentile([], 0.95) == 0
    assert percentile([5], 0.95) == 5
    assert percentile([1, 2, 3, 4], 0.5) == 2 or percentile([1, 2, 3, 4], 0.5) == 3


def test_targets_report_met_or_not() -> None:
    metrics = compute([_outcome("p", matched=1), _outcome("p2", matched=1)])
    assert metrics.precision == pytest.approx(1.0)
    assert metrics.targets()["precision"][1] is True
    assert metrics.to_dict()["targets"]["precision"]["target"] == pytest.approx(0.60)


def test_failed_cases_are_counted() -> None:
    assert compute([_outcome("x", error="boom")]).failed_cases == 1


def test_category_breakdown_and_top_fp_paths() -> None:
    outcome = CaseOutcome(
        case_id="c",
        expected=True,
        matched=[Reported("a.java", 1, "security")],
        spurious=[Reported("b.java", 2, "style"), Reported("b.java", 3, "style")],
    )
    metrics = compute([outcome])
    assert metrics.by_category["security"] == (1, 0)
    assert metrics.by_category["style"] == (0, 2)
    assert metrics.by_category_precision()["security"] == 1.0
    assert metrics.top_false_positive_paths()[0] == ("b.java", 2)


def test_cost_is_aggregated() -> None:
    metrics = compute(
        [
            _outcome("a", usage={"calls": 2, "prompt_tokens": 100, "completion_tokens": 50, "cost_micros": 1000}),
            _outcome("b", usage={"calls": 1, "prompt_tokens": 60, "completion_tokens": 20, "cost_micros": 500}),
        ]
    )
    assert metrics.total_calls == 3
    assert metrics.prompt_tokens == 160
    assert metrics.cost_per_case_micros == 750


def test_verify_rejection_rate_includes_zeros() -> None:
    """只平均非零值会把"验证全过"的用例悄悄排除，让驳回率看起来永远很高。"""
    metrics = compute(
        [
            _outcome("a", verify_enabled=True, verify_rejection_rate=1.0),
            _outcome("b", verify_enabled=True, verify_rejection_rate=0.0),
            _outcome("c", verify_enabled=True, verify_rejection_rate=0.0),
            # 未启用 Verify 的用例不参与统计
            _outcome("d", verify_enabled=False, verify_rejection_rate=0.0),
        ]
    )
    assert metrics.verify_rejection_rate == pytest.approx(1 / 3)


def test_verify_rejection_rate_zero_when_never_enabled() -> None:
    assert compute([_outcome("a")]).verify_rejection_rate == 0.0


def test_anchor_totals_are_exposed() -> None:
    """必须同时给出绝对数：11 个候选里丢 1 个就是 9 个百分点，
    只报比率会让 0.98 这种精度在小样本上显得可判定。"""
    metrics = compute(
        [
            _outcome("a", raw_candidates=11, anchor_dropped=1),
            _outcome("b", raw_candidates=0, anchor_dropped=0),
        ]
    )
    assert metrics.anchor_candidates == 11
    assert metrics.anchor_dropped == 1
    assert metrics.anchor_validity == pytest.approx(10 / 11)


def test_small_sample_is_flagged_in_report() -> None:
    metrics = compute([_outcome("a", raw_candidates=12, anchor_dropped=0)])
    assert metrics.sample_adequate is False
    text = render_report(metrics)
    assert "样本量不足" in text
    assert "12/12" in text


def test_large_sample_is_adequate() -> None:
    metrics = compute([_outcome(f"c{i}", raw_candidates=20) for i in range(6)])
    assert metrics.anchor_candidates == 120
    assert metrics.sample_adequate is True
    assert "样本量不足" not in render_report(metrics)


# ---------------------------------------------------------------------------- 基线对比与渲染


def test_compare_baseline_flags_precision_regression() -> None:
    # 2 个正例：命中 1 个、漏 1 个 → recall = 0.5；再加一个误报压低 precision
    metrics = compute([_outcome("p", matched=1, missed=1), _outcome("n", expected=False, spurious=1)])
    notes = compare_baseline(metrics, {"precision": 0.95, "recall": 0.9})
    assert any("不允许合并" in n for n in notes), notes
    assert any("Recall 下降" in n for n in notes), notes


def test_compare_baseline_silent_on_small_change() -> None:
    metrics = compute([_outcome("p", matched=2)])
    assert metrics.recall == pytest.approx(1.0)
    assert compare_baseline(metrics, {"precision": 1.0, "recall": 1.0}) == []


def test_render_report_lists_unmet_targets_and_repeat_spread() -> None:
    metrics = compute([_outcome("p", missed=1)])
    text = render_report(metrics, repeat_spread={"precision": 0.03})
    assert "未达标" in text
    assert "Recall" in text
    assert "跑批波动" in text
    assert "不能归因于改动" in text


def test_render_report_lists_missed_cases_and_fp_paths() -> None:
    metrics = compute(
        [
            _outcome("missed-one", missed=1),
            CaseOutcome(case_id="fp", expected=False, spurious=[Reported("x.java", 1, "style")]),
        ]
    )
    text = render_report(metrics)
    assert "漏报用例" in text
    assert "missed-one" in text
    assert "x.java" in text


# ---------------------------------------------------------------------------- 跑批波动


def _report(case_hits: dict[str, int], precision: float, recall: float) -> dict:
    return {
        "metrics": {"precision": precision, "recall": recall, "anchor_validity": 1.0},
        "cases": [{"case_id": cid, "tp": tp} for cid, tp in case_hits.items()],
    }


def test_variance_detects_unstable_hits() -> None:
    """回归：同配置两次运行命中集合互相替换、聚合指标却相同 —— 必须能看出来。"""
    a = _report({"x": 1, "y": 0, "z": 1}, 1.0, 0.667)
    b = _report({"x": 0, "y": 1, "z": 1}, 1.0, 0.667)
    result = variance_between(a, b)

    assert result.only_in_first == ["x"]
    assert result.only_in_second == ["y"]
    assert result.stable_hits == ["z"]
    assert result.unstable_cases == 2
    # 聚合指标完全相同 —— 因此单看指标发现不了问题
    assert result.max_metric_spread == pytest.approx(0.0)

    text = render_variance(result)
    assert "命中不稳定" in text
    assert "不能归因于改动" in text


def test_variance_reports_metric_spread() -> None:
    result = variance_between(
        _report({}, 0.9, 0.5), _report({}, 0.6, 0.55), labels=("第一次", "第二次")
    )
    assert result.metric_deltas["precision"] == pytest.approx((0.9, 0.6, -0.3))
    assert result.max_metric_spread == pytest.approx(0.3)
    assert "第一次" in render_variance(result)


def test_variance_accepts_metrics_only_payload() -> None:
    """允许直接传 metrics 字典（没有 cases 时不报错）。"""
    result = variance_between({"precision": 1.0}, {"precision": 0.5})
    assert result.metric_deltas["precision"][2] == pytest.approx(-0.5)
    assert result.stable_hits == []


# ---------------------------------------------------------------------------- 阈值扫描


def _sweep_case(case_id: str = "c1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        path="src/A.java",
        base="a\n",
        head="a\nBUG\n",
        expectations=[Expectation(marker="BUG", category="bug")],
    )


def _pool(entries: list[dict]) -> list[dict]:
    return [
        {
            "path": "src/A.java",
            "line": 2,
            "category": "bug",
            "severity": "high",
            "confidence": e["confidence"],
            "score": e.get("score", e["confidence"]),
            **{k: v for k, v in e.items() if k not in ("confidence", "score")},
        }
        for e in entries
    ]


def test_sweep_shows_recall_precision_tradeoff() -> None:
    """阈值调低应换来 Recall、付出 Precision —— 这是扫阈值要回答的问题。"""
    from acra.eval.sweep import sweep

    cases = [_sweep_case("hit"), _sweep_case("miss")]
    outcomes = [
        # 命中的用例：候选置信度 0.5，低于 0.65 门槛
        CaseOutcome(
            case_id="hit",
            expected=True,
            expected_lines=[2],
            raw_candidates=1,
            candidate_pool=_pool([{"confidence": 0.5}]),
        ),
        # 未命中的用例：候选置信度 0.2，且打在了别的行上（低质量候选）
        CaseOutcome(
            case_id="miss",
            expected=True,
            expected_lines=[2],
            raw_candidates=1,
            candidate_pool=[
                {
                    "path": "src/A.java",
                    "line": 99,
                    "category": "style",
                    "severity": "low",
                    "confidence": 0.2,
                    "score": 0.2,
                }
            ],
        ),
    ]
    report = sweep(cases, outcomes, thresholds=(0.65, 0.50, 0.20))
    by_threshold = {row.threshold: row.metrics for row in report.rows}

    # 阈值 0.65：两条候选都被门槛挡掉
    assert by_threshold[0.65].true_positives == 0
    assert by_threshold[0.65].recall == 0.0
    # 阈值降到 0.50：命中用例的候选进来了
    assert by_threshold[0.50].true_positives == 1
    assert by_threshold[0.50].recall > 0
    # 阈值降到 0.20：0.2 那条低质量候选也进来，Precision 被拉低
    assert by_threshold[0.20].false_positives >= 1
    assert by_threshold[0.20].precision < by_threshold[0.50].precision


def test_sweep_best_by_requires_recall_floor() -> None:
    """只挑最大 Precision 没意义：阈值拉满时一条都不报，Precision 反而最高。"""
    from acra.eval.sweep import sweep

    cases = [_sweep_case("hit")]
    outcomes = [
        CaseOutcome(
            case_id="hit",
            expected=True,
            expected_lines=[2],
            raw_candidates=1,
            candidate_pool=_pool([{"confidence": 0.7}]),
        )
    ]
    report = sweep(cases, outcomes)
    assert report.best_by("precision", minimum_recall=0.5) is not None
    assert report.best_by("precision", minimum_recall=1.1) is None


def test_sweep_respects_max_comments() -> None:
    from acra.eval.sweep import sweep

    cases = [_sweep_case("hit")]
    outcomes = [
        CaseOutcome(
            case_id="hit",
            expected=True,
            expected_lines=[2],
            raw_candidates=3,
            candidate_pool=_pool(
                [
                    {"confidence": 0.9, "score": 0.9},
                    {"confidence": 0.8, "score": 0.8, "line": 3},
                    {"confidence": 0.7, "score": 0.7, "line": 4},
                ]
            ),
        )
    ]
    report = sweep(cases, outcomes, thresholds=(0.5,), max_comments=1)
    assert report.rows[0].metrics.reported_total == 1


def test_sweep_reports_pool_size_and_skips_failed_cases() -> None:
    from acra.eval.sweep import sweep

    cases = [_sweep_case("ok"), _sweep_case("bad")]
    outcomes = [
        CaseOutcome(case_id="ok", expected=True, expected_lines=[2], candidate_pool=_pool([{"confidence": 0.9}])),
        CaseOutcome(case_id="bad", expected=True, error="boom"),
    ]
    report = sweep(cases, outcomes, thresholds=(0.5,))
    assert report.pool_size == 1
    assert report.cases_with_pool == 1
    assert report.rows[0].metrics.failed_cases == 1


def test_render_sweep_marks_current_threshold() -> None:
    from acra.eval.sweep import SweepRow, render_sweep, sweep

    report = sweep([_sweep_case("hit")], [], thresholds=(0.65,))
    report.rows = [SweepRow(threshold=0.65, metrics=compute([]))]
    text = render_sweep(report, current_threshold=0.65)
    assert "← 当前" in text
    assert "Recall 下限" in text
