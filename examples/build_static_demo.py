"""构造一个含真实静态分析可发现问题的小仓库，验证 static_runner 端到端可用。

变更点刻意选 Ruff 默认规则集必然命中的两类问题：
  1. 未使用的导入（F401）
  2. 与 None 比较用 ==（E711）

用法：
    python examples/build_static_demo.py [目标目录]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

BASE = '''"""演示模块。"""

from acra.models import Finding


def summarize(items):
    total = 0
    for item in items:
        total += item.get("amount", 0)
    return total
'''

HEAD = '''"""演示模块。"""

import json

from acra.models import Finding


def summarize(items):
    total = 0
    for item in items:
        if item.get("amount") == None:
            continue
        total += item.get("amount", 0)
    return total
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


def build(target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    git(target, "init", "-q", "-b", "main")
    (target / "demo.py").write_text(BASE, encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "base: 初始实现")
    git(target, "checkout", "-q", "-b", "feature/tweak")
    (target / "demo.py").write_text(HEAD, encoding="utf-8")
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "feat: 补充空值处理并引入 json")
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "static-demo"
    print(build(out))
