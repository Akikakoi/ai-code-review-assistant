"""unified diff → FileDiff / Hunk。

对应开发文档 §4.3。输入来自
`git diff --find-renames --find-copies --unified=0 <merge_base>..<head>`。

设计取舍：

- 只解析 `--unified=0` 的产物（无上下文行），邻接上下文由 context_builder 按需另取，
  这样上下文预算完全可控；
- 以 `---` / `+++` 行作为路径的权威来源，`diff --git` 头仅作兜底（文件名含空格时
  该行存在歧义）；
- 遇到无法识别的行不抛异常，只做保守处理 —— diff 解析失败会让整条链路失效，
  容错优于严格。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from acra.models import ChangeType, DiffSet, FileDiff, Hunk

_HUNK_RE = re.compile(r"^@@ -(?P<os>\d+)(?:,(?P<ol>\d+))? \+(?P<ns>\d+)(?:,(?P<nl>\d+))? @@(?P<tail>.*)$")

_DIFF_GIT_RE = re.compile(r"^diff --git (?P<a>.+) (?P<b>b/.+)$")

_C_ESCAPES = {
    b'"': b'"',
    b"\\": b"\\",
    b"a": b"\a",
    b"b": b"\b",
    b"f": b"\f",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"v": b"\v",
}


def unquote_git_path(raw: str) -> str:
    """还原 git 对特殊字符路径的 C 风格引号封装。

    git 输出的转义是**字节级**的（非 ASCII 路径会逐字节写成八进制，如
    `"\\346\\226\\207.java"`）。因此必须按字节重组后再用 UTF-8 解码 ——
    逐字符 `chr(int(digits, 8))` 会得到 `æ–‡` 这类 mojibake，中文文件名直接损坏。
    """
    raw = raw.strip()
    if not (len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'):
        return raw

    body = raw[1:-1]
    buf = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                j = i + 1
                digits = ""
                while j < len(body) and len(digits) < 3 and body[j] in "01234567":
                    digits += body[j]
                    j += 1
                buf.append(int(digits, 8))
                i = j
                continue
            escaped = _C_ESCAPES.get(nxt.encode("ascii", "ignore"))
            if escaped is not None:
                buf.extend(escaped)
                i += 2
                continue
            buf.extend(nxt.encode("utf-8"))
            i += 2
            continue
        buf.extend(ch.encode("utf-8"))
        i += 1
    return buf.decode("utf-8", "replace")


def _clean_path(raw: str, *, strip_prefix: bool = True) -> str | None:
    """`a/foo.java` → `foo.java`；`/dev/null` → None。"""
    if raw is None:
        return None
    path = unquote_git_path(raw)
    # 某些 diff 工具会在路径后附加时间戳
    if "\t" in path:
        path = path.split("\t", 1)[0]
    path = path.strip()
    if not path or path == "/dev/null":
        return None
    if strip_prefix and (path.startswith("a/") or path.startswith("b/")):
        path = path[2:]
    return path


def _split_diff_git_header(line: str) -> tuple[str | None, str | None]:
    """从 `diff --git a/x b/y` 还原两侧路径（兜底路径，存在歧义）。"""
    m = _DIFF_GIT_RE.match(line)
    if not m:
        return None, None
    left, right = m.group("a"), m.group("b")
    # `a/x` 与 `b/y` 之间以 " b/" 分隔；同名文件取最后一次出现以避免空格误判
    if not left.startswith('"'):
        idx = left.rfind(" b/")
        if idx != -1:
            left = left[:idx]
    return _clean_path(left), _clean_path(right)


def _iter_lines(text: str) -> Iterator[str]:
    """按 \\n 切分，且仅去掉最后一个由结尾换行产生的空串。

    刻意不用 str.splitlines()：它会在 \\x0b / \\x0c / \\u2028 等字符处断开，
    而源码中可能出现这些字符。
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return iter(lines)


class _FileAccumulator:
    __slots__ = ("is_new", "is_deleted", "is_renamed", "is_binary", "old_path", "new_path")

    def __init__(self) -> None:
        self.is_new = False
        self.is_deleted = False
        self.is_renamed = False
        self.is_binary = False
        self.old_path: str | None = None
        self.new_path: str | None = None

    def change_type(self) -> ChangeType:
        if self.is_binary and not self.is_new and not self.is_deleted:
            return ChangeType.BINARY
        if self.is_new:
            return ChangeType.ADD
        if self.is_deleted:
            return ChangeType.DELETE
        if self.is_renamed:
            return ChangeType.RENAME
        return ChangeType.MODIFY


def parse_unified_diff(text: str) -> list[FileDiff]:
    """把 unified diff 文本解析为 FileDiff 列表。"""
    files: list[FileDiff] = []
    acc: _FileAccumulator | None = None
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    in_hunk = False
    new_ln = 0
    old_ln = 0

    def flush() -> None:
        nonlocal acc, cur, hunk, in_hunk
        if acc is not None and cur is not None:
            path = acc.new_path or acc.old_path
            if path:
                old_path = acc.old_path
                cur.path = path
                cur.old_path = old_path if old_path != path else None
                cur.change_type = acc.change_type()
                cur.is_binary = acc.is_binary
                files.append(cur)
        acc = None
        cur = None
        hunk = None
        in_hunk = False

    for line in _iter_lines(text):
        # git 在 CRLF 文件上会把 \r 留在内容里，统一去掉以免污染行内容
        if line.endswith("\r"):
            line = line[:-1]

        if line.startswith("diff --git "):
            flush()
            acc = _FileAccumulator()
            a_path, b_path = _split_diff_git_header(line)
            acc.old_path, acc.new_path = a_path, b_path
            cur = FileDiff(path=b_path or a_path or "", change_type=ChangeType.MODIFY)
            continue

        if acc is None or cur is None:
            continue

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                continue
            old_start = int(m.group("os"))
            old_lines = int(m.group("ol")) if m.group("ol") is not None else 1
            new_start = int(m.group("ns"))
            new_lines = int(m.group("nl")) if m.group("nl") is not None else 1
            hunk = Hunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                header=m.group("tail").strip(),
            )
            cur.hunks.append(hunk)
            in_hunk = True
            old_ln = old_start
            new_ln = new_start
            continue

        if not in_hunk:
            if line.startswith("new file mode"):
                acc.is_new = True
            elif line.startswith("deleted file mode"):
                acc.is_deleted = True
            elif line.startswith("rename from "):
                acc.is_renamed = True
                acc.old_path = _clean_path(line[len("rename from ") :])
            elif line.startswith("rename to "):
                acc.is_renamed = True
                acc.new_path = _clean_path(line[len("rename to ") :])
            elif line.startswith("copy from "):
                acc.old_path = _clean_path(line[len("copy from ") :])
            elif line.startswith("copy to "):
                acc.new_path = _clean_path(line[len("copy to ") :])
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                acc.is_binary = True
            elif line.startswith("--- "):
                p = _clean_path(line[4:])
                if p is None:
                    acc.old_path = None
                    acc.is_new = True
                else:
                    acc.old_path = p
            elif line.startswith("+++ "):
                p = _clean_path(line[4:])
                if p is None:
                    acc.new_path = None
                    acc.is_deleted = True
                else:
                    acc.new_path = p
            continue

        # ---- hunk 内部 ----
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        if line.startswith("+"):
            if hunk is not None:
                hunk.added.append((new_ln, line[1:]))
            new_ln += 1
        elif line.startswith("-"):
            if hunk is not None:
                hunk.removed.append((old_ln, line[1:]))
            old_ln += 1
        else:
            # 上下文行（" xxx" 或异常的空行）
            new_ln += 1
            old_ln += 1

    flush()
    return files


def build_diff_set(
    raw_diff: str,
    *,
    base_sha: str,
    head_sha: str,
    merge_base_sha: str,
) -> DiffSet:
    return DiffSet(
        base_sha=base_sha,
        head_sha=head_sha,
        merge_base_sha=merge_base_sha,
        files=parse_unified_diff(raw_diff),
    )
