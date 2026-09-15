"""防注入诱饵与静态探针用例的测试。

两组用例都对着一个**容易被"接口就位"骗过去**的地方：

- 诱饵用例测"代码里的指令能不能让模型闭嘴"——这是安全属性，不是精度问题；
- 静态探针测"静态工具在真实代码上到底能不能报出来"——早前只有罐装 SARIF 的测试，
  真实工具 + 真实代码这条链路从未验证过（实测发现语料里的桩类 API 一条都匹配不上）。
"""

from __future__ import annotations

import pytest

from acra.eval.dataset import full_suite, injection_cases
from acra.eval.decoys import DECOY_TEXTS, decoy_case_ids, decoy_cases
from acra.eval.metrics import CaseOutcome, Reported, compute, render_report
from acra.eval.static_cases import static_cases

# ---------------------------------------------------------------------------- 诱饵


def test_decoy_cases_cover_every_template() -> None:
    cases = decoy_cases()
    assert len(cases) == len(injection_cases(variants=1, negatives=False))
    assert all(c.is_positive for c in cases)
    assert all(c.source == "decoy" for c in cases)


def test_decoy_cases_use_multiple_phrasings() -> None:
    """只测一种话术会高估防护能力。"""
    cases = decoy_cases()
    assert len({c.note for c in cases}) >= 2
    assert len(DECOY_TEXTS) >= 3


def test_decoy_only_touches_comments() -> None:
    """诱饵必须只加注释、不改代码语句 —— 否则"命中"的判定会同时受两件事影响。"""
    base_by_id = {c.case_id: c for c in injection_cases(variants=1, negatives=False)}
    for case in decoy_cases():
        original = base_by_id[case.case_id.removeprefix("decoy-")]
        stripped_head = "\n".join(
            line for line in case.head.split("\n") if not line.strip().startswith("//")
        )
        stripped_base = "\n".join(
            line for line in original.head.split("\n") if not line.strip().startswith("//")
        )
        assert stripped_head == stripped_base, f"{case.case_id} 改动了代码语句"


def test_decoy_preserves_expected_lines_semantics() -> None:
    """marker 沿用原用例，且必须仍然定位得到。"""
    for case in decoy_cases():
        assert case.expected_lines(), f"{case.case_id} 的 marker 定位不到"
        assert case.expectations[0].category
        assert case.expectations[0].severity_min


def test_decoy_ids_are_prefixed() -> None:
    assert all(cid.startswith("decoy-") for cid in decoy_case_ids())


# ---------------------------------------------------------------------------- 静态探针


def test_static_cases_use_real_library_apis() -> None:
    """静态探针必须用真实库 API。

    实测：语料里的自写桩类（`Jdbc` / `Mailer`）在真实 Semgrep 下**一条都匹配不上** ——
    静态规则匹配的是已知的真实 sink。桩类写法的用例只能测模型，测不了静态层。
    """
    case = static_cases()[0]
    assert "java.sql.Statement" in case.head
    assert "executeQuery" in case.head
    assert case.source == "static_probe"


def test_static_cases_change_from_prepared_to_concat() -> None:
    """base 用参数化查询、head 改成拼接 —— 这样 diff 就是"引入注入"这件事本身。"""
    case = static_cases()[0]
    assert "prepareStatement" in case.base
    assert "prepareStatement" not in case.head
    assert case.base != case.head


def test_static_cases_marker_is_locatable_and_unique() -> None:
    for case in static_cases():
        marker = case.expectations[0].marker
        assert case.head.count(marker) == 1
        assert case.expected_lines()


# ---------------------------------------------------------------------------- 完整套件


def test_full_suite_contains_all_three_groups() -> None:
    cases = full_suite()
    sources = {c.source for c in cases}
    assert sources == {"injection", "decoy", "static_probe"}

    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids)), "完整套件里 case_id 不能重复"


def test_full_suite_can_disable_groups() -> None:
    assert {c.source for c in full_suite(decoys=False, static_probes=False)} == {"injection"}
    assert {c.source for c in full_suite(decoys=True, static_probes=False)} == {
        "injection",
        "decoy",
    }


# ---------------------------------------------------------------------------- 分来源指标


def test_by_source_breakdown_and_decoy_protection() -> None:
    outcomes = [
        # 诱饵用例：2 条命中、1 条被带偏
        CaseOutcome(case_id="decoy-a", expected=True, source="decoy", matched=[Reported("a", 1, "bug")]),
        CaseOutcome(case_id="decoy-b", expected=True, source="decoy", matched=[Reported("a", 2, "bug")]),
        CaseOutcome(case_id="decoy-c", expected=True, source="decoy", missed=[3]),
        CaseOutcome(case_id="plain", expected=True, source="injection", matched=[Reported("a", 4, "bug")]),
    ]
    metrics = compute(outcomes)
    assert metrics.by_source["decoy"] == (2, 0, 1)
    assert metrics.by_source["injection"] == (1, 0, 0)
    assert metrics.decoy_protection == pytest.approx(2 / 3)


def test_decoy_protection_is_none_without_decoy_cases() -> None:
    """没有诱饵用例时必须返回 None —— 不能用 1.0 冒充"测过了"。"""
    metrics = compute([CaseOutcome(case_id="x", expected=True, source="injection")])
    assert metrics.decoy_protection is None
    assert metrics.to_dict()["decoy_protection"] is None


def test_report_warns_when_decoy_fools_model() -> None:
    metrics = compute(
        [CaseOutcome(case_id="decoy-a", expected=True, source="decoy", missed=[1])]
    )
    text = render_report(metrics)
    assert "防注入诱饵用例" in text
    assert "被诱饵带偏" in text
    assert "不可信输入" in text


def test_report_is_silent_about_decoys_when_absent() -> None:
    metrics = compute([CaseOutcome(case_id="x", expected=True, source="injection")])
    assert "防注入诱饵用例" not in render_report(metrics)
