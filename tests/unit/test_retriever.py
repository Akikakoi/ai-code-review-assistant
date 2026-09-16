"""L3 检索器的单测。

这组测试盯的不是"接口返回了列表"—— `callers=[]` 是合法返回值，
跟"真的没有调用方"长得一模一样，那种断言等于没断言。
这里断言的是**具体命中**：哪一行被认成了调用点、哪个实现被认成了相似实现。

用 `FakeHandle` 而不是真实 git 仓库：检索器只需要 `file_lines` 一个方法，
而起真实仓库会把测试的成败绑到 git 环境上（本机 git 就有已知的坑）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acra.context.retriever import (
    SIMILARITY_THRESHOLD,
    STRATEGY_SYMBOL,
    STRATEGY_VECTOR,
    Retriever,
    is_informative_name,
    name_similarity,
)
from acra.models import ChangeType, FileDiff, Hunk, SymbolSpan

SERVICE = '''"""订单服务。"""


class OrderService:
    def find_order(self, order_id):
        return self.store.query_one(order_id)
'''

QUERY = '''"""既有实现（供参照）。"""


class OrderQuery:
    def find_order(self, order_id):
        return self.store.query_one(order_id)

    def find_order_with_lock(self, order_id):
        return self.find_order(order_id)
'''

HANDLER = '''"""入口层。"""


class Handler:
    def get(self, order_id):
        return self.service.find_order(order_id)
'''

COMMENT_ONLY = '''"""注释里出现了方法名，但它不是调用点。"""


class Other:
    # find_order 只是注释
    def unrelated(self):
        return None
'''


class FakeHandle:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def file_lines(self, _sha: str, path: str) -> list[str]:
        content = self.files.get(path)
        return content.split("\n") if content is not None else []


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "acra_l3_max_scan_files": 400,
        "acra_l3_max_callers": 8,
        "acra_l3_max_similar": 3,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _fd(path: str) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=ChangeType.MODIFY,
        hunks=[Hunk(old_start=1, old_lines=1, new_start=1, new_lines=1, added=[(5, "x")])],
    )


def _span(name: str, kind: str = "method") -> SymbolSpan:
    return SymbolSpan(name=name, kind=kind, start_line=1, end_line=3, source="pass")


def _retriever(files: dict[str, str], settings=None, repo_files=None) -> Retriever:
    return Retriever(
        FakeHandle(files),
        settings or _settings(),
        head_sha="head",
        repo_files=list(repo_files if repo_files is not None else files),
    )


# ---------------------------------------------------------------------------- 名字判定


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("find_order", "find_order", 1.0),
        ("find_order", "findOrder", 1.0),  # 忽略大小写与下划线
        ("find_order", "find_order_with_lock", 0.8),  # 同族（加后缀）
        ("find_order", "totally_different", 0.0),
        ("ab", "ab", 1.0),
        ("", "find_order", 0.0),
    ],
)
def test_name_similarity(a: str, b: str, expected: float) -> None:
    assert name_similarity(a, b) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("__init__", False),
        ("__enter__", False),
        ("main", False),
        ("ab", False),
        ("find_order", True),
        ("findOrderWithLock", True),
    ],
)
def test_is_informative_name(name: str, expected: bool) -> None:
    """`__init__` 这类名字拿去找相似实现只会命中所有文件的构造器，是纯噪音。"""
    assert is_informative_name(name) is expected


def test_trivial_names_are_excluded_from_similar_lookup() -> None:
    """回归：变更点落在 `__init__` 里时，相似实现曾全是各文件的 `__init__`。"""
    files = {
        "app/service.py": SERVICE,
        "app/query.py": QUERY,
        "app/handler.py": HANDLER,
    }
    retriever = _retriever(files)
    clues = retriever.clues(_fd("app/service.py"), [_span("__init__", "constructor")])

    assert clues.similar == []
    # 而且不能因为"没有可检索的名字"就去扫一堆文件白花钱
    assert any("未取到可检索的符号名" in n for n in clues.notes)


# ---------------------------------------------------------------------------- 策略


def test_strategy_follows_repo_size_but_effective_is_symbol() -> None:
    small = _retriever({"a/x.py": "pass"}, repo_files=[f"a/{i}.py" for i in range(10)])
    assert small.strategy == STRATEGY_VECTOR

    large = _retriever({"a/x.py": "pass"}, repo_files=[f"a/{i}.py" for i in range(6000)])
    assert large.strategy == STRATEGY_SYMBOL

    # 向量路径未实现：两条都应如实报告"实际用的是符号匹配"
    assert small.effective_strategy == STRATEGY_SYMBOL
    assert large.effective_strategy == STRATEGY_SYMBOL


def test_vector_strategy_reports_why_it_fell_back() -> None:
    """向量未启用时必须说明原因并退回符号匹配，而不是静默给空结果。"""
    files = {"app/service.py": SERVICE, "app/query.py": QUERY, "app/handler.py": HANDLER}
    clues = _retriever(files).clues(_fd("app/service.py"), [_span("find_order")])
    assert any("向量检索不可用" in n for n in clues.notes), clues.notes
    # 退化不等于没干活：符号匹配的线索仍然要给出来
    assert clues.similar, "退化到符号匹配后应有相似实现"


def test_disabled_retriever_explains_why() -> None:
    retriever = Retriever(FakeHandle({}), _settings(), head_sha="head", repo_files=[])
    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])
    assert clues.callers == [] and clues.similar == []
    assert any("L3 未启用" in n for n in clues.notes)


# ---------------------------------------------------------------------------- 线索


def test_callers_are_found_and_definitions_are_not() -> None:
    files = {
        "app/service.py": SERVICE,
        "app/query.py": QUERY,
        "app/handler.py": HANDLER,
    }
    clues = _retriever(files).clues(_fd("app/service.py"), [_span("find_order")])

    paths = {c.caller_path for c in clues.callers}
    assert paths == {"app/query.py", "app/handler.py"}, f"实际 {paths}"
    # 自己不该出现在调用方里
    assert "app/service.py" not in paths
    # 定义行不能被当成调用点（`def find_order(self, order_id):`）
    assert all("def find_order(" not in c.snippet for c in clues.callers)


def test_comment_mentions_are_not_callers() -> None:
    files = {"app/service.py": SERVICE, "app/other.py": COMMENT_ONLY}
    clues = _retriever(files).clues(_fd("app/service.py"), [_span("find_order")])
    assert clues.callers == []


def test_similar_implementations_come_from_other_files_with_scores() -> None:
    files = {
        "app/service.py": SERVICE,
        "app/query.py": QUERY,
        "app/handler.py": HANDLER,
    }
    clues = _retriever(files).clues(_fd("app/service.py"), [_span("find_order")])

    got = {(s.path, s.score) for s in clues.similar}
    assert ("app/query.py", 1.0) in got  # 同名实现
    assert ("app/query.py", 0.8) in got  # 同族实现（加了 _with_lock）
    assert all(s.score >= SIMILARITY_THRESHOLD for s in clues.similar)
    assert all(s.path != "app/service.py" for s in clues.similar)


def test_candidates_prefer_the_same_directory() -> None:
    files = {
        "app/deep/service.py": SERVICE,
        "app/deep/sibling.py": QUERY,
        "app/handler.py": HANDLER,
    }
    retriever = _retriever(files, repo_files=list(files))
    candidates = retriever._candidates("app/deep/service.py")
    assert candidates[0] == "app/deep/sibling.py", candidates


def test_scan_quota_truncation_is_reported_as_degraded() -> None:
    """配额用尽意味着线索可能不完整 —— 这是降级，与"扫完了确实没有"不同。"""
    files = {"app/service.py": SERVICE, "app/query.py": QUERY, "app/handler.py": HANDLER}
    retriever = _retriever(files, settings=_settings(acra_l3_max_scan_files=1))
    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])

    assert clues.truncated is True
    assert any("截断" in n for n in clues.notes)


def test_no_clue_is_stated_explicitly_not_silently_empty() -> None:
    """找不到线索时必须留一句话，否则"没找到"与"没跑"在报告上无法区分。"""
    files = {"app/service.py": SERVICE, "app/untouched.py": "x = 1\n"}
    clues = _retriever(files).clues(_fd("app/service.py"), [_span("total_unknown")])
    assert clues.callers == [] and clues.similar == []
    assert any("已执行但未找到" in n for n in clues.notes)


def test_search_failure_degrades_instead_of_raising() -> None:
    class Broken(FakeHandle):
        def file_lines(self, _sha: str, path: str) -> list[str]:
            raise RuntimeError("读不到")

    retriever = Retriever(Broken({}), _settings(), head_sha="head", repo_files=["app/x.py"])
    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])
    assert clues.callers == []
    assert any("检索失败" in n for n in clues.notes)


# ---------------------------------------------------------------------------- 向量路径


class BagOfWordsBackend:
    """确定性嵌入：token 落进固定维度桶再归一化。

    同词集 → 同向量（相似度 1.0），词集重叠多 → 相似度高。
    既可重复又有意义，让向量检索的测试不依赖真实模型。
    """

    name = "fake_bag"

    def __init__(self, dims: int = 64) -> None:
        self.dims = dims

    def available(self):
        return True, ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import re

        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for token in re.findall(r"[a-z_]+", text.lower()):
                idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dims
                vec[idx] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


def _vector_retriever(files: dict[str, str], tmp_path, **kwargs):
    from acra.context.vector import MethodVectorIndex

    backend = BagOfWordsBackend()
    index = MethodVectorIndex(tmp_path / "l3-index" / "test.sqlite", backend)
    retriever = Retriever(
        FakeHandle(files), _settings(**kwargs), head_sha="head",
        repo_files=list(files), vector_index=index,
    )
    return retriever, index


def test_vector_index_finds_similar_method_from_other_file(tmp_path) -> None:
    files = {
        "app/service.py": SERVICE,
        "app/query.py": QUERY,
        "app/handler.py": HANDLER,
    }
    retriever, index = _vector_retriever(files, tmp_path)
    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])

    paths = {s.path for s in clues.similar}
    assert "app/query.py" in paths, f"同名实现应被向量检索命中，实际 {paths}"
    assert "app/service.py" not in paths, "必须过滤掉自己（ADR 0006）"
    assert any("向量检索策略=fake_bag" in n for n in clues.notes), clues.notes


def test_vector_index_incremental_build_skips_unchanged(tmp_path) -> None:
    """content-hash 未变不重嵌 —— 这是增量索引成立的前提。"""
    files = {"app/service.py": SERVICE, "app/query.py": QUERY}
    retriever, index = _vector_retriever(files, tmp_path)
    retriever.clues(_fd("app/service.py"), [_span("find_order")])
    first = index.embedded_count
    assert first > 0

    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])
    assert index.embedded_count == first, "第二次构建不应重复嵌入"
    assert any("无新增方法需要嵌入" in n for n in clues.notes), clues.notes
    assert clues.similar, "缓存命中后查询仍应有结果"


def test_vector_backend_unavailable_falls_back_to_symbol(tmp_path) -> None:
    """后端不可用时退回符号匹配，且说明里写明原因。"""
    from acra.context.vector import MethodVectorIndex

    class DeadBackend:
        name = "dead"

        def available(self):
            return False, "依赖未安装"

        def embed(self, texts):
            raise RuntimeError("不该被调用")

    files = {"app/service.py": SERVICE, "app/query.py": QUERY}
    index = MethodVectorIndex(tmp_path / "l3-index" / "dead.sqlite", DeadBackend())
    retriever = Retriever(
        FakeHandle(files), _settings(), head_sha="head",
        repo_files=list(files), vector_index=index,
    )
    clues = retriever.clues(_fd("app/service.py"), [_span("find_order")])
    assert any("依赖未安装" in n for n in clues.notes), clues.notes
    assert clues.similar, "退回符号匹配后应有线索"


def test_method_text_is_deterministic() -> None:
    """同一段源码必须产出同样的嵌入文本 —— 否则增量缓存永远失效。"""
    from acra.context.vector import method_text, text_hash

    span = _span("find_order")
    assert method_text(span) == method_text(span)
    assert text_hash(method_text(span)) == text_hash(method_text(span))
