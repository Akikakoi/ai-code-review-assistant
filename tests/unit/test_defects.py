"""缺陷注入语料的测试：模板自检、变体展开、分层抽样。

这份语料是 Recall 的唯一度量基础，模板写错会**静默**让指标失真（比如 marker 定位到
错的行、渲染出的文件根本编译不过）。因此这里重点测"语料本身是否自洽"。
"""

from __future__ import annotations

import pytest

from acra.eval.dataset import injection_cases, sample_cases
from acra.eval.defects import TEMPLATES, DefectTemplate, expand, template_ids


def test_library_covers_twenty_categories() -> None:
    """文档 §11.1 B 明确要求 20 类常见缺陷。"""
    assert len(TEMPLATES) == 20
    assert len(template_ids()) == 20


def test_doc_named_defects_are_present() -> None:
    """文档点名的那几类必须在库里：去掉空值判断、改掉锁范围、交换比较符号、
    删除资源释放、扩大事务范围。"""
    ids = set(template_ids())
    assert "null-check-removed" in ids  # 去掉空值判断
    assert "lock-scope-widened" in ids  # 改掉锁范围
    assert "condition-inverted" in ids  # 交换比较符号
    assert "resource-release-removed" in ids  # 删除资源释放
    assert "transaction-scope-widened" in ids  # 扩大事务范围


@pytest.mark.parametrize("template", TEMPLATES, ids=template_ids())
def test_template_renders_compilable_shape(template: DefectTemplate) -> None:
    base = template.render(
        class_name=template.class_name, method_name=template.method_name, defect=False
    )
    head = template.render(
        class_name=template.class_name, method_name=template.method_name, defect=True
    )

    assert base.startswith("package com.example.library;")
    assert f"public class {template.class_name} {{" in base
    assert base.rstrip().endswith("}")
    assert "__BODY__" not in base and "__BODY__" not in head
    assert "__CLASS__" not in head and "__METHOD__" not in head


@pytest.mark.parametrize("template", TEMPLATES, ids=template_ids())
def test_template_base_and_head_differ(template: DefectTemplate) -> None:
    base = template.render(
        class_name=template.class_name, method_name=template.method_name, defect=False
    )
    head = template.render(
        class_name=template.class_name, method_name=template.method_name, defect=True
    )
    assert base != head


@pytest.mark.parametrize("template", TEMPLATES, ids=template_ids())
def test_marker_is_unique_in_head(template: DefectTemplate) -> None:
    """marker 不唯一 → ground truth 行号会定位到错的行，指标静默失真。"""
    head = template.render(
        class_name=template.class_name, method_name=template.method_name, defect=True
    )
    marker = template.marker_for(template.method_name)
    assert head.count(marker) == 1, f"{template.template_id} 的 marker 不唯一"


def test_expand_produces_variants_and_negatives() -> None:
    cases = expand(variants=3, negatives=True)
    positives = [c for c in cases if c.is_positive]
    negatives = [c for c in cases if not c.is_positive]

    assert len(positives) == len(TEMPLATES) * 3
    assert len(negatives) == len(TEMPLATES)

    # 每个模板的第一份沿用原类名，其余加后缀
    named = {c.case_id for c in positives}
    assert "condition-inverted" in named
    assert "condition-inverted-1" in named


def test_expand_respects_variants_and_negatives_flags() -> None:
    assert len(expand(variants=1, negatives=False)) == len(TEMPLATES)
    assert len(expand(variants=2, negatives=False)) == len(TEMPLATES) * 2


def test_all_positive_expectations_are_locatable() -> None:
    for case in injection_cases():
        if not case.is_positive:
            continue
        lines = case.expected_lines()
        assert lines, f"{case.case_id}: marker 在 head 里定位不到"
        assert lines[0] > 0


def test_negatives_have_real_diff_but_no_defect() -> None:
    """反例必须是真实变更（否则 diff 为空），且不携带任何期望结论。"""
    negatives = [c for c in injection_cases() if not c.is_positive]
    assert negatives
    for case in negatives:
        assert case.base != case.head, f"{case.case_id} 没有真实变更"
        assert not case.expectations
        assert case.expected_lines() == []


def test_variant_paths_match_class_names() -> None:
    """Java 要求 public 类名与文件名一致，否则会引入一个"真问题"污染反例。"""
    for case in injection_cases():
        class_name = case.path.rsplit("/", 1)[-1].removesuffix(".java")
        assert f"class {class_name}" in case.head, f"{case.case_id} 类名与文件名不一致"


def test_expanded_ids_are_unique() -> None:
    ids = [c.case_id for c in injection_cases()]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------- 抽样


def test_sample_keeps_positive_negative_ratio() -> None:
    cases = injection_cases(variants=3, negatives=True)  # 60 正 / 20 负
    picked = sample_cases(cases, 20)
    positives = sum(1 for c in picked if c.is_positive)
    negatives = len(picked) - positives
    assert positives + negatives == 20
    # 原始比例 3:1，抽样后不应出现"全是正例"这种失衡
    assert positives >= 10
    assert negatives >= 3


def test_sample_is_deterministic_for_same_seed() -> None:
    cases = injection_cases()
    first = sample_cases(cases, 15, seed=7)
    second = sample_cases(cases, 15, seed=7)
    assert [c.case_id for c in first] == [c.case_id for c in second]


def test_sample_differs_with_other_seed() -> None:
    cases = injection_cases()
    assert [c.case_id for c in sample_cases(cases, 15, seed=1)] != [
        c.case_id for c in sample_cases(cases, 15, seed=2)
    ]


def test_sample_larger_than_dataset_returns_everything() -> None:
    cases = injection_cases()
    assert sample_cases(cases, 10_000) == list(cases)
    assert sample_cases(cases, None) == list(cases)
