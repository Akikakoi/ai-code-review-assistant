"""构造一个带真实缺陷的演示仓库，用于 acra 端到端验收。

三个变更点刻意对应三类典型缺陷（也是文档 G1 点名的"真实缺陷"）：

1. `OrderService.pay` —— 空值校验从 `||` 改成 `&&`，null 入参直接 NPE（bug）
2. `OrderRepository.findByStatus` —— 字符串拼接构造 SQL（security）
3. `FileExportService.export` —— 新增流未在异常路径关闭（资源泄漏）

用法：
    python examples/build_demo_repo.py [目标目录]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ORDER_SERVICE_BASE = """package com.example.order;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class OrderService {

    private final Map<String, Order> orders = new ConcurrentHashMap<>();

    public String pay(String userId, Order order) {
        if (userId == null || userId.isEmpty()) {
            throw new IllegalArgumentException("userId must not be empty");
        }
        orders.put(userId, order);
        return doPay(userId, order);
    }

    private String doPay(String userId, Order order) {
        return "ok:" + userId;
    }
}
"""

ORDER_SERVICE_HEAD = """package com.example.order;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class OrderService {

    private final Map<String, Order> orders = new ConcurrentHashMap<>();

    public String pay(String userId, Order order) {
        if (userId == null && userId.isEmpty()) {
            throw new IllegalArgumentException("userId must not be empty");
        }
        orders.put(userId, order);
        return doPay(userId, order);
    }

    private String doPay(String userId, Order order) {
        return "ok:" + userId;
    }
}
"""

ORDER_REPOSITORY_BASE = """package com.example.order;

import java.util.List;

public class OrderRepository {

    private final JdbcTemplate jdbc;

    public List<Order> findByStatus(String status) {
        return jdbc.query("select * from orders where status = ?", status);
    }
}
"""

ORDER_REPOSITORY_HEAD = """package com.example.order;

import java.util.List;

public class OrderRepository {

    private final JdbcTemplate jdbc;

    public List<Order> findByStatus(String status) {
        String sql = "select * from orders where status = '" + status + "'";
        return jdbc.query(sql);
    }
}
"""

FILE_EXPORT_BASE = """package com.example.order;

import java.io.FileInputStream;
import java.io.IOException;

public class FileExportService {

    public String export(String path) throws IOException {
        try (FileInputStream in = new FileInputStream(path)) {
            return new String(in.readAllBytes());
        }
    }
}
"""

FILE_EXPORT_HEAD = """package com.example.order;

import java.io.FileInputStream;
import java.io.IOException;

public class FileExportService {

    public String export(String path) throws IOException {
        FileInputStream in = new FileInputStream(path);
        byte[] data = in.readAllBytes();
        return new String(data);
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

FILES_BASE = {
    "src/main/java/com/example/order/OrderService.java": ORDER_SERVICE_BASE,
    "src/main/java/com/example/order/OrderRepository.java": ORDER_REPOSITORY_BASE,
    "src/main/java/com/example/order/FileExportService.java": FILE_EXPORT_BASE,
    # 同包类型，不被本次修改触及；L2 的"被引用类型签名"靠它来验证
    # （Java 同包不写 import，只能通过同目录兄弟文件解析出来）
    "src/main/java/com/example/order/Order.java": ORDER_TYPE,
    "README.md": "# demo-repo\n\nacra 端到端验收用的演示仓库。\n",
}

FILES_HEAD = {
    "src/main/java/com/example/order/OrderService.java": ORDER_SERVICE_HEAD,
    "src/main/java/com/example/order/OrderRepository.java": ORDER_REPOSITORY_HEAD,
    "src/main/java/com/example/order/FileExportService.java": FILE_EXPORT_HEAD,
}

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


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def build(target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    git(target, "init", "-q", "-b", "main")
    write(target, FILES_BASE)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "base: 初始实现")
    git(target, "checkout", "-q", "-b", "feature/order-hardening")
    write(target, FILES_HEAD)
    git(target, "add", "-A")
    git(target, "commit", "-q", "-m", "feat: 收紧校验并补充查询与导出逻辑")
    return target


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "demo-repo"
    repo = build(out)
    print(repo)
    print(git(repo, "log", "--oneline"))
