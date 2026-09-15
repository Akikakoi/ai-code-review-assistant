"""Token 预算与分块的单测。

文档 §14.1：「token 预算：装配顺序、超预算截断、空上下文」；
「分块算法：单方法、跨方法、超大方法截断、跨块变更合并」。
"""

from __future__ import annotations

from acra.context.budget import (
    RunBudget,
    Section,
    TokenBudget,
    truncate_head_tail,
    truncate_tokens,
)
from acra.context.builder import render_l1, render_symbols
from acra.context.chunker import OVERSIZED_METHOD_TOKENS, chunk_file
from acra.models import (
    ChangeType,
    FileContext,
    FileDiff,
    Hunk,
    SymbolSpan,
    estimate_tokens,
)
from acra.repo.diff_parser import parse_unified_diff
from acra.repo.symbol_index import find_enclosing_spans

# ---------------------------------------------------------------------------- 基础截断


def test_truncate_tokens_noop_when_within_budget() -> None:
    text = "short text"
    assert truncate_tokens(text, 1000) == text


def test_truncate_tokens_respects_allowance() -> None:
    text = "x" * 3000
    truncated = truncate_tokens(text, 100)
    assert len(truncated) < len(text)
    assert estimate_tokens(truncated) <= 100


def test_truncate_tokens_zero_budget_is_empty() -> None:
    assert truncate_tokens("anything", 0) == ""


def test_truncate_head_tail_keeps_head_and_focus_region() -> None:
    lines = [f"line {i}" for i in range(1, 400)]
    text = "\n".join(lines)
    out = truncate_head_tail(text, 200, focus_line=200)
    assert "省略" in out
    assert out.split("\n")[0] == "line 1"          # 头部（方法签名所在处）保留
    assert "line 200" in out                        # 变更点附近保留
    assert estimate_tokens(out) <= 200


def test_truncate_head_tail_noop_when_fits() -> None:
    text = "\n".join(f"l{i}" for i in range(5))
    assert truncate_head_tail(text, 10_000, focus_line=1) == text


# ---------------------------------------------------------------------------- 装配


def test_pack_assigns_budget_by_share() -> None:
    budget = TokenBudget(1000)
    assert budget.limit_for("l2") == 450
    assert budget.limit_for("l1") == 200
    assert budget.limit_for("system") == 100


def test_pack_truncates_oversized_section_and_records_it() -> None:
    budget = TokenBudget(500)
    packed = budget.pack([Section("l2", "y" * 9000, strategy="focus", focus_line=1)])
    assert "l2" in packed.truncated
    assert estimate_tokens(packed.get("l2")) <= budget.limit_for("l2") + 1


def test_pack_drops_l3_whole_when_over_budget() -> None:
    budget = TokenBudget(300)
    packed = budget.pack(
        [
            Section("l2", "a" * 300, strategy="focus", priority=1),
            Section("l3", "b" * 9000, strategy="drop", priority=3),
        ]
    )
    assert "l3" in packed.dropped
    assert packed.get("l3") == ""


def test_pack_empty_sections_are_reported_as_empty_not_dropped() -> None:
    packed = TokenBudget(500).pack([Section("l3", "", strategy="drop")])
    assert packed.get("l3") == ""
    assert packed.dropped == []


def test_pack_respects_stop_budget() -> None:
    budget = TokenBudget(10_000)
    packed = budget.pack(
        [Section("l2", "z" * 6000, strategy="focus")], stop_budget=100
    )
    assert estimate_tokens(packed.get("l2")) <= 100


def test_run_budget_gates_l3_and_exhaustion() -> None:
    rb = RunBudget(total=1000)
    assert rb.allow_l3 is True
    rb.charge(700)
    assert rb.ratio == 0.7
    assert rb.allow_l3 is True
    rb.charge(150)  # 85%
    assert rb.allow_l3 is False
    assert rb.exhausted is False
    rb.charge(200)
    assert rb.exhausted is True
    assert rb.remaining() == 0


# ---------------------------------------------------------------------------- L1 渲染


def test_render_l1_marks_added_lines_and_includes_context() -> None:
    diff = """diff --git a/A.java b/A.java
index 1..2 100644
--- a/A.java
+++ b/A.java
@@ -10,0 +11,1 @@
+int changed = 1;
"""
    fd = parse_unified_diff(diff)[0]
    source = "\n".join(f"src{i}" for i in range(1, 21)).split("\n")
    source = [f"src{i}" for i in range(1, 21)]
    text = render_l1(fd, source, context_lines=2)
    assert "   11|+int changed = 1;" in text
    assert "    9| src9" in text      # 上文 2 行
    assert "   13| src13" in text     # 下文 2 行


def test_render_l1_only_lines_filters_other_chunks() -> None:
    diff = """diff --git a/A.java b/A.java
index 1..2 100644
--- a/A.java
+++ b/A.java
@@ -10,0 +11,2 @@
+int a = 1;
+int b = 2;
"""
    fd = parse_unified_diff(diff)[0]
    source = [f"src{i}" for i in range(1, 21)]
    text = render_l1(fd, source, context_lines=0, only_lines={12})
    assert "int b = 2;" in text
    assert "int a = 1;" not in text


def test_render_l1_reports_removed_old_lines_separately() -> None:
    diff = """diff --git a/A.java b/A.java
index 1..2 100644
--- a/A.java
+++ b/A.java
@@ -10,1 +10,1 @@
-old value
+new value
"""
    fd = parse_unified_diff(diff)[0]
    text = render_l1(fd, [f"s{i}" for i in range(1, 15)], context_lines=0)
    assert "new value" in text
    assert "被删除的旧行" in text
    assert "old value" in text


def test_render_symbols_includes_line_numbers_and_truncation_marker() -> None:
    span = SymbolSpan(
        name="pay", kind="method", start_line=10, end_line=12, source="a\nb\nc", truncated=True
    )
    text = render_symbols([span])
    assert "method pay" in text
    assert "   10| a" in text
    assert "已截断" in text


# ---------------------------------------------------------------------------- 分块

JAVA_SOURCE = """package com.example;

public class Svc {

    public void alpha() {
        int a = 1;
        int b = 2;
    }

    public void beta() {
        int c = 3;
    }
}
"""


def _file_diff(path: str, added: dict[int, str], change_type=ChangeType.MODIFY) -> FileDiff:
    """构造一个只有新增行的 FileDiff（测试分块逻辑用，不走 git）。"""
    from acra.models import Hunk

    low = min(added)
    hunk = Hunk(
        old_start=low,
        old_lines=0,
        new_start=low,
        new_lines=len(added),
        added=sorted(added.items()),
    )
    return FileDiff(path=path, change_type=change_type, hunks=[hunk])


def _ctx(path: str, source: list[str], focus: set[int], level: int = 2) -> FileContext:
    spans, degraded = find_enclosing_spans("\n".join(source), focus, path=path)
    return FileContext(
        path=path,
        level=level,
        enclosing_symbols=spans,
        source_lines=source,
        degraded=degraded,
    )


def test_small_change_is_a_single_chunk(settings) -> None:
    source = JAVA_SOURCE.split("\n")
    diff = _file_diff("Svc.java", {6: "        int a = 1;", 7: "        int b = 2;"})
    chunks = chunk_file(diff, _ctx("Svc.java", source, {6, 7}), settings)
    assert len(chunks) == 1
    assert chunks[0].total == 1
    assert chunks[0].added_line_numbers == {6, 7}


def test_added_file_is_always_a_single_chunk(settings) -> None:
    source = [f"line {i}" for i in range(1, 400)]
    added = {i: source[i - 1] for i in range(1, 400)}
    diff = _file_diff("Big.java", added, change_type=ChangeType.ADD)
    chunks = chunk_file(diff, _ctx("Big.java", source, set(added)), settings)
    assert len(chunks) == 1


def test_many_methods_produce_multiple_chunks_partitioning_added_lines(settings) -> None:
    """多个相距很远的变更簇必须被切成多块，且各块的新增行号严格划分、不重不漏。"""
    # 每个方法体给足行数，让单个"单元"就接近块预算，合并无法把所有簇塞进一块
    settings.acra_chunk_input_token_budget = 700

    source = ["package p;", "public class Svc {"]
    clusters: list[tuple[int, int]] = []  # (方法体首行, 行数)
    for i in range(12):
        source.append(f"    public void m{i}() {{")
        body_start = len(source) + 1
        for k in range(24):
            source.append(f"        String v{i}_{k} = \"value number {i} {k}\";")
        clusters.append((body_start, 24))
        source.append("    }")
        source.append("")
    source.append("}")

    picks = [clusters[0], clusters[len(clusters) // 2], clusters[-1]]
    hunks: list[Hunk] = []
    expected: set[int] = set()
    for start, count in picks:
        added_lines = [(start + k, source[start + k - 1]) for k in range(count)]
        expected.update(ln for ln, _ in added_lines)
        hunks.append(
            Hunk(
                old_start=start,
                old_lines=0,
                new_start=start,
                new_lines=count,
                added=added_lines,
            )
        )

    diff = FileDiff(path="Svc.java", change_type=ChangeType.MODIFY, hunks=hunks)
    chunks = chunk_file(diff, _ctx("Svc.java", source, expected), settings)

    assert len(chunks) >= 2
    merged_lines: set[int] = set()
    for chunk in chunks:
        assert chunk.added_line_numbers & merged_lines == set(), "不同块的新增行不得重叠"
        merged_lines |= chunk.added_line_numbers
    assert merged_lines == expected, "所有新增行必须恰好被覆盖一次"
    assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c.total == len(chunks) for c in chunks)


def test_oversized_method_is_shrunk_and_marked_truncated(settings) -> None:
    """单方法超过 OVERSIZED_METHOD_TOKENS 时只保留签名 + 变更点前后各 60 行。"""
    body = [f"        int v{i} = {i};" for i in range(OVERSIZED_METHOD_TOKENS * 2)]
    source = ["package p;", "public class Svc {", "    public void big() {", *body, "    }", "}"]
    focus_line = 3 + len(body) // 2
    diff = FileDiff(
        path="Svc.java",
        change_type=ChangeType.MODIFY,
        hunks=[],
    )
    from acra.models import Hunk

    diff.hunks = [
        Hunk(
            old_start=focus_line,
            old_lines=0,
            new_start=focus_line,
            new_lines=1,
            added=[(focus_line, body[len(body) // 2])],
        )
    ]
    ctx = _ctx("Svc.java", source, {focus_line})
    assert "window" not in {s.kind for s in ctx.enclosing_symbols}, "应能定位到方法"

    chunks = chunk_file(diff, ctx, settings)
    assert len(chunks) == 1
    assert chunks[0].truncated is True
    assert "省略" in chunks[0].context.enclosing_source_text


def test_chunk_with_no_added_lines_is_empty(settings) -> None:
    diff = FileDiff(path="A.java", change_type=ChangeType.DELETE, hunks=[])
    assert chunk_file(diff, _ctx("A.java", ["a"], set()), settings) == []


def test_chunk_budget_is_enforced(settings) -> None:
    settings.acra_chunk_input_token_budget = 800
    source = ["package p;", "public class Svc {"]
    added = {}
    for i in range(60):
        source.append(f"    public void m{i}() {{")
        source.append(f"        String s{i} = \"value number {i}\";")
        added[len(source)] = source[-1]
        source.append("    }")
    source.append("}")
    diff = FileDiff(path="Svc.java", change_type=ChangeType.MODIFY, hunks=[])
    from acra.models import Hunk

    for ln, text in sorted(added.items()):
        diff.hunks.append(
            Hunk(old_start=ln, old_lines=0, new_start=ln, new_lines=1, added=[(ln, text)])
        )
    chunks = chunk_file(diff, _ctx("Svc.java", source, set(added)), settings)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.token_estimate <= settings.acra_chunk_input_token_budget
