"""用本地 `git init` 构造临时仓库，使集成测试完全不依赖网络（文档 §14.2）。

刻意覆盖的边界：rename、新增/删除文件、纯删除 hunk、无换行结尾、CRLF、二进制。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_ID = [
    "-c",
    "user.email=acra@test.local",
    "-c",
    "user.name=acra test",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
]


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *GIT_ID, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} 失败：{proc.stderr}")
    return proc.stdout


def init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    return root


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def write_files(repo: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")


ORDER_SERVICE_BASE = """package com.example.order;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class OrderService {

    private final Map<String, Order> orders = new ConcurrentHashMap<>();

    public String pay(String userId, Order order) {
        if (userId == null || userId.isEmpty()) {
            throw new IllegalArgumentException("userId empty");
        }
        return doPay(userId, order);
    }

    private String doPay(String userId, Order order) {
        return "ok:" + userId;
    }
}
"""

ORDER_SERVICE_BUGGY = """package com.example.order;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class OrderService {

    private final Map<String, Order> orders = new ConcurrentHashMap<>();

    public String pay(String userId, Order order) {
        if (userId == null && userId.isEmpty()) {
            throw new IllegalArgumentException("userId empty");
        }
        return doPay(userId, order);
    }

    private String doPay(String userId, Order order) {
        return "ok:" + userId;
    }
}
"""


ORDER_TYPE = """package com.example.order;

public class Order {

    private String id;

    private long amount;

    public String getId() {
        return id;
    }

    public long getAmount() {
        return amount;
    }
}
"""


def make_java_bug_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """构造一个"把空值校验写反"的真实缺陷仓库。

    返回 (仓库路径, base_ref, head_ref)。

    同时放入同包的 `Order.java`：它不被修改，但 `OrderService` 的方法签名里用到了
    `Order` 类型 —— L2 的"被引用类型签名"正是靠同包兄弟文件解析出来的（Java 同包不 import）。
    """
    repo = init_repo(tmp_path / "repo")
    write_files(
        repo,
        {
            "src/main/java/com/example/order/OrderService.java": ORDER_SERVICE_BASE,
            "src/main/java/com/example/order/Order.java": ORDER_TYPE,
            "README.md": "# demo\n",
        },
    )
    commit_all(repo, "base: 正常的空值校验")

    git(repo, "checkout", "-q", "-b", "feature/order")
    write_files(
        repo,
        {"src/main/java/com/example/order/OrderService.java": ORDER_SERVICE_BUGGY},
    )
    commit_all(repo, "feat: 收紧 userId 校验")

    return repo, "main", "feature/order"


LEGACY_SERVICE_V1 = """package com.example.legacy;

public class LegacyService {
    public int compute(int a) {
        return a + 1;
    }
}
"""

LEGACY_SERVICE_V2 = """package com.example.legacy;

public class LegacyService {
    public int compute(int a) {
        return a - 1;
    }
}
"""

NO_NEWLINE_JAVA = "package com.example;\n\nclass NoNewline {\n    int x = 1;\n}"


def make_edge_case_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """构造覆盖多种 diff 边界的仓库。

    刻意用源码文件而不是 `.txt`：`.txt` / `.md` 属于低价值路径，会被预算守卫直接跳过，
    那样就测不到 rename / CRLF / 二进制这些解析边界了。

    覆盖：rename + 内容变更、纯删除、CRLF、无结尾换行、二进制、纯文档变更。
    """
    repo = init_repo(tmp_path / "edge")
    write_files(
        repo,
        {
            "keep.txt": "keep\n",
            "old/LegacyService.java": LEGACY_SERVICE_V1,
            "to_delete/Gone.java": "package com.example;\n\nclass Gone {}\n",
            "Crlf.java": "package com.example;\r\n\r\nclass Crlf {\r\n    int a = 1;\r\n}\r\n",
            "blob.bin": b"\x00\x01\x02\x03binarycontent",
        },
    )
    commit_all(repo, "base")

    git(repo, "checkout", "-q", "-b", "feature/edge")
    write_files(repo, {"keep.txt": "keep\nchanged\n"})
    # git mv 的目标目录必须先存在，否则报 "No such file or directory"
    (repo / "new").mkdir(parents=True, exist_ok=True)
    git(repo, "mv", "old/LegacyService.java", "new/LegacyService.java")
    write_files(
        repo,
        {
            "new/LegacyService.java": LEGACY_SERVICE_V2,  # rename 之后再改内容
            "Crlf.java": "package com.example;\r\n\r\nclass Crlf {\r\n    int a = 2;\r\n}\r\n",
            "blob.bin": b"\x00\xff\xfeotherbinary",
            "NoNewline.java": NO_NEWLINE_JAVA,
        },
    )
    (repo / "to_delete/Gone.java").unlink()
    commit_all(repo, "feat: 多边界变更")
    return repo, "main", "feature/edge"


def run_git_diff(repo: Path, base: str, head: str) -> str:
    return git(
        repo,
        "diff",
        "--find-renames",
        "--find-copies",
        "--unified=0",
        "--no-color",
        f"{base}..{head}",
    )
