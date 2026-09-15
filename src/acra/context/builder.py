"""L1 / L2 / L3 上下文装配与渲染。

对应开发文档 §4.4 与 §7。

阶段一实现范围：L1（diff + 邻接上下文行）+ L2（变更所在方法源码、imports）；
L3（反向引用、相似实现、历史评论）接口已就位但默认关闭（文档 §7.1：L3 按需触发、默认关闭）。

提示词块的渲染格式刻意做得显式：每行都带真实行号，新增行用 `+` 标注，
让模型"选行号"这件事变成抄写而不是推测。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from acra.context.budget import DEFAULT_SHARES, Section, TokenBudget
from acra.context.retriever import Retriever
from acra.models import (
    CallSite,
    CodeSlice,
    FileContext,
    FileDiff,
    PriorComment,
    StaticFinding,
    SymbolSpan,
    TypeSig,
    estimate_tokens,
)
from acra.repo.symbol_index import (
    RepoFileIndex,
    SymbolIndex,
    extract_imports,
    find_enclosing_spans,
    language_for_path,
    resolve_referenced_paths,
)

# ---------------------------------------------------------------------------- 渲染


def render_numbered(text: str, start_line: int = 1, *, marker: str = " ") -> str:
    """给一段源码加上行号。"""
    out: list[str] = []
    for offset, line in enumerate(text.split("\n")):
        out.append(f"{start_line + offset:>5}|{marker}{line.rstrip()}")
    return "\n".join(out)


def render_symbols(spans: Iterable[SymbolSpan]) -> str:
    """渲染 L2 主体：变更所在的方法 / 类源码（含行号）。"""
    blocks: list[str] = []
    for span in spans:
        header = f"--- {span.kind} {span.name}"
        if span.parent:
            header += f" (in {span.parent})"
        header += f" @ lines {span.start_line}-{span.end_line} ---"
        if span.truncated:
            header += " [已截断，仅展示关键片段]"
        blocks.append(header)
        blocks.append(render_numbered(span.source, span.start_line))
    return "\n".join(blocks)


def render_imports(imports: Iterable[str]) -> str:
    items = list(imports)
    if not items:
        return ""
    return "\n".join(items)


def render_types(types) -> str:
    blocks: list[str] = []
    for t in types:
        blocks.append(f"--- {t.name}  (定义于 {t.path}) ---")
        blocks.append(t.signature)
    return "\n".join(blocks)


def render_static(findings: Iterable[StaticFinding]) -> str:
    lines: list[str] = []
    for f in findings:
        lines.append(f"- [{f.tool}:{f.rule_id}] line {f.line}: {f.message}")
    return "\n".join(lines)


def render_prior_comments(comments: Iterable[PriorComment]) -> str:
    lines: list[str] = []
    for c in comments:
        body = " ".join(c.body.split())[:200]
        lines.append(f"- {c.path}:{c.line} {body}")
    return "\n".join(lines)


def render_similar(impls: Iterable[CodeSlice]) -> str:
    blocks: list[str] = []
    for impl in impls:
        blocks.append(f"--- {impl.path}:{impl.start_line}-{impl.end_line} (相似度 {impl.score:.2f}) ---")
        blocks.append(render_numbered(impl.source, impl.start_line))
    return "\n".join(blocks)


def render_callers(callers: Iterable[CallSite]) -> str:
    lines: list[str] = []
    for c in callers:
        lines.append(f"- {c.caller_path}:{c.caller_line} 调用 {c.symbol}: {c.snippet[:160]}")
    return "\n".join(lines)


def render_l1(
    file_diff: FileDiff,
    source_lines: list[str],
    *,
    context_lines: int = 3,
    only_lines: set[int] | None = None,
    max_lines: int = 600,
) -> str:
    """渲染 L1：变更行 + 邻接上下文行。

    `only_lines` 用于分块场景 —— 只渲染属于本块的新增行，但邻接上下文照常带出，
    以保证每块的语义自洽（文档 §7.4 流程 3）。
    """
    blocks: list[str] = []
    seen: set[tuple[str, int]] = set()
    emitted = 0

    for idx, hunk in enumerate(file_diff.hunks, 1):
        added = [(ln, t) for ln, t in hunk.added if only_lines is None or ln in only_lines]
        relevant_removed = [
            (ln, t) for ln, t in hunk.removed if only_lines is None or _removed_relevant(hunk, only_lines)
        ]
        if not added and not relevant_removed:
            continue

        body: list[str] = []
        if added:
            added_map = dict(added)
            lo = min(ln for ln, _ in added)
            hi = max(ln for ln, _ in added)
            start = max(1, lo - context_lines)
            end = min(len(source_lines), hi + context_lines) if source_lines else hi
            for ln in range(start, end + 1):
                key = (file_diff.path, ln)
                if key in seen:
                    continue
                seen.add(key)
                if ln in added_map:
                    body.append(f"{ln:>5}|+{added_map[ln].rstrip()}")
                else:
                    src = source_lines[ln - 1] if 0 < ln <= len(source_lines) else ""
                    body.append(f"{ln:>5}| {src.rstrip()}")
        elif relevant_removed:
            for ln, text in relevant_removed:
                body.append(f"{ln:>5}|-{text.rstrip()}")

        if not body:
            continue

        head = f"@@ hunk {idx} -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@"
        block_lines = [head, *body]

        kept: list[str] = []
        for line in block_lines:
            if emitted >= max_lines:
                kept.append("... [L1 已达到本块渲染行数上限，后续行省略] ...")
                break
            kept.append(line)
            emitted += 1
        blocks.append("\n".join(kept))

        if hunk.removed and added:
            removed_lines = [f"{ln:>5}|-{t.rstrip()}" for ln, t in hunk.removed]
            blocks.append("--- 同 hunks 内被删除的旧行（旧文件行号，仅供理解上下文）---")
            blocks.append("\n".join(removed_lines[:80]))

    return "\n".join(blocks)


def _removed_relevant(hunk, only_lines: set[int]) -> bool:
    """该 hunk 的删除行是否与目标新增行同属一个改动簇。"""
    if not hunk.added:
        return True
    added_lines = {ln for ln, _ in hunk.added}
    return bool(added_lines & only_lines)


# ---------------------------------------------------------------------------- 构建器


@dataclass(slots=True)
class BuildResult:
    context: FileContext
    budget_note: str


class ContextBuilder:
    """按 L1 / L2 / L3 组装 ReviewContext。"""

    def __init__(
        self,
        handle,
        settings,
        *,
        head_sha: str,
        symbol_index: SymbolIndex | None = None,
        repo_files: list[str] | None = None,
    ) -> None:
        self.handle = handle
        self.settings = settings
        self.head_sha = head_sha
        self.symbol_index = symbol_index
        #: 仓库文件清单，用于把 import 解析成定义文件的路径（按需索引，不做全量索引）
        self._file_index = RepoFileIndex(repo_files) if repo_files else None
        self._indexed_paths: set[str] = set()
        self._source_cache: dict[str, list[str]] = {}

    # ---------------------------------------------------------------- 符号索引

    def _ensure_indexed(self, path: str, imports: list[str]) -> None:
        """把"当前文件 + 它引用的类型定义文件"按需加入索引。

        预算由 `ACRA_L2_MAX_INDEX_FILES` 控制：索引是手段不是目的，
        真正会写进提示词的只有当前方法签名里出现的那些类型（文档 §7.5）。
        """
        if self.symbol_index is None or self._file_index is None:
            return

        budget = self.settings.acra_l2_max_index_files
        targets = [path, *resolve_referenced_paths(imports, path, self._file_index)]
        for target in targets:
            if len(self._indexed_paths) >= budget:
                return
            if target in self._indexed_paths:
                continue
            # 先登记再读：文件不存在时不重复尝试
            self._indexed_paths.add(target)
            source = "\n".join(self.source_lines(target))
            if source:
                self.symbol_index.add_file(target, source)

    # ---------------------------------------------------------------- 源码

    def source_lines(self, path: str) -> list[str]:
        cached = self._source_cache.get(path)
        if cached is None:
            cached = self.handle.file_lines(self.head_sha, path)
            self._source_cache[path] = cached
        return cached

    # ---------------------------------------------------------------- 构建

    def build(
        self,
        file_diff: FileDiff,
        *,
        level: int | None = None,
        focus_lines: set[int] | None = None,
        static_findings: list[StaticFinding] | None = None,
        prior_comments: list[PriorComment] | None = None,
        allow_l3: bool = False,
        retriever: Retriever | None = None,
    ) -> FileContext:
        level = level or min(self.settings.acra_context_level_max, 3)
        # §7.2：触发条件命中就把该文件升到 L3。"默认关闭"的含义是**不命中就不开**，
        # 不是"命中了也不开" —— 之前 level 停在 2，于是 L3 那一段永远进不去，
        # 触发条件算出来了也没用。
        if allow_l3:
            level = max(level, 3)
        focus = set(focus_lines) if focus_lines else set(file_diff.added_line_numbers)
        degraded: list[str] = []

        lines = [] if file_diff.is_binary else self.source_lines(file_diff.path)
        if not lines and not file_diff.is_binary:
            degraded.append("source_unavailable")

        # ---- L2：变更所在方法 ----
        spans: list[SymbolSpan] = []
        if level >= 2 and lines:
            spans, span_degrades = find_enclosing_spans(
                "\n".join(lines), focus, path=file_diff.path
            )
            degraded.extend(span_degrades)
        elif level < 2:
            degraded.append("l2_disabled")

        lang = language_for_path(file_diff.path)
        imports = extract_imports("\n".join(lines), lang) if (level >= 2 and lines) else []

        referenced_types: list[TypeSig] = []
        if (
            level >= 2
            and lines
            and self.settings.acra_l2_type_signatures
            and self.symbol_index is not None
            and spans
        ):
            self._ensure_indexed(file_diff.path, imports)
            referenced_types = self.symbol_index.type_signatures(_type_names_in(spans))
            if not referenced_types and not self._file_index:
                degraded.append("l2_no_repo_file_index")

        # ---- L3：按需触发（§7.1 默认关闭；`allow_l3` 由 §7.2 的触发条件决定）----
        callers: list[CallSite] = []
        similar: list[CodeSlice] = []
        l3_notes: list[str] = []
        priors = list(prior_comments or [])
        # 三类 L3 线索一起受 `allow_l3` 管：不到 L3 就不该有任何一类出现在上下文里。
        # 原先这里只有一句 `priors = [] if level < 3 else priors` —— 它是个 no-op，
        # 于是 `allow_l3=False` 时历史评论照样会被注进去。
        if not allow_l3:
            priors = []
        else:
            if retriever is not None:
                clues = retriever.clues(file_diff, spans)
                callers = clues.callers
                similar = clues.similar
                l3_notes = list(clues.notes)
                if clues.truncated:
                    # 线索可能不完整 —— 这条是**降级**（与 notes 不同）
                    degraded.append(
                        f"l3_partial:候选文件按配额截断为 {retriever.max_scan_files}"
                    )
            else:
                l3_notes.append("L3 触发但未提供检索器，本次无 L3 线索")

        # ---- 预算装配 ----
        context_lines = self.settings.acra_context_context_lines
        l1_text = render_l1(file_diff, lines, context_lines=context_lines)
        l2_text = render_symbols(spans)
        static_text = render_static(static_findings or [])

        focus_line = 1
        if spans:
            focus_line = max(1, (min(focus) if focus else spans[0].start_line) - spans[0].start_line + 1)

        shares = {k: v for k, v in DEFAULT_SHARES.items() if k != "system"}
        shares = _normalize(shares)
        budget = TokenBudget(int(self.settings.acra_chunk_input_token_budget * 0.9), shares)
        packed = budget.pack(
            [
                Section("static", static_text, strategy="head", priority=0),
                Section("l2", l2_text, strategy="focus", focus_line=focus_line, priority=1),
                Section("l1", l1_text, strategy="no_truncate", priority=2),
                Section(
                    "l3",
                    _l3_text(similar, callers, priors),
                    strategy="drop",
                    priority=3,
                ),
            ]
        )

        if packed.truncated:
            degraded.extend(f"truncated:{name}" for name in packed.truncated)
        if packed.dropped:
            degraded.extend(f"dropped:{name}" for name in packed.dropped)
        for span in spans:
            if span.kind == "window":
                span.truncated = True

        ctx = FileContext(
            path=file_diff.path,
            level=level,
            diff_text=packed.get("l1"),
            enclosing_symbols=spans,
            imports=imports,
            referenced_types=referenced_types,
            callers=callers,
            similar_impls=similar,
            prior_comments=priors,
            static_findings=list(static_findings or []),
            source_lines=lines,
            degraded=degraded,
            l3_notes=l3_notes,
        )
        ctx.enclosing_source_text = packed.get("l2")
        ctx.static_text = packed.get("static")
        ctx.l3_text = packed.get("l3")
        ctx.packed_note = packed.note()
        ctx.truncated_sections = list(packed.truncated)
        ctx.token_estimate = (
            estimate_tokens(ctx.diff_text)
            + estimate_tokens(ctx.enclosing_source_text)
            + estimate_tokens(ctx.static_text)
        )
        return ctx


def _l3_text(similar: list[CodeSlice], callers: list[CallSite], priors: list[PriorComment]) -> str:
    parts: list[str] = []
    if callers:
        parts.append("=== 调用方（判断影响面）===\n" + render_callers(callers))
    if similar:
        parts.append("=== 仓库既有相似实现（仅供参考项目约定，不是待审代码）===\n" + render_similar(similar))
    if priors:
        parts.append("=== 该文件历史审查意见（不要重复提出）===\n" + render_prior_comments(priors))
    return "\n\n".join(parts)


def _normalize(shares: dict[str, float]) -> dict[str, float]:
    total = sum(shares.values()) or 1.0
    return {k: v / total for k, v in shares.items()}


_TYPE_NAME_RE = None


def _type_names_in(spans: Iterable[SymbolSpan]) -> list[str]:
    """从方法源码里粗略挑出可能被引用的类型名（用于查符号索引）。"""
    global _TYPE_NAME_RE
    if _TYPE_NAME_RE is None:
        import re

        _TYPE_NAME_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")
    found: list[str] = []
    for span in spans:
        for m in _TYPE_NAME_RE.finditer(span.source):
            name = m.group(1)
            if name not in found:
                found.append(name)
    return found[:24]
