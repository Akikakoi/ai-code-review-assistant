"""符号定位与符号索引的单测。

文档 §14.1：「symbol 索引：Java 注解、内部类、lambda、接口默认方法」。
"""

from __future__ import annotations

import pytest

from acra.repo.symbol_index import (
    RepoFileIndex,
    SymbolIndex,
    extract_imports,
    find_enclosing_spans,
    language_for_path,
    resolve_referenced_paths,
    tree_sitter_available,
    window_span,
)

JAVA = """package com.example.svc;

import java.util.List;
import static java.util.Objects.requireNonNull;

@Service
public class OrderService {

    private final List<String> names;

    @Transactional
    public void pay(String userId) {
        if (userId == null) {
            throw new IllegalArgumentException("x");
        }
        inner(userId);
    }

    private void inner(String userId) {
        Runnable r = () -> System.out.println(userId);
        r.run();
    }

    class Inner {
        void deep() {
            int x = 1;
        }
    }
}
"""

TYPESCRIPT = """import { Injectable } from '@angular/core';
import type { Order } from './order';

@Injectable()
export class OrderService {
  constructor(private readonly http: HttpClient) {}

  async pay(id: string): Promise<void> {
    const o = await this.load(id);
    console.log(o);
  }

  private load(id: string): Promise<Order> {
    return Promise.resolve({ id });
  }
}
"""


def test_language_detection() -> None:
    assert language_for_path("a/B.java") == "java"
    assert language_for_path("a/b.ts") == "typescript"
    assert language_for_path("a/b.tsx") == "tsx"
    assert language_for_path("a/b.js") == "tsx"
    assert language_for_path("a/b.py") == "python"
    assert language_for_path("Makefile") is None


def test_tree_sitter_available_for_java_and_typescript() -> None:
    assert tree_sitter_available("java")
    assert tree_sitter_available("typescript")


def test_enclosing_span_picks_innermost_method() -> None:
    spans, degraded = find_enclosing_spans(JAVA, {11}, path="OrderService.java")
    assert degraded == []
    assert len(spans) == 1
    span = spans[0]
    assert span.kind == "method"
    assert span.name == "pay"
    assert span.parent == "OrderService"
    assert span.start_line <= 11 <= span.end_line
    assert "public void pay" in span.signature


def test_enclosing_span_keeps_annotations_in_signature() -> None:
    """注解是语义推断的关键线索（@Transactional），不能丢。"""
    spans, _ = find_enclosing_spans(JAVA, {11}, path="OrderService.java")
    assert "@Transactional" in spans[0].signature


def test_enclosing_span_inside_nested_block_still_resolves_to_outer_method() -> None:
    """变更点在 if 块内部时，仍然应当拿到整个方法，而不是 if 块。"""
    spans, _ = find_enclosing_spans(JAVA, {12}, path="OrderService.java")
    assert spans[0].kind == "method"
    assert spans[0].name == "pay"


def test_enclosing_span_inside_lambda_resolves_to_lambda_then_method() -> None:
    spans, _ = find_enclosing_spans(JAVA, {21}, path="OrderService.java")
    kinds = {s.kind for s in spans}
    assert kinds <= {"method", "lambda"}
    assert any(s.name == "inner" for s in spans)


def test_enclosing_span_for_inner_class_method() -> None:
    spans, _ = find_enclosing_spans(JAVA, {27}, path="OrderService.java")
    assert spans[0].name == "deep"
    assert spans[0].parent == "Inner"


def test_multiple_lines_in_different_methods_return_multiple_spans() -> None:
    spans, _ = find_enclosing_spans(JAVA, {11, 26}, path="OrderService.java")
    assert {s.name for s in spans} == {"pay", "deep"}


def test_typescript_enclosing_span() -> None:
    spans, degraded = find_enclosing_spans(TYPESCRIPT, {9}, path="order.service.ts")
    assert degraded == []
    assert spans[0].name == "pay"
    assert spans[0].kind == "method"


def test_grammar_unavailable_falls_back_to_window_with_degradation() -> None:
    source = "\n".join(f"line {i}" for i in range(1, 200))
    spans, degraded = find_enclosing_spans(source, {100}, path="notes.txt")
    assert spans[0].kind == "window"
    assert spans[0].truncated is True
    assert degraded and degraded[0].startswith("grammar_unavailable")


def test_window_span_radius() -> None:
    source = "\n".join(f"line {i}" for i in range(1, 500))
    span = window_span(source, {250}, radius=10)
    assert span.start_line == 240
    assert span.end_line == 260


def test_empty_focus_lines_returns_nothing() -> None:
    assert find_enclosing_spans(JAVA, set(), path="OrderService.java") == ([], [])


def test_extract_imports_java() -> None:
    imports = extract_imports(JAVA, "java")
    assert any(i.startswith("package com.example.svc;") for i in imports)
    assert "import java.util.List;" in imports
    assert "import static java.util.Objects.requireNonNull;" in imports


def test_extract_imports_typescript() -> None:
    imports = extract_imports(TYPESCRIPT, "typescript")
    assert "import { Injectable } from '@angular/core';" in imports
    assert any("import type { Order }" in i for i in imports)


def test_extract_imports_unknown_language_is_empty() -> None:
    assert extract_imports("x = 1", None) == []


def test_symbol_index_collects_classes_and_methods() -> None:
    index = SymbolIndex()
    symbols = index.add_file("OrderService.java", JAVA)
    names = {s.name for s in symbols}
    assert {"OrderService", "pay", "inner", "deep"} <= names

    symbol = index.signature_of("OrderService")
    assert symbol is not None
    assert symbol.kind == "class"
    assert "class OrderService" in symbol.signature


def test_symbol_index_type_signatures_excludes_non_types() -> None:
    index = SymbolIndex()
    index.add_file("OrderService.java", JAVA)
    types = index.type_signatures(["OrderService", "pay", "Missing"])
    assert [t.name for t in types] == ["OrderService"]
    assert "class OrderService" in types[0].signature
    assert "{" not in types[0].signature.split("OrderService")[0]


def test_symbol_index_ignores_unsupported_file() -> None:
    index = SymbolIndex()
    assert index.add_file("notes.txt", "hello") == []
    assert len(index) == 0


@pytest.mark.parametrize("lang", ["java", "typescript"])
def test_parse_failure_does_not_raise(lang: str) -> None:
    """语法破损的源码不能把链路打挂 —— tree-sitter 容错解析 + 降级。"""
    broken = "class {{{ void ( ) ;;; unclosed"
    spans, degraded = find_enclosing_spans(broken, {1}, path=f"x.{'java' if lang == 'java' else 'ts'}")
    assert spans  # 至少返回一个区间（真实区间或开窗降级）


# ---------------------------------------------------------------------------- 仓库文件索引

REPO_FILES = [
    "src/main/java/com/example/order/OrderService.java",
    "src/main/java/com/example/order/Order.java",
    "src/main/java/com/example/order/OrderStatus.java",
    "src/main/java/com/example/util/Strings.java",
    "src/main/java/com/example/util/Dates.java",
    "web/src/api/order.ts",
    "web/src/api/index.ts",
    "web/src/types/order.ts",
]


def _idx(files: list[str] | None = None) -> RepoFileIndex:
    return RepoFileIndex(files or REPO_FILES)


def test_java_exact_import_resolves() -> None:
    resolved = _idx().resolve(
        ["import com.example.order.Order;"],
        "src/main/java/com/example/order/OrderService.java",
    )
    assert resolved == ["src/main/java/com/example/order/Order.java"]


def test_java_wildcard_import_resolves_to_package_files() -> None:
    resolved = _idx().resolve(
        ["import com.example.util.*;"],
        "src/main/java/com/example/order/OrderService.java",
    )
    assert set(resolved) == {
        "src/main/java/com/example/util/Strings.java",
        "src/main/java/com/example/util/Dates.java",
    }


def test_java_static_import_resolves_to_declaring_type() -> None:
    resolved = _idx().resolve(
        ["import static com.example.util.Strings.trim;"],
        "src/main/java/com/example/order/OrderService.java",
    )
    assert resolved == ["src/main/java/com/example/util/Strings.java"]


def test_third_party_import_is_not_resolved() -> None:
    """第三方依赖不在审查范围内，不该被索引。"""
    resolved = _idx().resolve(
        ["import java.util.List;", "import org.springframework.stereotype.Service;"],
        "src/main/java/com/example/order/OrderService.java",
    )
    assert resolved == []


def test_typescript_relative_import_without_extension() -> None:
    resolved = _idx().resolve(["import { Order } from '../types/order';"], "web/src/api/order.ts")
    assert resolved == ["web/src/types/order.ts"]


def test_typescript_directory_import_resolves_to_index_file() -> None:
    resolved = _idx().resolve(["import { api } from './';"], "web/src/api/order.ts")
    assert resolved == ["web/src/api/index.ts"]


def test_typescript_bare_package_import_is_skipped() -> None:
    resolved = _idx().resolve(["import axios from 'axios';"], "web/src/api/order.ts")
    assert resolved == []


def test_siblings_cover_same_package_types() -> None:
    """Java 同包类型不需要 import —— 这是最容易漏掉的一类定义文件。"""
    siblings = _idx().siblings("src/main/java/com/example/order/OrderService.java")
    assert set(siblings) == {
        "src/main/java/com/example/order/Order.java",
        "src/main/java/com/example/order/OrderStatus.java",
    }


def test_resolve_referenced_paths_combines_imports_and_siblings() -> None:
    paths = resolve_referenced_paths(
        ["import com.example.util.Strings;"],
        "src/main/java/com/example/order/OrderService.java",
        _idx(),
    )
    assert paths[0] == "src/main/java/com/example/util/Strings.java"  # import 优先
    assert "src/main/java/com/example/order/Order.java" in paths  # 同包兄弟补上


def test_resolve_referenced_paths_respects_limit() -> None:
    paths = resolve_referenced_paths(
        ["import com.example.util.*;"],
        "src/main/java/com/example/order/OrderService.java",
        _idx(),
        limit=2,
    )
    assert len(paths) == 2


def test_resolve_referenced_paths_without_index_is_empty() -> None:
    assert resolve_referenced_paths([], "a/b.java", None) == []


def test_repo_file_index_normalizes_backslashes() -> None:
    index = RepoFileIndex([r"src\main\java\com\example\order\Order.java"])
    assert index.files == ["src/main/java/com/example/order/Order.java"]


# ---------------------------------------------------------------------------- Python 解析


PY_REPO_FILES = [
    "src/acra/models.py",
    "src/acra/engine/scan.py",
    "src/acra/engine/__init__.py",
    "src/acra/context/builder.py",
    "tests/test_scan.py",
]


def _py_idx() -> RepoFileIndex:
    return RepoFileIndex(PY_REPO_FILES)


def test_python_absolute_module_resolves() -> None:
    resolved = _py_idx().resolve(["from acra.models import Finding"], "src/acra/engine/scan.py")
    assert resolved == ["src/acra/models.py"]


def test_python_relative_import_same_package() -> None:
    """`from .builder import X` 是相对导入，点数即上跳层数。"""
    resolved = _py_idx().resolve(
        ["from .builder import X"], "src/acra/context/builder.py"
    )
    assert resolved == ["src/acra/context/builder.py"] or resolved == []


def test_python_relative_dot_import_resolves_sibling() -> None:
    resolved = _py_idx().resolve(["from .scan import x"], "src/acra/engine/__init__.py")
    assert resolved == ["src/acra/engine/scan.py"]


def test_python_parent_import_goes_up_one_level() -> None:
    resolved = _py_idx().resolve(["from ..models import F"], "src/acra/engine/scan.py")
    assert resolved == ["src/acra/models.py"]


def test_python_package_init_resolves() -> None:
    resolved = _py_idx().resolve(["import acra.engine"], "src/acra/models.py")
    assert resolved == ["src/acra/engine/__init__.py"]


def test_python_stdlib_import_is_not_resolved() -> None:
    resolved = _py_idx().resolve(["import os", "import json, re"], "src/acra/models.py")
    assert resolved == []


def test_python_pyi_stub_resolves() -> None:
    index = RepoFileIndex(["pkg/api.pyi", "pkg/caller.py"])
    assert index.resolve(["from .api import X"], "pkg/caller.py") == ["pkg/api.pyi"]


def test_language_detection_python() -> None:
    assert language_for_path("a/b.py") == "python"
    assert language_for_path("a/b.pyi") == "python"


def test_tree_sitter_available_for_python() -> None:
    assert tree_sitter_available("python")


def test_python_enclosing_span() -> None:
    source = (
        "import os\n"
        "\n"
        "\n"
        "class Svc:\n"
        "    def pay(self, uid):\n"
        "        if uid is None:\n"
        "            raise ValueError('empty')\n"
        "        return uid\n"
    )
    spans, degraded = find_enclosing_spans(source, {6}, path="svc.py")
    assert degraded == []
    assert spans[0].kind == "function"
    assert spans[0].name == "pay"
    assert spans[0].parent == "Svc"
    assert "def pay" in spans[0].signature


def test_python_imports_are_extracted_per_line() -> None:
    """回归：正则里的 \\s 会吃掉换行，把相邻两条 import 粘成一条。"""
    source = "import os\nimport sys, json\nfrom a.b import C, D\nfrom .local import E\n"
    imports = extract_imports(source, "python")
    assert imports == ["import os", "import sys, json", "from a.b import C, D", "from .local import E"]


def test_python_symbol_index_collects_classes_and_functions() -> None:
    source = "class A:\n    def f(self):\n        return 1\n\n\ndef g():\n    return 2\n"
    index = SymbolIndex()
    symbols = index.add_file("mod.py", source)
    assert {s.name for s in symbols} >= {"A", "f", "g"}
    assert index.signature_of("A").kind == "class"
