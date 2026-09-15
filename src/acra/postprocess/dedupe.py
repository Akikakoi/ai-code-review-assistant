"""第 6 步：重复校验 / 归并。

与 `engine/merge.py` 的区别：merge 发生在**锚定之前**，作用于原始候选，按模型给的
行号做 ±3 归并；dedupe 发生在**锚定之后**，作用于已确认可锚定的结论。

这一步不是冗余的：锚定会把模型给出的旧文件行号映射成新文件行号（`line_mapper`），
映射之后原本相距很远的两条结论可能落到同一行上 —— 这是 merge 阶段看不到的重复。
"""

from __future__ import annotations

from acra.models import DropRecord, Finding

DEFAULT_WINDOW = 3


def dedupe_findings(
    findings: list[Finding],
    *,
    window: int = DEFAULT_WINDOW,
) -> tuple[list[Finding], list[DropRecord]]:
    """按 (path, line±window, category) 归并，保留 score 最高的一条。"""
    ordered = sorted(findings, key=lambda f: f.sort_key())
    kept: list[Finding] = []
    dropped: list[DropRecord] = []

    for finding in ordered:
        target = _find_target(kept, finding, window)
        if target is None:
            kept.append(finding)
            continue
        target.merged_sources += finding.merged_sources
        if finding.evidence:
            target.evidence = list(dict.fromkeys([*target.evidence, *finding.evidence]))
        _append_supplement(target, finding)
        dropped.append(
            DropRecord(
                "6_duplicate",
                f"merged_into:{target.path}:{target.line}:{target.category}",
                finding.to_dict(),
            )
        )
    return kept, dropped


def _find_target(kept: list[Finding], finding: Finding, window: int) -> Finding | None:
    best: Finding | None = None
    best_distance = window + 1
    for candidate in kept:
        if candidate.path != finding.path or candidate.category != finding.category:
            continue
        distance = abs(candidate.line - finding.line)
        if distance <= window and distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _append_supplement(target: Finding, other: Finding) -> None:
    header = "补充证据："
    if header in target.body:
        return
    snippet = f"{other.title}（{other.path}:{other.line}）"
    if snippet in target.body or len(target.body) + len(header) + len(snippet) > 600:
        return
    target.body = f"{target.body}\n\n{header}{snippet}"
