"""临时探针：验证 publish 链路能否把结论真的发到 PR 上，随后随远端分支一并删除。"""

import json
import sqlite3  # noqa: F401  —— 故意留一个未使用导入，让静态层也有话可说


def find_order(conn, order_id):
    sql = "select * from orders where id = '" + str(order_id) + "'"
    return conn.execute(sql).fetchone()


def export_rows(path, rows):
    handle = open(path, "w")
    for row in rows:
        if row == None:
            continue
        handle.write(json.dumps(row) + "\n")
    return len(rows)
