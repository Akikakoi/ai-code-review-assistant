"""远端克隆路径（webhook 走的就是它）的集成测试。

CLI 用本地仓库、历史完整，所以"浅克隆下 merge-base 失败"这类问题**本地怎么测都不会发现**；
而 webhook 的 job 带的是远端 `clone_url`，worker 会按 §4.3 走
"裸仓库缓存 + `fetch --depth=1`"。这组测试用**本地路径当远端**、走与生产完全相同的
`ensure_bare_clone(depth=1)`，把那条此前从未被执行过的路径真实覆盖一次。

实测结论（也是这组测试的存在理由）：
`fetch --depth=1` 全分支后，两个分支的 tip 之间没有共同祖先，
`git merge-base` 返回非零且无输出 —— 于是 `_prepare_diff` 第一步就抛 GitError，
整次审查记成 failed。
"""

from __future__ import annotations

import pytest

from acra.errors import GitError
from acra.orchestrator.pipeline import _merge_base_with_deepen
from acra.repo import gateway
from tests.fixtures.repo import commit_all, git, init_repo, write_files

BASE = '''"""订单服务。"""


class OrderService:
    def find_order(self, order_id):
        return self.store.query_one(order_id)
'''

HEAD = '''"""订单服务。"""


class OrderService:
    def find_order(self, order_id):
        sql = "select * from orders where id = " + str(order_id)
        return self.store.query_one(sql)
'''


def _two_branch_repo(tmp_path) -> object:
    repo = init_repo(tmp_path / "src")
    write_files(repo, {"app/service.py": BASE})
    commit_all(repo, "base: 参数化查询")
    git(repo, "checkout", "-q", "-b", "topic")  # 分支名刻意不带斜杠（本机 git 的已知问题）
    write_files(repo, {"app/service.py": HEAD})
    commit_all(repo, "feat: 拼接 SQL")
    git(repo, "checkout", "-q", "main")
    return repo


def _shallow_handle(tmp_path, repo) -> object:
    handle = gateway.ensure_bare_clone(str(repo), tmp_path / "work", depth=1)
    assert handle.is_shallow, "前置条件：depth=1 的远端克隆必须是浅克隆"
    return handle


def test_depth1_clone_cannot_compute_merge_base(tmp_path) -> None:
    """先固化缺陷本身：浅克隆下 merge-base 必然失败。

    这条测试存在的意义是防止有人"顺手优化"掉加深逻辑 —— 删掉 `_merge_base_with_deepen`
    后，webhook 路径会重新变回"真实 PR 上必然失败"，而且本地测试全绿。
    """
    repo = _two_branch_repo(tmp_path)
    handle = _shallow_handle(tmp_path, repo)
    base = handle.resolve_commit("refs/remotes/origin/main")
    head = handle.resolve_commit("refs/remotes/origin/topic")

    with pytest.raises(GitError):
        handle.merge_base(base, head)


def test_deepen_recovers_merge_base_and_diff(tmp_path) -> None:
    repo = _two_branch_repo(tmp_path)
    handle = _shallow_handle(tmp_path, repo)
    base = handle.resolve_commit("refs/remotes/origin/main")
    head = handle.resolve_commit("refs/remotes/origin/topic")

    info: list[str] = []
    merge_base = _merge_base_with_deepen(handle, base, head, info)

    assert merge_base, "加深后应当能算出 merge-base"
    assert any("加深" in note for note in info)

    # 更重要的验证：加深之后 diff 真的可用（这是整条链路的最终目的）
    raw = handle.diff(merge_base, head)
    assert "select * from orders" in raw


def test_deepen_is_not_attempted_on_a_full_clone(tmp_path) -> None:
    """非浅克隆（本地路径）本就能算出 merge-base，不应触发加深。"""
    repo = _two_branch_repo(tmp_path)
    handle = gateway.discover_local(repo)
    base = handle.resolve_commit("main")
    head = handle.resolve_commit("topic")

    info: list[str] = []
    assert handle.is_shallow is False
    assert _merge_base_with_deepen(handle, base, head, info)
    assert info == [], "没有发生加深就不该有加深说明"
