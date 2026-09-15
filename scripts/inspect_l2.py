"""离线检查 L2 上下文是否真的把"被引用类型签名"装进去了。

不调用模型、不花钱，用来验证阶段二的 L2 完整实现：

    ./.venv/Scripts/python.exe scripts/inspect_l2.py [仓库路径] [base] [head]
"""

from __future__ import annotations

import sys
from pathlib import Path

from acra.context.builder import ContextBuilder
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set
from acra.repo.symbol_index import SymbolIndex
from acra.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "examples" / "demo-repo")
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    head = sys.argv[3] if len(sys.argv) > 3 else "feature/order-hardening"

    settings = get_settings()
    handle = gateway.discover_local(repo)
    merge_base = handle.merge_base(base, head)
    head_sha = handle.resolve_commit(head)
    diff_set = build_diff_set(
        handle.diff(merge_base, head_sha),
        base_sha=base,
        head_sha=head_sha,
        merge_base_sha=merge_base,
    )

    repo_files = handle.list_files(head_sha)
    print(f"仓库文件 {len(repo_files)} 个；本次变更 {len(diff_set.files)} 个文件\n")

    builder = ContextBuilder(
        handle,
        settings,
        head_sha=head_sha,
        symbol_index=SymbolIndex(),
        repo_files=repo_files,
    )

    for file_diff in diff_set.files:
        ctx = builder.build(file_diff)
        print(f"=== {file_diff.path} ===")
        print(f"  新增行      : {sorted(file_diff.added_line_numbers)}")
        print(f"  该文件 import: {len(ctx.imports)} 条")
        print(f"  所属方法    : {[(s.kind, s.name) for s in ctx.enclosing_symbols]}")
        print(f"  引用类型签名: {[(t.name, t.path.rsplit('/', 1)[-1]) for t in ctx.referenced_types]}")
        print(f"  降级说明    : {ctx.degraded}")
        print(f"  token 估算  : {ctx.token_estimate}（{ctx.packed_note}）")
        print()

    print(f"符号索引规模: {len(builder.symbol_index)} 个符号，"
          f"按需索引了 {len(builder._indexed_paths)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
