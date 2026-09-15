"""静态工具**应当**能检出的用例（用来验证静态路径本身）。

## 为什么单独放一组

`defects.py` 的 20 类模板是照着"让模型识别"写的，里面的 `Jdbc` / `Mailer` 之类
都是自写的桩类。用真实 Semgrep 一试就会发现：**一条都匹配不上** ——
静态规则匹配的是已知的真实库 API 与 sink，桩类不在其中。

于是出现一个隐蔽的假象："静态分析已接入"（接口、解析、过滤都有测试），
但在真实工具 + 真实代码上从未被验证过。这跟早前"static-only 模式其实什么都不展示"
是同一类问题：**接口就位 ≠ 端到端可用**。

## 这组用例的取舍

这里只用**语法型**规则能命中的写法（例如 SQL 字符串里做拼接）。
实测发现：命令注入、资源泄漏这类规则是**污点分析型**的，需要可识别的污点源
（HTTP 参数、Servlet 输入等）才成立；一个孤立的 `Runtime.exec(param)` 片段
不会被报出来 —— 没有源就没有流。要让这组覆盖污点规则，得写真实框架代码
（Spring Controller / Servlet），成本明显更高，暂时不做，但**限制写在这里**，
免得下次误以为"静态分析没报 = 代码没问题"。
"""

from __future__ import annotations

from acra.eval.dataset import EvalCase, Expectation

STATIC_PKG = "src/main/java/com/example/staticprobe"


def static_cases() -> list[EvalCase]:
    """静态可检出的用例。实测每条都先用真实 Semgrep 验证过能触发。"""
    return [
        EvalCase(
            case_id="static-sql-concat",
            path=f"{STATIC_PKG}/LegacyOrderDao.java",
            language="java",
            kind="positive",
            source="static_probe",
            note="真实 JDBC API + 字符串拼接，Semgrep 的 formatted-sql-string 应当命中",
            base="""package com.example.staticprobe;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;

public class LegacyOrderDao {

    private final Connection connection;

    public LegacyOrderDao(Connection connection) {
        this.connection = connection;
    }

    public ResultSet findByStatus(String status) throws SQLException {
        PreparedStatement ps = connection.prepareStatement(
                "select * from orders where status = ?");
        ps.setString(1, status);
        return ps.executeQuery();
    }
}
""",
            head="""package com.example.staticprobe;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

public class LegacyOrderDao {

    private final Connection connection;

    public LegacyOrderDao(Connection connection) {
        this.connection = connection;
    }

    public ResultSet findByStatus(String status) throws SQLException {
        Statement statement = connection.createStatement();
        return statement.executeQuery("select * from orders where status = '" + status + "'");
    }
}
""",
            # 实测 Semgrep 在这条 `executeQuery` 上命中 formatted-sql-string
            expectations=[
                Expectation(
                    marker='statement.executeQuery("select * from orders where status = \'"',
                    category="security",
                    severity_min="high",
                    note="静态工具应检出（真实 JDBC sink + 拼接）",
                )
            ],
        ),
    ]
