"""评分、分级、排序、截断、去重与候选合并的单测。

文档 §14.1：「评分各权重项、去重窗口边界、截断规则」。
"""

from __future__ import annotations

import pytest

from acra.engine.merge import merge_candidates
from acra.models import Finding, RawFinding, ReviewContext
from acra.postprocess.dedupe import dedupe_findings
from acra.postprocess.ranker import (
    EVIDENCE_BONUS,
    HUMAN_JUDGMENT_PENALTY,
    RISK_CATEGORY_BONUS,
    STYLE_PENALTY,
    assign_scores,
    order_and_truncate,
    score_finding,
)


def _finding(**overrides) -> Finding:
    payload = {
        "path": "src/A.java",
        "line": 10,
        "category": "bug",
        "severity": "medium",
        "confidence": 0.5,
        "score": 0.0,
        "title": "标题",
        "body": "现象→触发条件→影响→修复建议",
    }
    payload.update(overrides)
    return Finding(**payload)


# ---------------------------------------------------------------------------- 评分


def test_score_is_confidence_when_no_modifiers() -> None:
    f = _finding(severity="low", confidence=0.5)
    assert score_finding(f) == 0.5


def test_severity_boost_weights() -> None:
    assert score_finding(_finding(severity="blocker", confidence=0.5)) == 0.8
    assert score_finding(_finding(severity="high", confidence=0.5)) == 0.7
    assert score_finding(_finding(severity="medium", confidence=0.5)) == 0.6
    assert score_finding(_finding(severity="low", confidence=0.5)) == 0.5


def test_evidence_bonus() -> None:
    f = _finding(severity="low", confidence=0.5, evidence_rule_ids=["java.sqli"])
    assert score_finding(f) == 0.5 + EVIDENCE_BONUS


def test_risk_category_bonus() -> None:
    f = _finding(severity="low", confidence=0.5, category="security")
    assert score_finding(f, risk_categories={"security"}) == 0.5 + RISK_CATEGORY_BONUS


def test_human_judgment_penalty() -> None:
    f = _finding(severity="low", confidence=0.5, needs_human_judgment=True)
    assert score_finding(f) == 0.5 - HUMAN_JUDGMENT_PENALTY


def test_style_penalty() -> None:
    f = _finding(severity="low", confidence=0.5, category="style")
    assert score_finding(f) == 0.5 - STYLE_PENALTY


def test_duplicate_penalty_accumulates() -> None:
    f = _finding(severity="low", confidence=0.5)
    assert score_finding(f, dup_count=2) == 0.5 - 0.2


def test_score_is_clamped_to_unit_range() -> None:
    assert score_finding(_finding(severity="blocker", confidence=1.0)) == 1.0
    assert score_finding(_finding(severity="low", confidence=0.05, category="style")) == 0.0


def test_assign_scores_derives_dup_count_from_same_file_and_category() -> None:
    """同文件同类别的重复问题会互相降权（§9.2 的 -0.10 × dup_count）。"""
    a = _finding(line=10, category="bug", confidence=0.5)
    b = _finding(line=50, category="bug", confidence=0.5)
    c = _finding(line=60, category="style", confidence=0.5)

    assign_scores([a, b, c], ReviewContext(head_sha="x"))
    # medium(0.10) + 0.5 = 0.6，再扣掉同文件同类别的 1 条重复 → 0.5
    assert a.score == pytest.approx(0.5)
    assert b.score == pytest.approx(0.5)
    # style(medium 0.10，再扣 style 0.20) + 0.5 = 0.4，且只有自己一条
    assert c.score == pytest.approx(0.4)


def test_assign_scores_no_penalty_for_unique_file() -> None:
    f = _finding(path="only.java", category="bug", confidence=0.5)
    assign_scores([f], ReviewContext(head_sha="x"))
    assert f.score == pytest.approx(0.6)


# ---------------------------------------------------------------------------- 排序与截断


def test_sort_key_is_score_then_severity_then_line() -> None:
    low_score = _finding(line=1, severity="blocker", confidence=0.1, score=0.4)
    same_score_high = _finding(line=9, severity="high", confidence=0.2, score=0.6)
    same_score_blocker = _finding(line=20, severity="blocker", confidence=0.3, score=0.6)
    ordered = sorted([low_score, same_score_high, same_score_blocker], key=lambda f: f.sort_key())
    assert [f.line for f in ordered] == [20, 9, 1]


def test_truncates_to_max_comments() -> None:
    # 注意：同一文件同一类别会互相降权（§9.2），所以这里刻意分散到不同文件，
    # 单独验证"截断至 max_comments"这条规则本身。
    findings = [
        _finding(path=f"a{i}.java", line=i, confidence=0.9, title=f"t{i}") for i in range(1, 9)
    ]
    result = order_and_truncate(findings, max_comments=3, per_file_max=10)
    assert len(result.line_comments) == 3
    assert len(result.summary_only) == 5
    assert result.hidden == []


def test_per_file_cap_pushes_extra_into_summary() -> None:
    findings = [_finding(path="a.java", line=i, confidence=0.9) for i in range(1, 6)]
    result = order_and_truncate(findings, max_comments=10, per_file_max=2)
    assert len(result.line_comments) == 2
    assert len(result.summary_only) == 3


def test_below_summary_threshold_is_hidden_not_reported() -> None:
    weak = _finding(category="style", severity="nit", confidence=0.1)
    strong = _finding(line=20, severity="high", confidence=0.9)
    result = order_and_truncate([weak, strong], max_comments=5)
    assert [f.line for f in result.line_comments] == [20]
    assert [f.line for f in result.hidden] == [10]
    assert result.summary_only == []


def test_counts_by_severity_only_covers_reported() -> None:
    result = order_and_truncate(
        [
            _finding(line=1, severity="high", confidence=0.9),
            _finding(line=2, severity="high", confidence=0.9),
            _finding(line=3, severity="low", confidence=0.9),
        ],
        max_comments=5,
    )
    assert result.counts_by_severity() == {"high": 2, "low": 1}


# ---------------------------------------------------------------------------- 去重


def test_dedupe_merges_within_window_and_keeps_highest_score() -> None:
    a = _finding(line=30, score=0.9, title="A")
    b = _finding(line=32, score=0.5, title="B")  # 距离 2 ≤ 3
    kept, dropped = dedupe_findings([a, b])
    assert len(kept) == 1
    assert kept[0].title == "A"
    assert kept[0].merged_sources == 2
    assert dropped[0].step == "6_duplicate"


def test_dedupe_keeps_findings_outside_window() -> None:
    a = _finding(line=30, score=0.9)
    b = _finding(line=34, score=0.5)  # 距离 4 > 3
    kept, dropped = dedupe_findings([a, b])
    assert len(kept) == 2
    assert dropped == []


def test_dedupe_does_not_cross_category_boundary_in_same_line() -> None:
    a = _finding(line=30, category="bug", score=0.9)
    b = _finding(line=30, category="security", score=0.9)
    kept, _ = dedupe_findings([a, b])
    assert len(kept) == 2


# ---------------------------------------------------------------------------- 候选合并


def _raw(line: int, confidence: float, title: str, **kw) -> RawFinding:
    payload = {
        "path": "src/A.java",
        "line": line,
        "category": "bug",
        "severity": "medium",
        "confidence": confidence,
        "title": title,
        "body": f"{title} 的现象与影响",
    }
    payload.update(kw)
    return RawFinding.from_dict(payload)


def test_merge_keeps_higher_confidence_and_appends_evidence() -> None:
    merged = merge_candidates([_raw(10, 0.5, "较弱"), _raw(11, 0.9, "较强")])
    assert len(merged) == 1
    assert merged[0].title == "较强"
    assert "补充证据：" in merged[0].body
    assert merged[0].raw["_merged_sources"] == 2


def test_merge_respects_window_boundary() -> None:
    assert len(merge_candidates([_raw(10, 0.5, "a"), _raw(13, 0.5, "b")])) == 1
    assert len(merge_candidates([_raw(10, 0.5, "a"), _raw(14, 0.5, "b")])) == 2


def test_merge_takes_higher_severity() -> None:
    merged = merge_candidates(
        [_raw(10, 0.5, "a", severity="low"), _raw(10, 0.9, "b", severity="blocker")]
    )
    assert merged[0].severity == "blocker"


def test_merge_empty_input() -> None:
    assert merge_candidates([]) == []
