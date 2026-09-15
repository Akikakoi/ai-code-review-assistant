# 数据库迁移

阶段一使用 `Base.metadata.create_all()` 建表（`acra.store.db.Database.create_all`），
因为此时表结构尚未稳定、也没有需要保数据的历史运行记录。

引入 Alembic 的时机：**第一次需要在生产库上做增量变更时**（阶段二接入评估数据集之后，
`eval_case` / `eval_result` 会开始积累需要保留的数据）。

接入方式（预留说明，避免阶段二重新设计）：

```bash
pip install alembic
alembic init src/acra/store/migrations
# 在 alembic/env.py 里
#   from acra.store.models import Base
#   target_metadata = Base.metadata
alembic revision --autogenerate -m "add eval tables"
```

注意事项：

- `store/models.py` 里 `TEXT[]` / `JSONB` 用 `with_variant` 抹平了 SQLite 与 PostgreSQL
  的差异。autogenerate 在 SQLite 上会产出错误的方言类型，**迁移必须在 PostgreSQL 上生成**。
- `idx_review_run_status` 在文档 §5.2 里是部分索引
  （`WHERE status IN ('queued','running')`），SQLite 支持该语法，PostgreSQL 也支持，
  当前用普通索引代替；迁移到 Alembic 时补成 `postgresql_where` 条件索引。
