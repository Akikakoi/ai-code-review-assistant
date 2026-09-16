"""离线检查 L3 上下文是否真的取到了仓库级线索。

不调用模型、不花钱。要回答的问题只有一个：**L3 到底有没有内容进提示词**。

在此之前三类线索（反向引用 / 相似实现 / 历史评论）是"接口在、渲染在、提示词留了位置，
但没有任何东西去填"—— 这种状态最容易被误读成"L3 已经做好了"，
因为 `callers=[]` 是合法的返回值，跟真的没有调用方长得一样。

用法：
    ./.venv/Scripts/python.exe scripts/inspect_l3.py [仓库路径] [base] [head]
默认用 `examples/build_l3_demo.py` 生成的演示仓库（先跑一次生成）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from acra.analysis import risk_rules
from acra.context.builder import ContextBuilder
from acra.context.retriever import Retriever
from acra.engine.prompt_loader import repo_content_block
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set
from acra.repo.symbol_index import SymbolIndex
from acra.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "examples" / "l3-demo")
    base = sys.argv[2] if len(sys.argv) > 2 else "main"
    head = sys.argv[3] if len(sys.argv) > 3 else "l3-demo-head"

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

    # 向量路径：配置开启时按与 pipeline 相同的规则构建索引
    vector_index = None
    if settings.acra_l3_vector_enabled and repo_files:
        import hashlib

        from acra.context.vector import MethodVectorIndex, pick_backend

        backend, why = pick_backend(settings)
        if backend is None:
            print(f"⚠ 向量后端不可用：{why}")
        else:
            key = hashlib.sha256(str(handle.root).encode("utf-8")).hexdigest()[:16]
            vector_index = MethodVectorIndex(
                settings.ensure_workdir() / "l3-index" / f"{key}.sqlite", backend
            )
            print(f"向量后端：{backend.name}")

    retriever = Retriever(
        handle, settings, head_sha=head_sha, repo_files=repo_files, vector_index=vector_index
    )
    builder = ContextBuilder(
        handle, settings, head_sha=head_sha, symbol_index=SymbolIndex(), repo_files=repo_files
    )

    print(f"仓库文件 {len(repo_files)} 个；本次变更 {len(diff_set.files)} 个文件")
    print(f"L3 策略：应然={retriever.strategy}  实然={retriever.effective_strategy}")
    print(f"扫描配额 {retriever.max_scan_files} / 调用方上限 {retriever.max_callers} / 相似上限 {retriever.max_similar}")
    print("")

    failures: list[str] = []
    for file_diff in diff_set.files:
        triggers = risk_rules.l3_triggers(file_diff)
        ctx = builder.build(file_diff, level=3, allow_l3=True, retriever=retriever)
        block = repo_content_block(ctx)

        print(f"=== {file_diff.path} ===")
        print(f"  L3 触发条件 : {triggers or '（无，本次由 --level 3 强制打开）'}")
        print(f"  变更方法    : {[s.name for s in ctx.enclosing_symbols]}")
        print(f"  反向引用    : {[(c.caller_path, c.caller_line, c.symbol) for c in ctx.callers]}")
        print(
            "  相似实现    : "
            f"{[(s.path, s.start_line, s.score) for s in ctx.similar_impls]}"
        )
        print(f"  历史评论    : {[(c.path, c.line) for c in ctx.prior_comments]}")
        print(f"  L3 说明     : {ctx.l3_notes}")
        print(f"  降级说明    : {ctx.degraded}")
        print(f"  l3_text 大小: {len(ctx.l3_text)} 字符")
        if ctx.l3_text:
            print("  --- l3_text 前 240 字 ---")
            print("  " + ctx.l3_text[:240].replace("\n", "\n  "))
        print(f"  提示词里含 L3 段落: {_l3_in_prompt(block)}")
        print("")

        if not ctx.l3_text:
            failures.append(f"{file_diff.path}: l3_text 为空")
        elif not _l3_in_prompt(block):
            failures.append(f"{file_diff.path}: l3_text 非空但没进提示词")

    print("结论：" + ("L3 线索已真实进入提示词" if not failures else "；".join(failures)))
    return 0 if not failures else 1


def _l3_in_prompt(block: str) -> list[str]:
    marks = []
    for label in ("调用方（判断影响面）", "仓库既有相似实现", "该文件历史审查意见"):
        if label in block:
            marks.append(label)
    return marks


if __name__ == "__main__":
    raise SystemExit(main())
