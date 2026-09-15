"""候选合并。

对应开发文档 §4.6「合并」：

> 同一文件相邻块可能对同一处产生重复候选，用 `(path, line, category)` 三元组加 ±3 行
> 窗口做归并；归并时保留置信度最高的那条，并把其他条的理由作为补充证据拼接。
"""

from __future__ import annotations

from acra.models import RawFinding

DEFAULT_WINDOW = 3

SUPPLEMENT_HEADER = "补充证据："


def merge_candidates(candidates: list[RawFinding], *, window: int = DEFAULT_WINDOW) -> list[RawFinding]:
    """按 (path, category) 分组、行号 ±window 归并。保持原有相对顺序。"""
    if not candidates:
        return []

    merged: list[RawFinding] = []
    for raw in candidates:
        target = _find_merge_target(merged, raw, window)
        if target is None:
            merged.append(raw)
            continue
        _absorb(target, raw)
    return merged


def _find_merge_target(merged: list[RawFinding], raw: RawFinding, window: int) -> RawFinding | None:
    best: RawFinding | None = None
    best_distance = window + 1
    for candidate in reversed(merged):  # 只与近邻比较，避免 O(n^2) 全扫
        if candidate.path != raw.path or candidate.category != raw.category:
            continue
        distance = abs(candidate.line - raw.line)
        if distance <= window and distance < best_distance:
            best, best_distance = candidate, distance
    return best


def _absorb(target: RawFinding, raw: RawFinding) -> None:
    """把 raw 并入 target：保留高置信度的主体，补充另一条的理由。"""
    target_confidence = target.confidence
    raw_confidence = raw.confidence

    if raw_confidence > target_confidence:
        # 交换主体：把 target 的旧内容作为补充
        _append_supplement(raw, target.title, target.body)
        target.title = raw.title
        target.body = raw.body
        target.confidence = raw_confidence
        target.severity = _higher_severity(target.severity, raw.severity)
        target.suggestion = raw.suggestion or target.suggestion
        target.evidence = list(dict.fromkeys([*raw.evidence, *target.evidence]))
        target.needs_human_judgment = target.needs_human_judgment and raw.needs_human_judgment
    else:
        _append_supplement(target, raw.title, raw.body)
        target.severity = _higher_severity(target.severity, raw.severity)
        target.evidence = list(dict.fromkeys([*target.evidence, *raw.evidence]))
        target.needs_human_judgment = target.needs_human_judgment and raw.needs_human_judgment

    merged_sources = int(target.raw.get("_merged_sources", 1)) + 1
    target.raw["_merged_sources"] = merged_sources
    target.raw.setdefault("_merged_from", []).append({"title": raw.title, "line": raw.line})


def _append_supplement(target: RawFinding, title: str, body: str) -> None:
    if SUPPLEMENT_HEADER in target.body:
        return
    snippet = f"{title}：{body}"
    if snippet in target.body:
        return
    room = 600 - len(SUPPLEMENT_HEADER) - 2 - len(snippet)
    if room < 40:  # body 上限 600 字符（文档 §5.3），放不下就不硬塞
        return
    target.body = f"{target.body}\n\n{SUPPLEMENT_HEADER}{snippet}"


_SEVERITY_ORDER = ["nit", "low", "medium", "high", "blocker"]


def _higher_severity(a: str, b: str) -> str:
    try:
        return a if _SEVERITY_ORDER.index(a) >= _SEVERITY_ORDER.index(b) else b
    except ValueError:
        return a or b
