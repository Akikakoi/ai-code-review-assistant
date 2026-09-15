"""diff 分块。

对应开发文档 §7.4：

```
1  若变更行总数 <= 120 且所属方法 <= 2 个 → 整文件一块，不切
2  否则按"变更行所属的顶层语法节点"聚类
3  跨块的变更（同一方法被切到两块）→ 强制合并到同一块
4  超大单块（单方法 > 8k tokens）→ 只保留方法签名 + 变更点前后各 60 行
5  块间保持顺序，编号后注入提示词
```

为什么按语法边界切：机械按行数切会把一个方法劈成两半，模型看到前半段时无法判断
后半段的资源释放情况，必然产生误报或漏报（文档 §7.4 末段）。
"""

from __future__ import annotations

from acra.context.budget import DEFAULT_SHARES, Section, TokenBudget
from acra.context.builder import render_l1, render_symbols
from acra.models import Chunk, FileContext, FileDiff, SymbolSpan, estimate_tokens

#: 单块不切分的变更行阈值（文档 §7.4 流程 1）
SINGLE_CHUNK_MAX_LINES = 120
SINGLE_CHUNK_MAX_METHODS = 2

#: 单方法超过该 token 数即判定为超大单块（文档 §7.4 流程 4）
OVERSIZED_METHOD_TOKENS = 8000
OVERSIZED_RADIUS = 60

#: 无归属方法的散落变更行的聚类距离
LOOSE_CLUSTER_GAP = 20

_METHOD_KINDS = {"method", "constructor", "function", "lambda"}


def _method_spans(ctx: FileContext) -> list[SymbolSpan]:
    return [s for s in ctx.enclosing_symbols if s.kind in _METHOD_KINDS]


def _group_loose(lines: list[int]) -> list[list[int]]:
    """把不属于任何方法的变更行按邻近关系聚类。"""
    groups: list[list[int]] = []
    for ln in lines:
        if groups and ln - groups[-1][-1] <= LOOSE_CLUSTER_GAP:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    return groups


def _chunk_units(file_diff: FileDiff, ctx: FileContext) -> list[tuple[set[int], list[SymbolSpan]]]:
    """把变更行按语法节点聚成若干"单元"。"""
    added = sorted(file_diff.added_line_numbers)
    spans = _method_spans(ctx)

    assignments: dict[int, SymbolSpan] = {}
    loose: list[int] = []
    for ln in added:
        hit = [s for s in spans if s.start_line <= ln <= s.end_line]
        if hit:
            # 同一行可能落在嵌套节点内，取最内层
            assignments[ln] = min(hit, key=lambda s: (s.end_line - s.start_line, s.start_line))
        else:
            loose.append(ln)

    grouped: dict[tuple[int, int], tuple[set[int], SymbolSpan]] = {}
    for ln, span in assignments.items():
        key = (span.start_line, span.end_line)
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = ({ln}, span)
        else:
            entry[0].add(ln)

    units: list[tuple[set[int], list[SymbolSpan]]] = [
        (lines, [span]) for lines, span in grouped.values()
    ]
    units.sort(key=lambda u: min(u[0]))
    for group in _group_loose(loose):
        units.append((set(group), []))
    return units


def _shrink_span(span: SymbolSpan, focus: set[int]) -> SymbolSpan:
    """超大单块：只保留方法签名 + 变更点前后各 60 行。"""
    lines = span.source.split("\n")
    targets = sorted(ln for ln in focus if span.start_line <= ln <= span.end_line) or [span.start_line]
    lo = max(span.start_line, min(targets) - OVERSIZED_RADIUS)
    hi = min(span.end_line, max(targets) + OVERSIZED_RADIUS)

    kept: list[str] = []
    kept.append(f"// {span.signature}   // <本块仅展示方法签名与变更点附近 {OVERSIZED_RADIUS} 行>")
    if lo > span.start_line:
        kept.append(f"// ... [省略 {lo - span.start_line} 行] ...")
    for line in lines[lo - span.start_line : hi - span.start_line + 1]:
        kept.append(line)
    if hi < span.end_line:
        kept.append(f"// ... [省略 {span.end_line - hi} 行] ...")

    return SymbolSpan(
        name=span.name,
        kind=span.kind,
        start_line=lo,
        end_line=hi,
        signature=span.signature,
        parent=span.parent,
        source="\n".join(kept),
        truncated=True,
    )


def _pack_chunk_context(
    ctx: FileContext,
    file_diff: FileDiff,
    spans: list[SymbolSpan],
    lines: set[int],
    chunk_budget: int,
    context_lines: int,
) -> FileContext:
    """为单个块重新装配上下文（L1 只含本块变更行，L2 只含本块方法）。"""
    l1 = render_l1(file_diff, ctx.source_lines, context_lines=context_lines, only_lines=lines)
    l2 = render_symbols(spans)

    shares = _normalize({k: v for k, v in DEFAULT_SHARES.items() if k != "system"})
    budget = TokenBudget(int(chunk_budget * 0.9), shares)
    focus_line = 1
    if spans:
        focus_line = max(1, (min(lines) if lines else spans[0].start_line) - spans[0].start_line + 1)
    packed = budget.pack(
        [
            Section("static", ctx.static_text, strategy="head", priority=0),
            Section("l2", l2, strategy="focus", focus_line=focus_line, priority=1),
            Section("l1", l1, strategy="no_truncate", priority=2),
            Section("l3", ctx.l3_text, strategy="drop", priority=3),
        ]
    )

    degraded = list(ctx.degraded)
    degraded.extend(f"truncated:{n}" for n in packed.truncated)
    degraded.extend(f"dropped:{n}" for n in packed.dropped)

    chunk_ctx = FileContext(
        path=ctx.path,
        level=ctx.level,
        diff_text=packed.get("l1"),
        enclosing_symbols=spans,
        imports=ctx.imports,
        referenced_types=ctx.referenced_types,
        callers=ctx.callers,
        similar_impls=ctx.similar_impls,
        prior_comments=ctx.prior_comments,
        static_findings=ctx.static_findings,
        source_lines=ctx.source_lines,
        degraded=degraded,
        l3_notes=list(ctx.l3_notes),
    )
    chunk_ctx.enclosing_source_text = packed.get("l2")
    chunk_ctx.static_text = packed.get("static")
    chunk_ctx.l3_text = packed.get("l3")
    chunk_ctx.packed_note = packed.note()
    chunk_ctx.truncated_sections = list(packed.truncated)
    chunk_ctx.token_estimate = (
        estimate_tokens(chunk_ctx.diff_text)
        + estimate_tokens(chunk_ctx.enclosing_source_text)
        + estimate_tokens(chunk_ctx.static_text)
    )
    return chunk_ctx


def _normalize(shares: dict[str, float]) -> dict[str, float]:
    total = sum(shares.values()) or 1.0
    return {k: v / total for k, v in shares.items()}


def _was_truncated(ctx: FileContext) -> bool:
    """本次装配是否触发了 l1/l2/static 截断。"""
    return bool(ctx.truncated_sections)


def _maybe_shrink(spans: list[SymbolSpan], focus: set[int]) -> list[SymbolSpan]:
    """超大方法（文档 §7.4 流程 4）：单方法超预算时只保留签名 + 变更点前后各 60 行。

    这条规则在"整文件单块"与"多块"两条路径上都必须生效 —— 一个 4000 行的单方法
    同样属于"超大单块"，不能因为文件里只有它一个方法就躲过截断。
    """
    if not any(estimate_tokens(s.source) > OVERSIZED_METHOD_TOKENS for s in spans):
        return spans
    return [_shrink_span(s, focus) for s in spans]


def chunk_file(file_diff: FileDiff, ctx: FileContext, settings) -> list[Chunk]:
    """把一个文件的变更切成若干块。"""
    chunk_budget = settings.acra_chunk_input_token_budget
    context_lines = settings.acra_context_context_lines
    added = file_diff.added_line_numbers

    if not added:
        return []

    method_spans = _method_spans(ctx)
    single = (
        len(added) <= SINGLE_CHUNK_MAX_LINES and len(method_spans) <= SINGLE_CHUNK_MAX_METHODS
    ) or file_diff.change_type == "add"

    if single:
        spans = _maybe_shrink(method_spans or ctx.enclosing_symbols, added)
        chunk_ctx = _pack_chunk_context(ctx, file_diff, spans, added, chunk_budget, context_lines)
        return [
            Chunk(
                index=1,
                total=1,
                path=file_diff.path,
                change_type=file_diff.change_type.value
                if hasattr(file_diff.change_type, "value")
                else str(file_diff.change_type),
                added_line_numbers=set(added),
                diff_text=chunk_ctx.diff_text,
                context=chunk_ctx,
                token_estimate=chunk_ctx.token_estimate,
                truncated=any(s.truncated for s in spans),
            )
        ]

    units = _chunk_units(file_diff, ctx)

    merged: list[tuple[set[int], list[SymbolSpan]]] = []
    for lines, spans in units:
        if merged:
            prev_lines, prev_spans = merged[-1]
            candidate_lines = prev_lines | lines
            candidate_spans = prev_spans + [s for s in spans if s not in prev_spans]
            probe = _pack_chunk_context(
                ctx, file_diff, candidate_spans, candidate_lines, chunk_budget, context_lines
            )
            # 判据必须用"装配过程有没有触发截断"，而不是装配后的 token 估算：
            # 装配本身会按预算截断，截断后的估算永远在预算之内，条件恒为真、块永远合不完。
            if probe.token_estimate <= chunk_budget and not _was_truncated(probe):
                merged[-1] = (candidate_lines, candidate_spans)
                continue
        merged.append((set(lines), list(spans)))

    chunks: list[Chunk] = []
    for lines, spans in merged:
        spans = _maybe_shrink(spans, lines)
        chunk_ctx = _pack_chunk_context(ctx, file_diff, spans, lines, chunk_budget, context_lines)
        chunks.append(
            Chunk(
                index=0,
                total=0,
                path=file_diff.path,
                change_type=file_diff.change_type.value
                if hasattr(file_diff.change_type, "value")
                else str(file_diff.change_type),
                added_line_numbers=set(lines),
                diff_text=chunk_ctx.diff_text,
                context=chunk_ctx,
                token_estimate=chunk_ctx.token_estimate,
                truncated=any(s.truncated for s in spans),
            )
        )

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        chunk.index = i
        chunk.total = total
    return chunks
