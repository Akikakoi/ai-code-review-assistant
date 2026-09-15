"""validator 的单测。

文档 §14.1：「每种丢弃原因各一组用例；构造"行号不在白名单"的幻觉样本」。

同时验证一处显式的工程化偏离：`_merged_sources` 等私有键会被投影剔除，
不因为 `additionalProperties: false` 而把合并过的好结论打死。
"""

from __future__ import annotations

import pytest

from acra.models import DiffSet, RawFinding
from acra.postprocess.validator import (
    apply_threshold,
    check_category_severity,
    check_existing_code,
    cross_validate_findings,
    normalize_enums,
    normalize_path,
    validate_candidates,
    validate_schema,
)
from acra.repo.diff_parser import parse_unified_diff

DIFF = """diff --git a/src/A.java b/src/A.java
index 1..2 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,0 +11,2 @@
+int changed = 1;
+int other = 2;
diff --git a/src/B.java b/src/B.java
index 3..4 100644
--- a/src/B.java
+++ b/src/B.java
@@ -1,0 +2,1 @@
+int b = 1;
"""


def _diff_set() -> DiffSet:
    return DiffSet(
        base_sha="b" * 40,
        head_sha="h" * 40,
        merge_base_sha="m" * 40,
        files=parse_unified_diff(DIFF),
    )


def _raw(**overrides) -> RawFinding:
    payload = {
        "path": "src/A.java",
        "line": 11,
        "category": "bug",
        "severity": "high",
        "confidence": 0.8,
        "title": "空值校验被取反",
        "body": "现象：userId 为 null 时直接抛 NPE。触发条件：调用方传入 null。影响：支付接口 500。修复建议：改回 || 短路。",
    }
    payload.update(overrides)
    return RawFinding.from_dict(payload)


# ---------------------------------------------------------------------------- Schema


def test_valid_finding_passes_schema() -> None:
    ok, reason = validate_schema(_raw())
    assert ok, reason


def test_missing_required_field_is_dropped() -> None:
    raw = _raw()
    raw.body = ""
    ok, reason = validate_schema(raw)
    assert not ok
    assert "body" in reason


def test_illegal_severity_enum_is_dropped() -> None:
    ok, reason = validate_schema(_raw(severity="catastrophic"))
    assert not ok
    assert "severity" in reason


def test_illegal_category_enum_is_dropped() -> None:
    ok, _ = validate_schema(_raw(category="vibes"))
    assert not ok


def test_confidence_out_of_range_is_dropped() -> None:
    ok, _ = validate_schema(_raw(confidence=1.7))
    assert not ok


def test_title_over_length_limit_is_dropped() -> None:
    ok, _ = validate_schema(_raw(title="标" * 81))
    assert not ok


def test_private_and_noise_keys_are_projected_out() -> None:
    """合并阶段写入的私有键、模型额外输出的噪声键都不应导致整条被丢弃。"""
    raw = _raw()
    raw.raw["_merged_sources"] = 3
    raw.raw["reason"] = "模型自己加的字段"
    raw.raw["line_content"] = "int changed = 1;"
    ok, reason = validate_schema(raw)
    assert ok, reason


# ---------------------------------------------------------------------------- 枚举归一化


def test_category_synonym_is_normalized_not_dropped() -> None:
    """实测回归：模型把"流未关闭"标成 category=resource，本身是真缺陷，不该被丢掉。"""
    raw = _raw(category="resource")
    notes = normalize_enums(raw)
    assert raw.category == "bug"
    assert raw.raw["_category_original"] == "resource"
    assert notes == ["category:resource->bug"]
    ok, reason = validate_schema(raw)
    assert ok, reason


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("resource", "bug"),
        ("null_safety", "bug"),
        ("npe", "bug"),
        ("sql_injection", "security"),
        ("Race-Condition", "concurrency"),
        ("n_plus_one", "performance"),
        ("readability", "maintainability"),
        ("naming", "style"),
    ],
)
def test_category_synonyms(given: str, expected: str) -> None:
    raw = _raw(category=given)
    normalize_enums(raw)
    assert raw.category == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("critical", "blocker"),
        ("major", "high"),
        ("error", "high"),
        ("moderate", "medium"),
        ("minor", "low"),
        ("info", "nit"),
    ],
)
def test_severity_synonyms(given: str, expected: str) -> None:
    raw = _raw(severity=given)
    normalize_enums(raw)
    assert raw.severity == expected


def test_valid_enum_values_are_left_untouched() -> None:
    raw = _raw(category="security", severity="high")
    assert normalize_enums(raw) == []
    assert raw.category == "security"
    assert raw.severity == "high"
    assert "_category_original" not in raw.raw


def test_unmappable_enum_is_still_dropped() -> None:
    """归一化只是给一次机会，不是放宽：仍不合法的照样丢。"""
    raw = _raw(category="vibes")
    normalize_enums(raw)
    result = validate_candidates([raw], _diff_set())
    assert result.kept == []
    assert result.dropped[0].step == "1_schema"


def test_normalization_happens_inside_validate_candidates() -> None:
    result = validate_candidates([_raw(line=11, category="resource")], _diff_set())
    assert len(result.kept) == 1
    assert result.kept[0].category == "bug"


# ---------------------------------------------------------------------------- 第 3 步：类别校验


@pytest.mark.parametrize(
    ("category", "severity", "expected"),
    [
        ("style", "blocker", "low"),
        ("style", "high", "low"),
        ("maintainability", "blocker", "medium"),
        ("maintainability", "high", "medium"),
        ("test", "blocker", "high"),
        ("bug", "blocker", "blocker"),  # 不受限
        ("security", "blocker", "blocker"),
        ("performance", "blocker", "blocker"),
    ],
)
def test_category_severity_cap(category: str, severity: str, expected: str) -> None:
    raw = _raw(category=category, severity=severity, confidence=0.95)
    check_category_severity(raw)
    assert raw.severity == expected


def test_style_blocker_is_clamped_not_dropped() -> None:
    """风格问题当不了 blocker —— 但结论本身保留，只压严重度。"""
    result = validate_candidates(
        [_raw(line=11, category="style", severity="blocker", confidence=0.9)], _diff_set()
    )
    assert len(result.kept) == 1
    assert result.kept[0].severity == "low"
    assert any("category=style 的上限" in n for n in result.kept[0].raw["_adjustments"])


def test_blocker_without_support_is_downgraded() -> None:
    raw = _raw(line=11, severity="blocker", confidence=0.55)
    check_category_severity(raw, has_tool_support=False)
    assert raw.severity == "high"
    assert any("blocker->high" in n for n in raw.raw["_adjustments"])


def test_blocker_with_static_support_is_kept() -> None:
    raw = _raw(line=11, severity="blocker", confidence=0.55)
    check_category_severity(raw, has_tool_support=True)
    assert raw.severity == "blocker"


def test_low_confidence_blocker_escalated_by_tool_evidence_in_validate() -> None:
    """evidence 命中真实静态规则 → 视为有佐证，不被降级。"""
    raw = _raw(
        line=11,
        severity="blocker",
        confidence=0.6,
        evidence=["semgrep:java.sqli"],
    )
    result = validate_candidates(
        [raw], _diff_set(), static_findings=[_FakeStatic("java.sqli")]
    )
    assert result.kept[0].severity == "blocker"


def test_blocker_downgraded_when_evidence_references_unknown_rule() -> None:
    raw = _raw(
        line=11,
        severity="blocker",
        confidence=0.6,
        evidence=["semgrep:not.a.real.rule"],
    )
    result = validate_candidates(
        [raw], _diff_set(), static_findings=[_FakeStatic("java.sqli")]
    )
    assert result.kept[0].severity == "high"


# ---------------------------------------------------------------------------- 第 4 步：存量校验


def test_evidence_found_in_added_lines_is_not_penalized() -> None:
    raw = _raw(line=11, confidence=0.8, evidence=["int changed = 1;"])
    notes = check_existing_code(raw, _diff_set().files[0])
    assert notes == []
    assert raw.confidence == 0.8


def test_evidence_from_existing_code_is_penalized() -> None:
    """证据全部指向存量代码 → 降权（说明模型在评论老问题）。"""
    raw = _raw(
        line=11,
        confidence=0.8,
        evidence=["legacyHelper.compute(a, b);", "if (oldFlag) {"],
    )
    notes = check_existing_code(raw, _diff_set().files[0])
    assert notes and notes[0].startswith("evidence_not_in_added_lines")
    assert raw.confidence == pytest.approx(0.48)  # 0.8 * 0.6
    assert raw.raw["_evidence_not_in_diff"] is True


def test_evidence_whitespace_difference_still_matches() -> None:
    """diff 的缩进与模型引用必然不一致，比较前要去掉全部空白。"""
    raw = _raw(line=11, confidence=0.8, evidence=["    int changed = 1;   "])
    assert check_existing_code(raw, _diff_set().files[0]) == []
    assert raw.confidence == 0.8


def test_non_code_evidence_is_not_treated_as_mismatch() -> None:
    """一句话描述或规则 ID 不是"代码证据"，不该触发这条降权。"""
    raw = _raw(line=11, confidence=0.8, evidence=["缺少空值校验", "java.sqli"])
    assert check_existing_code(raw, _diff_set().files[0]) == []
    assert raw.confidence == 0.8


def test_partial_evidence_hit_is_enough() -> None:
    raw = _raw(
        line=11,
        confidence=0.8,
        evidence=["int changed = 1;", "totally.unrelated.call()"],
    )
    assert check_existing_code(raw, _diff_set().files[0]) == []
    assert raw.confidence == 0.8


def test_end_line_outside_added_range_is_nulled_with_note() -> None:
    result = validate_candidates([_raw(line=11, end_line=999)], _diff_set())
    assert result.kept[0].end_line is None
    assert any("end_line" in n for n in result.kept[0].raw["_adjustments"])


def test_adjustments_are_recorded_for_audit() -> None:
    raw = _raw(line=11, category="resource", severity="critical", confidence=0.95)
    result = validate_candidates([raw], _diff_set())
    adjustments = result.kept[0].raw["_adjustments"]
    assert any(n.startswith("category:resource->bug") for n in adjustments)
    assert any(n.startswith("severity:critical->blocker") for n in adjustments)


# ---------------------------------------------------------------------------- 锚定


def test_anchor_drop_for_hallucinated_line() -> None:
    result = validate_candidates([_raw(line=4242)], _diff_set())
    assert result.kept == []
    assert len(result.dropped) == 1
    assert result.dropped[0].step == "2_anchor"
    assert result.dropped[0].reason.startswith("line_not_in_any_hunk")


def test_keeps_only_added_lines() -> None:
    """带上下文行的 diff：hunk 区间内的上下文行不是"新增行"，必须拒绝。"""
    diff = """diff --git a/src/A.java b/src/A.java
index 1..2 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,2 +11,4 @@
 context before
+int changed = 1;
+int other = 2;
 context after
"""
    diff_set = DiffSet(
        base_sha="b" * 40,
        head_sha="h" * 40,
        merge_base_sha="m" * 40,
        files=parse_unified_diff(diff),
    )
    fd = diff_set.files[0]
    assert fd.added_line_numbers == {12, 13}

    result = validate_candidates(
        [_raw(line=12, title="命中新增行"), _raw(line=14, title="存量上下文行")], diff_set
    )
    assert [f.title for f in result.kept] == ["命中新增行"]
    assert result.dropped[0].reason.startswith("line_in_hunk_but_not_added")


def test_unknown_path_is_dropped() -> None:
    result = validate_candidates([_raw(path="src/NotInDiff.java")], _diff_set())
    assert result.kept == []
    assert result.dropped[0].reason.startswith("path_not_in_diff")


def test_end_line_outside_whitelist_is_nulled_not_dropped() -> None:
    result = validate_candidates([_raw(line=11, end_line=999)], _diff_set())
    assert len(result.kept) == 1
    assert result.kept[0].end_line is None


def test_confidence_threshold_filter() -> None:
    """第 8 步：门槛过滤。它是独立一步（文档 §9.1 最后一步），
    不与第 1~4 步混在一起 —— 评估层要"过门槛之前的完整候选池"才能离线扫阈值。"""
    kept, dropped = apply_threshold([_raw(line=11, confidence=0.2)], 0.65)
    assert kept == []
    assert dropped[0].step == "8_threshold"
    assert "confidence<0.65" in dropped[0].reason


def test_apply_threshold_none_keeps_everything() -> None:
    candidate = _raw(line=11, confidence=0.1)
    kept, dropped = apply_threshold([candidate], None)
    assert kept == [candidate]
    assert dropped == []


def test_apply_threshold_boundary_is_inclusive() -> None:
    """恰好等于门槛应保留 —— 否则 0.65 会被门槛无声地吃掉一条。"""
    candidate = _raw(line=11, confidence=0.65)
    kept, _dropped = apply_threshold([candidate], 0.65)
    assert kept == [candidate]


def test_validate_candidates_does_not_apply_threshold() -> None:
    """第 8 步不在第 1~4 步里，位置错了会让离线扫阈值无法重放。"""
    result = validate_candidates([_raw(line=11, confidence=0.01)], _diff_set())
    assert len(result.kept) == 1
    assert all(record.step != "8_threshold" for record in result.dropped)


def test_anchor_validity_ratio() -> None:
    result = validate_candidates([_raw(line=11), _raw(line=9999)], _diff_set())
    assert result.anchor_validity == 0.5


# ---------------------------------------------------------------------------- 路径归一化


def test_normalize_path_handles_prefixes_and_separators() -> None:
    candidates = ["src/A.java", "src/pkg/B.java"]
    assert normalize_path("src/A.java", candidates) == "src/A.java"
    assert normalize_path("./src/A.java", candidates) == "src/A.java"
    assert normalize_path("a/src/A.java", candidates) == "src/A.java"
    assert normalize_path("b/src/A.java", candidates) == "src/A.java"
    assert normalize_path("src\\A.java", candidates) == "src/A.java"


def test_normalize_path_unique_basename_and_suffix() -> None:
    candidates = ["src/pkg/B.java", "src/C.java"]
    assert normalize_path("pkg/B.java", candidates) == "src/pkg/B.java"
    assert normalize_path("B.java", candidates) == "src/pkg/B.java"


def test_normalize_path_refuses_ambiguous_match() -> None:
    candidates = ["a/X.java", "b/X.java"]
    assert normalize_path("X.java", candidates) is None
    assert normalize_path("other/Y.java", candidates) is None


# ---------------------------------------------------------------------------- 交叉验证


class _FakeStatic:
    def __init__(self, rule_id: str, tool: str = "semgrep") -> None:
        self.rule_id = rule_id
        self.tool = tool


def test_cross_validation_records_matched_rule_ids() -> None:
    raw = _raw(evidence=["semgrep:java.sqli", "无来源的断言"])
    cross_validate_findings([raw], [_FakeStatic("java.sqli")])
    assert raw.raw["_evidence_rule_ids"] == ["java.sqli"]
    assert raw.confidence == 0.8  # 命中即有佐证，不降权


def test_cross_validation_halves_confidence_for_fabricated_evidence() -> None:
    raw = _raw(evidence=["semgrep:not.a.real.rule"])
    cross_validate_findings([raw], [_FakeStatic("java.sqli")])
    assert raw.raw["_evidence_rule_ids"] == []
    assert raw.confidence == 0.4  # 编造证据 → confidence *= 0.5
    assert raw.raw["_evidence_unverified"] is True


def test_cross_validation_is_noop_when_no_static_findings() -> None:
    """阶段一静态分析关闭，这一条保证它确实是等效 no-op。"""
    raw = _raw(evidence=["semgrep:anything"])
    cross_validate_findings([raw], [])
    assert raw.confidence == 0.8
    assert raw.raw["_evidence_rule_ids"] == []
