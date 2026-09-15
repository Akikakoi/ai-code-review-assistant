"""行号映射与锚定判定的单测。

文档 §14.1：「旧行号→新行号换算、hunk 边界、多 hunk 偏移累积」。
锚定校验是整个系统防幻觉的唯一依据，因此每种 miss 原因都要有断言。
"""

from __future__ import annotations

from acra.models import DiffSet
from acra.repo.diff_parser import parse_unified_diff
from acra.repo.line_mapper import (
    build_whitelist,
    is_delete_only,
    map_old_to_new,
    nearest_added_line,
    resolve_anchor,
)

DIFF = """diff --git a/src/A.java b/src/A.java
index 1111111..2222222 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,3 +10,4 @@ class A {
-old a
-old b
-old c
+new a
+new b
+new c
+new d
@@ -30,2 +31,2 @@ class A {
-old x
-old y
+new x
+new y
"""


def _fd():
    return parse_unified_diff(DIFF)[0]


def test_whitelist_collects_all_hunks() -> None:
    fd = _fd()
    diff_set = DiffSet(
        base_sha="b" * 40,
        head_sha="h" * 40,
        merge_base_sha="m" * 40,
        files=[fd],
    )
    assert build_whitelist(diff_set) == {"src/A.java": {10, 11, 12, 13, 31, 32}}
    assert fd.added_line_numbers == {10, 11, 12, 13, 31, 32}


def test_exact_anchor_hit() -> None:
    result = resolve_anchor(_fd(), 11)
    assert result.kind == "exact"
    assert result.line == 11
    assert result.ok


def test_old_line_maps_to_new_line_via_hunk_offset() -> None:
    fd = _fd()
    # 首 hunk：new_start - old_start = 0，因此旧行号 10 → 新行号 10
    assert map_old_to_new(fd, 10) == 10
    # 次 hunk：+31 相对 -30 偏移 +1，旧行号 30 是删除行 → 新行号 31
    assert map_old_to_new(fd, 30) == 31


def test_old_line_outside_removed_range_is_not_mapped() -> None:
    # 行号 5 不在任何 hunk 的删除区间内，不做就近吸附
    assert map_old_to_new(_fd(), 5) is None


def test_anchor_accepts_mapped_line() -> None:
    result = resolve_anchor(_fd(), 30)
    assert result.kind == "mapped"
    assert result.line == 31


DIFF_WITH_CONTEXT = """diff --git a/src/A.java b/src/A.java
index 1..2 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,3 +10,3 @@
-old value
+new value
 context line
"""


def test_anchor_rejects_line_inside_hunk_but_not_added() -> None:
    """行号落在 hunk 的行区间内、但不是新增行 —— 属于存量行，必须拒绝。"""
    fd = parse_unified_diff(DIFF_WITH_CONTEXT)[0]
    # hunk 覆盖新文件 10..12，其中只有 10 是新增行，11 是上下文行
    assert fd.added_line_numbers == {10}
    assert resolve_anchor(fd, 10).kind == "exact"

    result = resolve_anchor(fd, 11)
    assert result.kind == "miss"
    assert result.detail == "line_in_hunk_but_not_added"


def test_anchor_rejects_line_outside_any_hunk() -> None:
    result = resolve_anchor(_fd(), 9999)
    assert result.kind == "miss"
    assert result.detail == "line_not_in_any_hunk"


def test_anchor_rejects_non_positive_line() -> None:
    result = resolve_anchor(_fd(), 0)
    assert result.kind == "miss"
    assert result.detail == "line<=0"


def test_nearest_added_line_only_within_window() -> None:
    fd = _fd()
    assert nearest_added_line(fd, 12, max_distance=3) == 12
    assert nearest_added_line(fd, 13, max_distance=1) == 13
    # 行号 15 最近的新增行是 13，距离 2 > 1 → 拒绝，不做就近吸附
    assert nearest_added_line(fd, 15, max_distance=1) is None
    assert nearest_added_line(fd, 15, max_distance=2) == 13


def test_delete_only_file() -> None:
    diff = """diff --git a/gone.txt b/gone.txt
index 1..2 100644
--- a/gone.txt
+++ b/gone.txt
@@ -1 +0,0 @@
-bye
"""
    assert is_delete_only(parse_unified_diff(diff)[0]) is True
    assert is_delete_only(_fd()) is False
