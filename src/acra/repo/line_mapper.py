"""行号映射与白名单。

对应开发文档 §4.3「行号陷阱」：

- `added_line_numbers` 既是给模型的锚点约束，也是 validator 判定幻觉的唯一依据；
- validator 采用保守策略，只接受落在 `added_line_numbers` 内的行号；
- 若模型给的是旧文件行号，用 hunk 偏移做一次映射，映射后不在白名单内仍然丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from acra.models import DiffSet, FileDiff

AnchorKind = Literal["exact", "mapped", "miss"]


@dataclass(slots=True)
class AnchorResult:
    kind: AnchorKind
    line: int | None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in ("exact", "mapped")


def build_whitelist(diff_set: DiffSet) -> dict[str, set[int]]:
    """path → 可评论行号集合。"""
    return diff_set.whitelist()


def map_old_to_new(file_diff: FileDiff, old_line: int) -> int | None:
    """旧文件行号 → 新文件行号。

    仅当目标行落在某个 hunk 的删除区间内时才做偏移换算；换算结果必须落在白名单内，
    否则返回 None（不做"就近吸附"，那是噪音的入口）。
    """
    whitelist = file_diff.added_line_numbers
    for h in file_diff.hunks:
        if h.old_lines <= 0:
            continue
        if h.old_start <= old_line <= h.old_start + h.old_lines - 1:
            candidate = old_line + h.old_to_new_offset()
            if candidate in whitelist:
                return candidate
    return None


def resolve_anchor(file_diff: FileDiff, line: int) -> AnchorResult:
    """判定一条结论的行号能否锚定。"""
    if line <= 0:
        return AnchorResult("miss", None, "line<=0")

    whitelist = file_diff.added_line_numbers
    if line in whitelist:
        return AnchorResult("exact", line, "in added_line_numbers")

    mapped = map_old_to_new(file_diff, line)
    if mapped is not None:
        return AnchorResult("mapped", mapped, f"old:{line}->new:{mapped}")

    # 区分"落在这个文件的其它行"和"完全不属于本文件"
    for h in file_diff.hunks:
        if h.new_start <= line <= h.new_start + max(h.new_lines, 1) - 1:
            return AnchorResult("miss", None, "line_in_hunk_but_not_added")

    return AnchorResult("miss", None, "line_not_in_any_hunk")


def nearest_added_line(file_diff: FileDiff, line: int, max_distance: int = 3) -> int | None:
    """取最近的新增行。供阶段二 Verify 修正行号时使用，阶段一不参与校验决策。"""
    whitelist = file_diff.added_line_numbers
    if not whitelist:
        return None
    best = min(whitelist, key=lambda x: (abs(x - line), x))
    return best if abs(best - line) <= max_distance else None


def is_delete_only(file_diff: FileDiff) -> bool:
    """纯删除（没有可评论的新增行）。"""
    return file_diff.added_count == 0 and file_diff.removed_count > 0
