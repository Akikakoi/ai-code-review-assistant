"""L2 完整实现的集成测试。

文档 §16 阶段二的交付物第 1 项：「`symbol_index`（tree-sitter）与 L2 完整实现
（imports、类型签名）」。

上一版实现里 `SymbolIndex` 一直是**空索引** —— 方法都在，但没人往里塞东西，
所以 `referenced_types` 恒为空。这组测试专门盯住这一点：必须真的从仓库里解析出
被引用类型的签名，而不是"接口就位、结果为空的假通过"。
"""

from __future__ import annotations

from acra.context.builder import ContextBuilder
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set
from acra.repo.symbol_index import SymbolIndex
from tests.fixtures.repo import make_java_bug_repo


def _builder_and_diff(repo, base, head, settings, *, with_repo_files: bool = True):
    handle = gateway.discover_local(repo)
    merge_base = handle.merge_base(base, head)
    head_sha = handle.resolve_commit(head)
    diff_set = build_diff_set(
        handle.diff(merge_base, head_sha),
        base_sha=base,
        head_sha=head_sha,
        merge_base_sha=merge_base,
    )
    symbol_index = SymbolIndex()
    builder = ContextBuilder(
        handle,
        settings,
        head_sha=head_sha,
        symbol_index=symbol_index,
        repo_files=handle.list_files(head_sha) if with_repo_files else None,
    )
    return builder, diff_set


def test_l2_collects_imports_of_changed_file(settings, tmp_path) -> None:
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings)
    ctx = builder.build(diff_set.files[0])

    assert "import java.util.Map;" in ctx.imports
    assert "import java.util.concurrent.ConcurrentHashMap;" in ctx.imports


def test_l2_resolves_same_package_type_signature(settings, tmp_path) -> None:
    """`Order` 与 `OrderService` 同包、不 import，必须靠兄弟文件解析出来。"""
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings)
    ctx = builder.build(diff_set.files[0])

    names = {t.name for t in ctx.referenced_types}
    assert "Order" in names, f"未解析到同包类型，实际拿到 {names}"

    order = next(t for t in ctx.referenced_types if t.name == "Order")
    assert order.path.endswith("Order.java")
    # 只注入签名，不注入实现体
    assert "class Order" in order.signature
    assert "getAmount()" not in order.signature


def test_l2_type_signatures_absent_without_repo_file_index(settings, tmp_path) -> None:
    """没有仓库文件清单时不应假装成功 —— 降级要可观测。"""
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings, with_repo_files=False)
    ctx = builder.build(diff_set.files[0])

    assert ctx.referenced_types == []
    assert "l2_no_repo_file_index" in ctx.degraded


def test_l2_type_signatures_disabled_by_setting(settings, tmp_path) -> None:
    settings.acra_l2_type_signatures = False
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings)
    ctx = builder.build(diff_set.files[0])

    assert ctx.referenced_types == []
    # 关闭时不应报"缺文件索引"，那是误导
    assert "l2_no_repo_file_index" not in ctx.degraded


def test_l2_index_budget_is_respected(settings, tmp_path) -> None:
    settings.acra_l2_max_index_files = 1
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings)
    builder.build(diff_set.files[0])

    assert len(builder._indexed_paths) <= 1


def test_symbol_index_populated_after_build(settings, tmp_path) -> None:
    repo, base, head = make_java_bug_repo(tmp_path)
    builder, diff_set = _builder_and_diff(repo, base, head, settings)
    builder.build(diff_set.files[0])

    # 索引里应当同时有变更文件自身与它引用的同包类型
    assert builder.symbol_index.signature_of("OrderService") is not None
    assert builder.symbol_index.signature_of("Order") is not None
    assert len(builder.symbol_index) > 0
