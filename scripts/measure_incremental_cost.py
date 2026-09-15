"""实测"重复 push 场景"的增量审查成本降幅（文档 §16 阶段三验收：≥ 60%）。

为什么必须真跑一遍：这个数字是**增量判定 + token 预算 + 缓存计价 + 成本口径**
四件事合起来的结果。任何一环单独有测试，都不足以说明"重复 push 能省钱"成立 ——
它和 ADR 0010 记的那类坑同源：零件齐了，合起来没验过。

场景（模拟同一个 PR 上的两次 push）：

    main ──A──B
      A：三个文件、多处真实缺陷（"大的那一次 push"）
      B：A 之上的一行小改动（"小的那一次 push"）

    run1  full         main..A   建立基线（并在库里留下可用的增量基线记录）
    run2  incremental  A..B     有增量能力时要花的钱
    run3  full         main..B   没有增量能力、同样目标状态要花的钱（对照）

降幅 = 1 − cost(run2) / cost(run3)。用 run3 而不是 run1 做分母是关键：
两者目标状态相同，差别**只有**有没有复用上一轮的审查范围。

刻意关掉静态分析（`ACRA_STATIC_ANALYSIS_ENABLED=false`）：静态层是 diff-aware 的、
成本本来就与 diff 大小线性相关，混进来只会稀释信号；这里要测的是模型那条路径。

用法（需要真实模型，会产生费用）：
    ./.venv/Scripts/python.exe scripts/measure_incremental_cost.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / ".acra-work" / "incr-measure"
PR_NUMBER = 77

GIT_ID = [
    "-c",
    "user.email=acra@measure.local",
    "-c",
    "user.name=acra measure",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
]

# ---------------------------------------------------------------- 演示仓库内容

BASE_FILES = {
    "app/__init__.py": '"""演示包。"""\n',
    "app/order_service.py": '''"""订单服务。"""

from app.inventory import Inventory


class OrderService:
    """下单与查询。"""

    def __init__(self, store, inventory):
        self.store = store
        self.inventory = inventory

    def total_amount(self, items):
        total = 0
        for item in items:
            price = item.get("price")
            if price is None:
                continue
            total += price * item.get("quantity", 1)
        return total

    def find_order(self, order_id):
        query = "select * from orders where id = ?"
        return self.store.query_one(query, [order_id])

    def place(self, items):
        total = self.total_amount(items)
        for item in items:
            self.inventory.reserve(item["sku"], item.get("quantity", 1))
        return self.store.insert("orders", {"total": total})
''',
    "app/inventory.py": '''"""库存。"""


class Inventory:
    """简单的库存账。"""

    def __init__(self, store):
        self.store = store

    def available(self, sku):
        row = self.store.query_one("select qty from stock where sku = ?", [sku])
        return int(row["qty"]) if row else 0

    def reserve(self, sku, quantity):
        current = self.available(sku)
        if current - quantity < 0:
            raise ValueError("库存不足")
        self.store.execute(
            "update stock set qty = qty - ? where sku = ?", [quantity, sku]
        )
        return current - quantity
''',
    "app/report.py": '''"""报表。"""

from app.order_service import OrderService


class ReportWriter:
    """把订单汇总写成文本。"""

    def __init__(self, service: OrderService, path):
        self.service = service
        self.path = path

    def daily(self, orders):
        handle = open(self.path, "w", encoding="utf-8")
        total = 0
        for order in orders:
            amount = self.service.total_amount(order["items"])
            total += amount
            handle.write(f"{order['id']},{amount}\\n")
        handle.write(f"total,{total}\\n")
        handle.close()
        return total
''',
    "README.md": "# 演示仓库\n\n用于实测增量审查的成本降幅。\n",
}

#: 变更点 A：三个文件，多处真实缺陷
HEAD_A_FILES = {
    "app/order_service.py": '''"""订单服务。"""

from app.inventory import Inventory


class OrderService:
    """下单与查询。"""

    def __init__(self, store, inventory):
        self.store = store
        self.inventory = inventory

    def total_amount(self, items):
        total = 0
        for item in items:
            total += item.get("price") * item.get("quantity", 1)
        return total

    def find_order(self, order_id):
        query = "select * from orders where id = " + str(order_id)
        return self.store.query_one(query)

    def place(self, items):
        total = self.total_amount(items)
        for item in items:
            self.inventory.reserve(item["sku"], item.get("quantity", 1))
        return self.store.insert("orders", {"total": total})
''',
    "app/inventory.py": '''"""库存。"""

import threading


class Inventory:
    """简单的库存账。"""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def available(self, sku):
        row = self.store.query_one("select qty from stock where sku = ?", [sku])
        return int(row["qty"]) if row else 0

    def reserve(self, sku, quantity):
        with self.lock:
            current = self.available(sku)
            self.store.execute(
                "update stock set qty = qty - ? where sku = ?", [quantity, sku]
            )
            self.reindex(sku)
            return current - quantity

    def reindex(self, sku):
        self.store.execute("update stock set updated_at = now() where sku = ?", [sku])
''',
    "app/report.py": '''"""报表。"""

from app.order_service import OrderService


class ReportWriter:
    """把订单汇总写成文本。"""

    def __init__(self, service: OrderService, path):
        self.service = service
        self.path = path

    def daily(self, orders):
        handle = open(self.path, "w", encoding="utf-8")
        total = 0
        for order in orders:
            try:
                amount = self.service.total_amount(order["items"])
            except Exception:
                pass
            total += amount
            handle.write(f"{order['id']},{amount}\\n")
        handle.write(f"total,{total}\\n")
        return total
''',
}

#: 变更点 B：一行小改动（"小的那一次 push"）
HEAD_B_FILES = {
    "app/order_service.py": HEAD_A_FILES["app/order_service.py"].replace(
        'total += item.get("price") * item.get("quantity", 1)',
        'total += item.get("price", 0) * item.get("quantity", 1)',
    ),
}


# ---------------------------------------------------------------- git / 运行


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
    return proc.stdout.strip()


def write_files(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def build_repo(root: Path) -> dict[str, str]:
    repo = root / "demo-incremental"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    write_files(repo, BASE_FILES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base: 初始实现")

    git(repo, "checkout", "-q", "-b", "feature/incr")
    write_files(repo, HEAD_A_FILES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: 订单查询与报表")
    sha_a = git(repo, "rev-parse", "HEAD")

    write_files(repo, HEAD_B_FILES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fix: 价格缺失时按 0 计")
    sha_b = git(repo, "rev-parse", "HEAD")

    return {"a": sha_a, "b": sha_b}


def run_review(*, repo: Path, base: str, head: str, tag: str, force_full: bool) -> dict[str, Any]:
    out_path = WORKDIR / f"{tag}.json"
    env = {
        **os.environ,
        "ACRA_WORKDIR": str(WORKDIR),
        # 关掉静态分析：它按 diff 大小线性计费，混进来只会稀释"增量省了多少"的信号
        "ACRA_STATIC_ANALYSIS_ENABLED": "false",
        # 不发布（本机也没有发布凭据）
        "ACRA_GITHUB_TOKEN": "",
        "GITHUB_APP_ID": "",
        "GITHUB_APP_PRIVATE_KEY_PATH": "",
    }
    cmd = [
        sys.executable,
        "-m",
        "acra.cli",
        "review",
        "--repo",
        str(repo),
        "--base",
        base,
        "--head",
        head,
        "--pr",
        str(PR_NUMBER),
        "--repo-full-name",
        "measure/incremental",
        "--format",
        "json",
        "--out",
        str(out_path),
        "--dry-run",
    ]
    if force_full:
        cmd.append("--full")

    print(f"  → {tag}: {' '.join(cmd[4:])}", flush=True)
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if proc.returncode != 0 or not out_path.exists():
        raise SystemExit(
            f"{tag} 运行失败（rc={proc.returncode}）：\n{(proc.stdout or '')[-2000:]}\n"
            f"{(proc.stderr or '')[-2000:]}"
        )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    job = payload.get("job") or {}
    return {
        "tag": tag,
        "mode": job.get("mode"),
        "files_analyzed": job.get("files_analyzed"),
        "lines_changed": job.get("lines_changed"),
        "chunks": job.get("chunks"),
        "calls": (job.get("usage") or {}).get("calls"),
        "prompt_tokens": (job.get("usage") or {}).get("prompt_tokens"),
        "cached_tokens": (job.get("usage") or {}).get("cached_tokens"),
        "completion_tokens": (job.get("usage") or {}).get("completion_tokens"),
        "cost_micros": (job.get("usage") or {}).get("cost_micros"),
        "findings": job.get("findings_reported"),
        "degrade_notes": job.get("degrade_notes"),
    }


def main() -> int:
    WORKDIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="acra-incr-") as tmp:
        shas = build_repo(Path(tmp))
        print(f"仓库就绪：A={shas['a'][:8]} B={shas['b'][:8]}", flush=True)

        runs = [
            run_review(
                repo=Path(tmp) / "demo-incremental",
                base="main",
                head=shas["a"],
                tag="run1-full",
                force_full=True,
            ),
            run_review(
                repo=Path(tmp) / "demo-incremental",
                base="main",
                head=shas["b"],
                tag="run2-incremental",
                force_full=False,
            ),
            run_review(
                repo=Path(tmp) / "demo-incremental",
                base="main",
                head=shas["b"],
                tag="run3-full",
                force_full=True,
            ),
        ]

    by_tag = {r["tag"]: r for r in runs}
    incr, full = by_tag["run2-incremental"], by_tag["run3-full"]

    def yuan(micros: int | None) -> str:
        return f"¥{(micros or 0) / 1_000_000:.6f}"

    print("")
    print(f"{'运行':<18}{'mode':<14}{'块':>4}{'输入':>9}{'缓存命中':>10}{'输出':>8}{'成本':>14}")
    for r in runs:
        print(
            f"{r['tag']:<18}{str(r['mode']):<14}{r['chunks'] or 0:>4}"
            f"{r['prompt_tokens'] or 0:>9}{r['cached_tokens'] or 0:>10}"
            f"{r['completion_tokens'] or 0:>8}{yuan(r['cost_micros']):>14}"
        )

    saving_cost = None
    saving_tokens = None
    if full["cost_micros"]:
        saving_cost = 1 - (incr["cost_micros"] or 0) / full["cost_micros"]
    if full["prompt_tokens"]:
        saving_tokens = 1 - (incr["prompt_tokens"] or 0) / full["prompt_tokens"]

    print("")
    if incr["mode"] != "incremental":
        print(f"⚠ run2 的 mode 是 {incr['mode']!r}，增量判定没有生效，下面的降幅不成立。")
    if saving_cost is not None:
        print(f"成本降幅（增量 vs 同目标全量）：{saving_cost * 100:.1f}%")
    if saving_tokens is not None:
        print(f"输入 token 降幅：{saving_tokens * 100:.1f}%")

    payload = {
        "pr_number": PR_NUMBER,
        "shas": shas,
        "runs": runs,
        "cost_saving": saving_cost,
        "input_token_saving": saving_tokens,
        "incremental_effective": incr["mode"] == "incremental",
    }
    (ROOT / ".acra-work" / "incremental_cost.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("明细已写入 .acra-work/incremental_cost.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
