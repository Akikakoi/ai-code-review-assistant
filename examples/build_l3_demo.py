"""构造一个能自然触发 L3 的小仓库，用来验证"仓库级线索"真的取到了。

为什么需要它：L3 的三类线索（反向引用、相似实现、历史评论）此前**接口齐全、渲染齐全、
提示词也留了位置，但没有任何东西去填** —— 于是"验证 L3"这件事本身没有素材。
这个仓库给出可判定的素材：

    app/handler.py       调用 find_order          → 反向引用（影响面）
    app/admin.py         另一个调用点             → 反向引用
    app/order_query.py   同名的既有实现 find_order → 相似实现（项目约定）
    app/order_service.py 被改的文件，含 SQL 拼接  → 触发 sql_touched

变更点刻意包含 `select `，因为 `risk_rules.l3_triggers` 对 SQL 的判定与语言无关，
这样 `acra review` 的**默认路径**也会走到 L3，而不是只有 `--level 3` 才走到。

用法：
    python examples/build_l3_demo.py [目标目录]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

INIT = '"""演示包。"""\n'

BASE_SERVICE = '''"""订单查询服务。"""


class OrderService:
    """按条件查询订单。"""

    def __init__(self, store):
        self.store = store

    def find_order(self, order_id):
        return self.store.query_one(
            "select * from orders where id = ?", [order_id]
        )
'''

HEAD_SERVICE = '''"""订单查询服务。"""

import threading


class OrderService:
    """按条件查询订单。"""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def find_order(self, order_id):
        with self.lock:
            sql = "select * from orders where id = '" + str(order_id) + "'"
            return self.store.query_one(sql)
'''

QUERY = '''"""订单查询的另一种写法（既有实现，供 L3 作为项目约定参照）。"""


class OrderQuery:
    """只读查询。"""

    def find_order(self, order_id):
        return self.store.query_one(
            "select * from orders where id = ?", [order_id]
        )

    def find_order_with_lock(self, order_id):
        with self.lock:
            return self.find_order(order_id)
'''

HANDLER = '''"""订单接口层。"""


class OrderHandler:
    """HTTP 入口。"""

    def __init__(self, service):
        self.service = service

    def get(self, order_id):
        order = self.service.find_order(order_id)
        return {"order": order}
'''

ADMIN = '''"""管理端。"""


class AdminConsole:
    """运营后台的查询入口。"""

    def __init__(self, service):
        self.service = service

    def lookup(self, order_id):
        return self.service.find_order(order_id)
'''

GIT_ID = [
    "-c",
    "user.email=acra@demo.local",
    "-c",
    "user.name=acra demo",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
]

#: 变更所在的分支。刻意不用 `feature/l3` —— 见 `build()` 里的说明。
HEAD_BRANCH = "l3-demo-head"

FILES = {
    "app/__init__.py": INIT,
    "app/order_query.py": QUERY,
    "app/handler.py": HANDLER,
    "app/admin.py": ADMIN,
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *GIT_ID, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr}")
    return proc.stdout


def branch_exists(repo: Path, name: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode == 0


def build(target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    git(target, "init", "-q", "-b", "main")
    for rel, content in FILES.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (target / "app" / "order_service.py").write_text(BASE_SERVICE, encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "base: 订单查询走参数化")

    # 分支名刻意**不带斜杠**，并且建完立刻验证 ref 真的存在。
    # 实测踩过：`git checkout -b a/b` 会先把 HEAD 指过去、却没写成 ref
    # （写 refs/heads/a/b 需要新建子目录），于是分支名看起来生效了、
    # 后续 `git rev-parse` 才发现 HEAD 悬空 —— 而构造器当时是"成功返回"的。
    git(target, "checkout", "-q", "-b", HEAD_BRANCH)
    if not branch_exists(target, HEAD_BRANCH):
        raise SystemExit(
            f"分支 {HEAD_BRANCH} 建完却不存在（HEAD 悬空）。"
            "这是本机 git 在 .git 下新建子目录时的已知问题，请改用不带斜杠的分支名。"
        )
    (target / "app" / "order_service.py").write_text(HEAD_SERVICE, encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "feat: 查询加锁并拼接 SQL")
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "l3-demo"
    print(build(out))
