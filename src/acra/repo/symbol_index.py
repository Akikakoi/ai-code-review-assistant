"""基于 tree-sitter 的符号定位与索引。

对应开发文档 §4.4（L2 上下文：变更所在方法的完整源码、imports）、§7.5（符号索引）。

阶段一实际使用的能力：

- `find_enclosing_spans`：给定文件源码与变更行集合，返回变更行所属的方法/类源码区间 ——
  这是 L2 的主体，也是"按语法边界分块"（§7.4）的依据；
- `extract_imports`：L2 的 import 列表。

`SymbolIndex`（跨文件符号表与反向引用）在阶段二启用，此处已实现定义抽取与签名查询，
由 `ACRA_L2_TYPE_SIGNATURES` 开关控制是否注入提示词。

降级：grammar 缺失或解析失败时退回"以变更行为中心的开窗"策略，并在 `degraded` 中标注，
不让链路静默丢失上下文。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache

from acra.models import Symbol, SymbolSpan, TypeSig

# ---------------------------------------------------------------------------- 语言

_EXT_LANG: dict[str, str] = {
    ".java": "java",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "tsx",  # tsx grammar 对 JS 是宽松超集，阶段一够用
    ".jsx": "tsx",
    ".mjs": "tsx",
    ".cjs": "tsx",
    ".py": "python",
    ".pyi": "python",
}

#: 语言 → 容器节点类型（可承载"方法"语义的节点）
CONTAINERS: dict[str, dict[str, tuple[str, ...]]] = {
    "java": {
        "method": ("method_declaration", "constructor_declaration", "compact_constructor_declaration"),
        "type": (
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        ),
        "other": ("static_initializer", "lambda_expression"),
    },
    "typescript": {
        "method": (
            "method_definition",
            "function_declaration",
            "function_expression",
            "generator_function_declaration",
            "arrow_function",
            "method_signature",
            "abstract_method_signature",
        ),
        "type": (
            "class_declaration",
            "abstract_class_declaration",
            "interface_declaration",
            "enum_declaration",
            "type_alias_declaration",
            "internal_module",
        ),
        "other": ("public_field_definition", "required_parameter"),
    },
}
CONTAINERS["tsx"] = CONTAINERS["typescript"]
CONTAINERS["python"] = {
    "method": ("function_definition", "lambda"),
    "type": ("class_definition",),
    "other": ("decorated_definition",),
}

_METHOD_KINDS = {"method", "constructor", "function", "lambda"}
_TYPE_KINDS = {"class", "interface", "enum", "record"}

#: 超过该字节数不再走 tree-sitter，改为开窗降级
MAX_PARSE_BYTES = 2_000_000

#: 节点类型 → 对外暴露的 kind
_KIND_MAP: dict[str, str] = {
    "method_declaration": "method",
    "constructor_declaration": "constructor",
    "compact_constructor_declaration": "constructor",
    "method_definition": "method",
    "method_signature": "method",
    "abstract_method_signature": "method",
    "function_declaration": "function",
    "function_expression": "function",
    "generator_function_declaration": "function",
    "arrow_function": "lambda",
    "lambda_expression": "lambda",
    "static_initializer": "static_initializer",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "interface",
    "type_alias_declaration": "class",
    "function_definition": "function",
    "class_definition": "class",
    "decorated_definition": "function",
}


def language_for_path(path: str) -> str | None:
    idx = path.rfind(".")
    if idx == -1:
        return None
    return _EXT_LANG.get(path[idx:].lower())


@lru_cache(maxsize=8)
def _load_language(lang: str):
    if lang == "java":
        import tree_sitter_java
        from tree_sitter import Language

        return Language(tree_sitter_java.language())
    if lang == "python":
        try:
            import tree_sitter_python
            from tree_sitter import Language
        except ImportError:  # 未安装时降级为开窗，而不是报错
            return None
        return Language(tree_sitter_python.language())
    if lang in ("typescript", "tsx"):
        import tree_sitter_typescript
        from tree_sitter import Language

        if lang == "typescript":
            return Language(tree_sitter_typescript.language_typescript())
        return Language(tree_sitter_typescript.language_tsx())
    return None


@lru_cache(maxsize=8)
def _load_parser(lang: str):
    from tree_sitter import Parser

    language = _load_language(lang)
    if language is None:
        return None
    try:
        return Parser(language)  # tree-sitter >= 0.22
    except TypeError:  # pragma: no cover - 老版本 API
        parser = Parser()
        parser.set_language(language)
        return parser


_SELF_TEST_SOURCE: dict[str, bytes] = {
    "java": b"class A { void f() { int x = 1; } }",
    "typescript": b"class A { f(): void {} }",
    "tsx": b"const A = () => 1;",
    "python": b"class A:\n    def f(self):\n        x = 1\n",
}


@lru_cache(maxsize=8)
def _self_test(lang: str) -> bool:
    parser = _load_parser(lang)
    probe = _SELF_TEST_SOURCE.get(lang)
    if parser is None or probe is None:
        return False
    try:
        tree = parser.parse(probe)
        root = tree.root_node
        # 故意读这些字段：绑定与 grammar 的 ABI 不匹配正是从这里开始崩（见 ADR 0007）
        _ = (
            root.type,
            root.start_byte,
            root.end_byte,
            root.start_point.row,
            root.end_point.column,
        )
        _ = [node.type for node in root.children]
        return True
    except Exception:  # pragma: no cover - 取决于安装环境
        return False


def tree_sitter_available(lang: str) -> bool:
    """探测某语言的解析能力，并且真的走一遍解析。

    只判断"grammar 能否加载"是不够的：**Python 绑定与 grammar 的 ABI 不匹配**时，
    加载会成功、小输入也能跑，但在稍大的文件上访问 `start_point` / 字节偏移会读越界内存，
    进程直接 access violation —— 那是 try/except 兜不住的。所以这里解析一个小样本并
    触碰那些会踩到 ABI 差异的字段，把问题尽早暴露成"该语言降级"，而不是随机的进程崩溃。
    """
    return _self_test(lang)


# ---------------------------------------------------------------------------- 区间与签名


@dataclass(slots=True)
class _RawSpan:
    kind: str
    start_line: int
    end_line: int
    name: str
    start_byte: int
    end_byte: int
    parent_name: str | None = None
    is_method_like: bool = False


def _node_name(node, source: bytes) -> str:
    for field in ("name", "declarator"):
        try:
            child = node.child_by_field_name(field)
        except Exception:
            child = None
        if child is not None:
            text = source[child.start_byte : child.end_byte].decode("utf-8", "replace").strip()
            if field == "declarator":
                m = re.search(r"([A-Za-z_$][\w$]*)\s*=", text)
                if m:
                    return m.group(1)
                continue
            if text:
                return text.split("(")[0].strip()
    return ""


_HEADER_SPLIT_RE = re.compile(r"[{;]")


def _signature_of(text: str) -> str:
    """取节点头部：到第一个 `{` 或 `;` 为止，去掉注释与多余空白。"""
    cleaned = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    cleaned = re.sub(r"//[^\n]*", " ", cleaned)
    m = _HEADER_SPLIT_RE.search(cleaned)
    head = cleaned[: m.start()] if m else cleaned
    head = " ".join(head.split())
    return head[:300]


# ---------------------------------------------------------------------------- 解析


def _iter_containers(root, lang: str) -> Iterator[tuple[object, str]]:
    spec = CONTAINERS.get(lang)
    if not spec:
        return
    kind_of_type: dict[str, str] = {}
    for group, types in spec.items():
        for t in types:
            kind_of_type[t] = "method" if group in ("method", "other") and t not in (
                "static_initializer",
                "public_field_definition",
                "required_parameter",
            ) else ("type" if group == "type" else "other")

    stack = [root]
    while stack:
        node = stack.pop()
        group = kind_of_type.get(node.type)
        if group:
            yield node, group
        stack.extend(node.children)


def _collect_spans(source: bytes, lang: str) -> list[_RawSpan]:
    parser = _load_parser(lang)
    if parser is None:
        return []
    try:
        tree = parser.parse(source)
    except Exception:
        return []

    spans: list[_RawSpan] = []
    # 先建好父子关系，便于回填 parent_name
    for node, _group in _iter_containers(tree.root_node, lang):
        kind = _KIND_MAP.get(node.type, node.type)
        start_line = node.start_point.row + 1
        end_line = node.end_point.row + 1
        if node.end_point.column == 0 and end_line > start_line:
            end_line -= 1
        spans.append(
            _RawSpan(
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                name=_node_name(node, source),
                start_byte=node.start_byte,
                end_byte=node.end_byte,
                is_method_like=kind in _METHOD_KINDS,
            )
        )

    # 回填最近的外层类型名
    spans.sort(key=lambda s: (s.start_byte, -s.end_byte))
    for span in spans:
        if span.kind in _TYPE_KINDS:
            continue
        outer = [
            c
            for c in spans
            if c.kind in _TYPE_KINDS and c.start_byte <= span.start_byte and c.end_byte >= span.end_byte
        ]
        if outer:
            span.parent_name = min(outer, key=lambda c: c.end_byte - c.start_byte).name or None
    return spans


def _to_symbol_span(span: _RawSpan, lines: list[str]) -> SymbolSpan:
    src = "\n".join(lines[span.start_line - 1 : span.end_line])
    return SymbolSpan(
        name=span.name or f"<{span.kind}@{span.start_line}>",
        kind=span.kind,
        start_line=span.start_line,
        end_line=span.end_line,
        signature=_signature_of(src),
        parent=span.parent_name,
        source=src,
    )


def window_span(source: str, line_numbers: Iterable[int], radius: int = 60) -> SymbolSpan:
    """树解析不可用时的降级方案：以变更行为中心开窗。"""
    lines = source.split("\n")
    targets = [ln for ln in line_numbers if ln > 0] or [1]
    start = max(1, min(targets) - radius)
    end = min(len(lines), max(targets) + radius)
    return SymbolSpan(
        name="<window>",
        kind="window",
        start_line=start,
        end_line=end,
        signature="",
        source="\n".join(lines[start - 1 : end]),
        truncated=True,
    )


def find_enclosing_spans(
    source: str,
    line_numbers: Iterable[int],
    *,
    lang: str | None = None,
    path: str = "",
    max_spans: int = 8,
) -> tuple[list[SymbolSpan], list[str]]:
    """返回 (变更行所属的语法区间列表, 降级说明)。

    优先返回方法级区间；若只有类型级，则返回类型区间。同一区间只返回一次。
    """
    lines = source.split("\n")
    targets = sorted({ln for ln in line_numbers if ln > 0})
    if not targets:
        return [], []

    lang = lang or language_for_path(path)
    degraded: list[str] = []

    if lang and tree_sitter_available(lang):
        spans = _collect_spans(source.encode("utf-8"), lang)
        if spans:
            picked: list[_RawSpan] = []
            for line in targets:
                inside = [s for s in spans if s.start_line <= line <= s.end_line]
                if not inside:
                    continue
                method_like = [s for s in inside if s.is_method_like]
                pool = method_like or [s for s in inside if s.kind in _TYPE_KINDS] or inside
                chosen = min(pool, key=lambda s: (s.end_line - s.start_line, -s.start_byte))
                if chosen not in picked:
                    picked.append(chosen)
            if picked:
                picked.sort(key=lambda s: s.start_line)
                return [_to_symbol_span(s, lines) for s in picked[:max_spans]], degraded
            degraded.append("no_enclosing_symbol")
        else:
            degraded.append("tree_sitter_parse_empty")
    else:
        degraded.append(f"grammar_unavailable:{lang or 'unknown'}")

    return [window_span(source, targets)], degraded


# ---------------------------------------------------------------------------- imports

_IMPORT_PATTERNS: dict[str, re.Pattern[str]] = {
    "java": re.compile(r"^\s*(?:package\s+[\w.]+\s*;|import\s+(?:static\s+)?[\w.*]+\s*;)", re.M),
    "typescript": re.compile(r"^\s*(?:import\s.+?;?|(?:const|let|var)\s+\w+\s*=\s*require\(.+?\);?)\s*$", re.M),
    "tsx": re.compile(r"^\s*(?:import\s.+?;?|(?:const|let|var)\s+\w+\s*=\s*require\(.+?\);?)\s*$", re.M),
    # 注意：行内只能用 [ \t]，不能写 \s —— \s 会吃掉换行，把相邻两条 import 粘成一条
    "python": re.compile(r"^[ \t]*(?:import[ \t]+[\w., \t]+|from[ \t]+[\w.]+[ \t]+import[ \t]+.+)$", re.M),
}


def extract_imports(source: str, lang: str | None, *, limit: int = 60) -> list[str]:
    """抽取 import / package 语句。纯正则即可，无需 AST。"""
    if not lang:
        return []
    pattern = _IMPORT_PATTERNS.get(lang)
    if not pattern:
        return []
    out: list[str] = []
    for m in pattern.finditer(source):
        text = " ".join(m.group(0).split())
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------- 符号索引


class SymbolIndex:
    """轻量跨文件符号索引（阶段二完整启用，此处提供定义抽取与签名查询）。"""

    def __init__(self) -> None:
        self._symbols: dict[str, Symbol] = {}
        self._by_file: dict[str, list[Symbol]] = {}

    def add_file(self, path: str, source: str) -> list[Symbol]:
        lang = language_for_path(path)
        if not lang or not tree_sitter_available(lang):
            return []
        lines = source.split("\n")
        out: list[Symbol] = []
        for span in _collect_spans(source.encode("utf-8"), lang):
            if span.kind not in _TYPE_KINDS and not span.is_method_like:
                continue
            text = "\n".join(lines[span.start_line - 1 : span.end_line])
            qualified = f"{span.parent_name}.{span.name}" if span.parent_name else span.name
            symbol = Symbol(
                name=span.name,
                qualified_name=qualified,
                kind=span.kind,
                path=path,
                start_line=span.start_line,
                end_line=span.end_line,
                signature=_signature_of(text),
            )
            out.append(symbol)
            self._symbols.setdefault(qualified, symbol)
            self._symbols.setdefault(span.name, symbol)
        self._by_file[path] = out
        return out

    def symbols_in(self, path: str) -> list[Symbol]:
        return self._by_file.get(path, [])

    def signature_of(self, name: str) -> Symbol | None:
        return self._symbols.get(name)

    def type_signatures(self, names: Iterable[str], *, limit: int = 12) -> list[TypeSig]:
        """被引用类型的签名（不含实现体）。文档 §4.4 referenced_types。"""
        out: list[TypeSig] = []
        for name in names:
            symbol = self._symbols.get(name)
            if symbol is None or symbol.kind not in _TYPE_KINDS:
                continue
            out.append(TypeSig(name=symbol.name, path=symbol.path, signature=symbol.signature))
            if len(out) >= limit:
                break
        return out

    def __len__(self) -> int:
        return len(self._symbols)


# ---------------------------------------------------------------------------- 仓库文件索引

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+(?:\.\*)?)\s*;")
_TS_MODULE_RE = re.compile(r"""(?:from|require\()\s*['"]([^'"]+)['"]""")
_PY_FROM_RE = re.compile(r"^[ \t]*from[ \t]+(?P<module>[\w.]*)[ \t]+import[ \t]+")
_PY_BARE_RE = re.compile(r"^[ \t]*import[ \t]+(?P<modules>[\w., \t]+)$")
_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_INDEX_FILES = ("index.ts", "index.tsx", "index.js", "index.mjs")


class RepoFileIndex:
    """仓库文件清单 + `import` → 文件路径的解析。

    存在的唯一理由：**按需**把"被引用的类型定义文件"喂给符号索引。
    全量索引一个仓库既慢又没必要 —— 真正影响判断的只有当前方法签名里出现的那些类型
    （文档 §7.5：轻量符号索引）。

    Java 有个容易漏的点：**同包类型不需要 import**，所以除了 import 解析结果，
    还要把同目录的兄弟文件作为候选。
    """

    def __init__(self, files: Iterable[str]) -> None:
        self.files = [f.replace("\\", "/") for f in files]
        self._file_set = set(self.files)
        self._by_module: dict[str, str] = {}
        self._by_dir: dict[str, list[str]] = {}
        self._by_package: dict[str, list[str]] = {}

        for path in self.files:
            directory, _, name = path.rpartition("/")
            self._by_dir.setdefault(directory, []).append(path)

            if path.endswith((".py", ".pyi")):
                # Python 的 from a.b import C 同样是全限定名，与文件路径差一层源码根
                stem = path.rsplit(".", 1)[0]
                if path.endswith("/__init__.py") or path.endswith("/__init__.pyi"):
                    stem = stem.rpartition("/")[0]
                segments = stem.split("/")
                for i in range(len(segments) - 1):
                    self._by_module.setdefault(".".join(segments[i:]), path)
                continue

            if not path.endswith(".java"):
                if "." in name:
                    self._by_module.setdefault(path.rsplit(".", 1)[0], path)
                continue

            # Java 的 import 用全限定名（com.example.order.Order），而文件路径带源码根
            # （src/main/java/com/example/order/Order.java）。两边不可能直接相等，
            # 因此把路径的**所有后缀**都登记为候选名 —— 这正是"源码根不固定"的正解。
            segments = path[:-5].split("/")
            for i in range(len(segments) - 1):
                key = ".".join(segments[i:])
                if "." in key:
                    self._by_module.setdefault(key, path)
            for i in range(len(segments) - 1):
                self._by_package.setdefault("/".join(segments[i:-1]), []).append(path)

    def resolve(self, imports: Iterable[str], from_path: str, *, limit: int = 8) -> list[str]:
        """把 import 语句解析成候选文件路径（去重、按语句顺序）。"""
        from_dir = from_path.replace("\\", "/").rpartition("/")[0]
        out: list[str] = []
        for stmt in imports:
            for candidate in self._resolve_one(stmt, from_dir):
                if candidate == from_path or candidate in out:
                    continue
                if candidate not in self._file_set:
                    continue
                out.append(candidate)
                if len(out) >= limit:
                    return out
        return out

    def siblings(self, from_path: str, *, limit: int = 5) -> list[str]:
        """同目录的兄弟文件（Java 同包类型不走 import）。"""
        directory = from_path.replace("\\", "/").rpartition("/")[0]
        return [p for p in self._by_dir.get(directory, []) if p != from_path][:limit]

    def _resolve_one(self, stmt: str, from_dir: str) -> list[str]:
        java = _JAVA_IMPORT_RE.match(stmt)
        if java:
            fqn = java.group(2)
            if fqn.endswith(".*"):
                return list(self._by_package.get(fqn[:-2].replace(".", "/"), []))
            hit = self._by_module.get(fqn)
            if hit:
                return [hit]
            if java.group(1):
                # import static a.b.C.method; → 退一级再试（少一层成员名）
                parent_hit = self._by_module.get(fqn.rsplit(".", 1)[0])
                return [parent_hit] if parent_hit else []
            return []

        py_from = _PY_FROM_RE.match(stmt)
        if py_from:
            module = py_from.group("module") or ""
            return self._resolve_python_module(module, from_dir)

        py_bare = _PY_BARE_RE.match(stmt)
        if py_bare:
            out: list[str] = []
            for module in py_bare.group("modules").split(","):
                out.extend(self._resolve_python_module(module.strip(), from_dir))
            return out

        ts = _TS_MODULE_RE.search(stmt)
        if ts:
            spec = ts.group(1)
            if not spec.startswith("."):
                return []  # 裸包名是第三方依赖，不在本次审查范围内
            base = posixpath.normpath(posixpath.join(from_dir, spec))
            return list(_ts_candidates(base, self._file_set))
        return []

    def _resolve_python_module(self, module: str, from_dir: str) -> list[str]:
        """Python 的模块名 → 文件路径。

        `from .local import X` 是**相对导入**，点数就是上跳层数；不带点的按全限定名查表。
        两种情况都要考虑包（`pkg/__init__.py`）。
        """
        if module.startswith("."):
            level = len(module) - len(module.lstrip("."))
            base = from_dir
            for _ in range(max(0, level - 1)):
                base = base.rpartition("/")[0]
            rest = module.lstrip(".").replace(".", "/")
            target = posixpath.normpath(posixpath.join(base, rest)) if rest else base
            return list(_py_candidates(target, self._file_set))

        if not module:
            return []
        hit = self._by_module.get(module)
        if hit:
            return [hit]
        # from a.b import C 里的 a.b 也可能是包名
        return list(_py_candidates(module.replace(".", "/"), self._file_set))


def _ts_candidates(base: str, file_set: set[str]) -> Iterator[str]:
    for candidate in (
        base,
        *(base + ext for ext in _TS_EXTENSIONS),
        *(f"{base}/{index}" for index in _TS_INDEX_FILES),
    ):
        if candidate in file_set:
            yield candidate


def _py_candidates(base: str, file_set: set[str]) -> Iterator[str]:
    for candidate in (f"{base}.py", f"{base}.pyi", f"{base}/__init__.py", f"{base}/__init__.pyi"):
        if candidate in file_set:
            yield candidate


def resolve_referenced_paths(
    imports: Iterable[str],
    from_path: str,
    repo_index: RepoFileIndex | None,
    *,
    limit: int = 8,
) -> list[str]:
    """当前文件"可能引用了其类型定义"的文件集合：import 解析结果 + 同目录兄弟。"""
    if repo_index is None:
        return []
    out = repo_index.resolve(imports, from_path, limit=limit)
    for sibling in repo_index.siblings(from_path):
        if len(out) >= limit:
            break
        if sibling not in out:
            out.append(sibling)
    return out
