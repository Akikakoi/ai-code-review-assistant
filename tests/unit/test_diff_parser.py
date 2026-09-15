"""diff 解析的单测。

覆盖文档 §14.1 点名的用例重点：rename、新增/删除文件、纯删除 hunk、无换行结尾、CRLF、
二进制、大文件，以及对 `diff --git` 头歧义（文件名含空格）与 git 引号路径的处理。
"""

from __future__ import annotations

from acra.models import ChangeType
from acra.repo.diff_parser import parse_unified_diff, unquote_git_path

SIMPLE = """diff --git a/src/A.java b/src/A.java
index 1111111..2222222 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,0 +11,3 @@ class A {
+    int x = 1;
+    int y = 2;
+    int z = 3;
@@ -20 +23,0 @@ class A {
-    int old = 0;
"""

NEW_FILE = """diff --git a/src/New.java b/src/New.java
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/New.java
@@ -0,0 +1,2 @@
+package x;
+class New {}
"""

DELETED_FILE = """diff --git a/src/Gone.java b/src/Gone.java
deleted file mode 100644
index 4444444..0000000
--- a/src/Gone.java
+++ /dev/null
@@ -1,2 +0,0 @@
-package x;
-class Gone {}
"""

RENAMED_FILE = """diff --git a/src/Old.java b/src/pkg/New.java
similarity index 90%
rename from src/Old.java
rename to src/pkg/New.java
index 5555555..6666666 100644
--- a/src/Old.java
+++ b/src/pkg/New.java
@@ -3 +3 @@
-    int a = 1;
+    int a = 2;
"""

BINARY_FILE = """diff --git a/img/logo.png b/img/logo.png
index 7777777..8888888 100644
Binary files a/img/logo.png and b/img/logo.png differ
"""

BINARY_NEW = """diff --git a/img/new.png b/img/new.png
new file mode 100644
index 0000000..9999999
Binary files /dev/null and b/img/new.png differ
"""

NO_NEWLINE = """diff --git a/tail.txt b/tail.txt
index aaaaaaa..bbbbbbb 100644
--- a/tail.txt
+++ b/tail.txt
@@ -1 +1 @@
-old tail
\\ No newline at end of file
+new tail
\\ No newline at end of file
"""

CRLF_CONTENT = "diff --git a/crlf.txt b/crlf.txt\nindex ccccccc..ddddddd 100644\n--- a/crlf.txt\n+++ b/crlf.txt\n@@ -2 +2 @@\n-B\r\n+B\r\n"

QUOTED_PATH = (
    'diff --git "a/src/\\346\\226\\207\\344\\273\\266.java" "b/src/\\346\\226\\207\\344\\273\\266.java"\n'
    "index eeeeee..ffffff 100644\n"
    '--- "a/src/\\346\\226\\207\\344\\273\\266.java"\n'
    '+++ "b/src/\\346\\226\\207\\344\\273\\266.java"\n'
    "@@ -1 +1 @@\n-old\n+new\n"
)

SPACE_IN_NAME = """diff --git a/src/my file.java b/src/my file.java
index 0a0a0a0..0b0b0b0 100644
--- a/src/my file.java
+++ b/src/my file.java
@@ -1 +1 @@
-a
+b
"""

MODE_ONLY = """diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
"""


def test_added_line_numbers_are_exact() -> None:
    files = parse_unified_diff(SIMPLE)
    assert len(files) == 1
    fd = files[0]
    assert fd.path == "src/A.java"
    assert fd.change_type is ChangeType.MODIFY
    assert fd.added_line_numbers == {11, 12, 13}
    assert [text for _, text in fd.hunks[0].added] == [
        "    int x = 1;",
        "    int y = 2;",
        "    int z = 3;",
    ]


def test_multiple_hunks_accumulate_offsets_independently() -> None:
    fd = parse_unified_diff(SIMPLE)[0]
    assert len(fd.hunks) == 2
    assert fd.hunks[0].new_start == 11
    assert fd.hunks[0].new_lines == 3
    # 第二个 hunk 只删不加，因此没有可评论行
    assert fd.hunks[1].added == []
    assert fd.hunks[1].removed == [(20, "    int old = 0;")]


def test_new_file() -> None:
    fd = parse_unified_diff(NEW_FILE)[0]
    assert fd.change_type is ChangeType.ADD
    assert fd.old_path is None
    assert fd.added_line_numbers == {1, 2}


def test_deleted_file_keeps_old_path() -> None:
    fd = parse_unified_diff(DELETED_FILE)[0]
    assert fd.change_type is ChangeType.DELETE
    assert fd.path == "src/Gone.java"
    assert fd.added_line_numbers == set()
    assert fd.removed_line_numbers == {1, 2}


def test_renamed_file_keeps_both_paths() -> None:
    fd = parse_unified_diff(RENAMED_FILE)[0]
    assert fd.change_type is ChangeType.RENAME
    assert fd.path == "src/pkg/New.java"
    assert fd.old_path == "src/Old.java"
    assert fd.added_line_numbers == {3}


def test_binary_modify() -> None:
    fd = parse_unified_diff(BINARY_FILE)[0]
    assert fd.is_binary is True
    assert fd.change_type is ChangeType.BINARY
    assert fd.added_line_numbers == set()


def test_binary_new_file_is_add() -> None:
    fd = parse_unified_diff(BINARY_NEW)[0]
    assert fd.is_binary is True
    assert fd.change_type is ChangeType.ADD


def test_no_newline_marker_is_not_treated_as_content() -> None:
    fd = parse_unified_diff(NO_NEWLINE)[0]
    assert fd.added_line_numbers == {1}
    assert fd.hunks[0].added == [(1, "new tail")]
    assert fd.hunks[0].removed == [(1, "old tail")]


def test_crlf_content_is_normalized_without_shifting_line_numbers() -> None:
    fd = parse_unified_diff(CRLF_CONTENT)[0]
    assert fd.added_line_numbers == {2}
    assert fd.hunks[0].added == [(2, "B")]  # 行尾 \r 被剥掉，行号不受影响


def test_quoted_unicode_path_is_unescaped() -> None:
    fd = parse_unified_diff(QUOTED_PATH)[0]
    assert fd.path == "src/文件.java"
    assert fd.added_line_numbers == {1}


def test_filename_with_space() -> None:
    fd = parse_unified_diff(SPACE_IN_NAME)[0]
    assert fd.path == "src/my file.java"


def test_mode_only_change_has_no_commentable_lines() -> None:
    fd = parse_unified_diff(MODE_ONLY)[0]
    assert fd.hunks == []
    assert fd.added_line_numbers == set()


def test_empty_input() -> None:
    assert parse_unified_diff("") == []


def test_garbage_input_does_not_raise() -> None:
    assert parse_unified_diff("not a diff at all\nrandom text\n") == []


def test_large_file_diff_is_linear_and_correct() -> None:
    lines = ["diff --git a/Big.java b/Big.java", "index 1..2 100644",
             "--- a/Big.java", "+++ b/Big.java", "@@ -0,0 +1,5000 @@"]
    lines += [f"+line {i}" for i in range(1, 5001)]
    fd = parse_unified_diff("\n".join(lines))[0]
    assert fd.added_count == 5000
    assert min(fd.added_line_numbers) == 1
    assert max(fd.added_line_numbers) == 5000


def test_unquote_git_path_plain_passthrough() -> None:
    assert unquote_git_path("src/A.java") == "src/A.java"
