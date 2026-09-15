"""提示词模板加载与消息装配。

对应开发文档 §8。

模板用 `string.Template`（`$name` 占位）而不是 `str.format`：扫描提示里会注入大量
源码，Java/TS 代码中的花括号会让 `str.format` 直接抛异常，而 `$` 在源码中出现概率低，
且 Template 不会对注入值做二次替换，安全性更好。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from string import Template

from acra import PROMPT_TEMPLATE_VERSION
from acra.context.builder import render_callers, render_imports, render_similar, render_types
from acra.errors import PromptNotFound
from acra.models import Chunk, FileContext

PROMPT_DIR = Path(__file__).with_name("prompts")

UNTRUSTED_OPEN = "<<<UNTRUSTED_REPO_CONTENT id={ident}>>>"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_REPO_CONTENT>>>"


@lru_cache(maxsize=32)
def load_template(name: str, version: str | None = None) -> Template:
    """加载 `<name>_v<version>.txt`。"""
    version = version or PROMPT_TEMPLATE_VERSION
    path = PROMPT_DIR / f"{name}_v{version}.txt"
    if not path.exists():
        raise PromptNotFound(f"提示词模板不存在：{path}")
    return Template(path.read_text(encoding="utf-8"))


def available_versions() -> list[str]:
    versions: set[str] = set()
    for path in PROMPT_DIR.glob("*_v*.txt"):
        stem = path.stem
        if "_v" in stem:
            versions.add(stem.rsplit("_v", 1)[1])
    return sorted(versions)


def render(template: Template, **values: object) -> str:
    return template.substitute(**{k: _stringify(v) for k, v in values.items()})


def _stringify(value: object) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else list(value)
        return ", ".join(str(x) for x in items)
    if value is None:
        return ""
    return str(value)


# ---------------------------------------------------------------------------- 不可信输入


def wrap_untrusted(text: str) -> str:
    """用不可与代码自然混淆的定界符包裹仓库内容（文档 §8.4 输入隔离）。"""
    ident = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
    return "\n".join(
        [
            UNTRUSTED_OPEN.format(ident=ident),
            text,
            UNTRUSTED_CLOSE,
        ]
    )


def repo_content_block(ctx: FileContext) -> str:
    """组装一次扫描中的仓库内容部分（整体作为不可信输入）。"""
    parts: list[str] = []

    static_text = ctx.static_text or "(本次未启用静态检查)"
    parts.append("=== 静态检查结果（工具产出的事实，不要复述原文）===")
    parts.append(static_text or "(无)")

    parts.append("=== 变更所在方法的完整源码（含行号）===")
    parts.append(ctx.enclosing_source_text or "(无可用方法上下文)")

    if ctx.imports:
        parts.append("=== 该文件 import 列表 ===")
        parts.append(render_imports(ctx.imports))

    if ctx.referenced_types:
        parts.append("=== 被引用类型的签名 ===")
        parts.append(render_types(ctx.referenced_types))

    parts.append("=== 本次变更 diff（左侧为真实行号，+ 为新增行）===")
    parts.append(ctx.diff_text or "(无)")

    if ctx.similar_impls:
        parts.append("=== 仓库既有相似实现（仅供参考项目约定，不是待审代码）===")
        parts.append(render_similar(ctx.similar_impls))

    if ctx.callers:
        parts.append("=== 调用方（判断影响面）===")
        parts.append(render_callers(ctx.callers))

    if ctx.prior_comments:
        parts.append("=== 该文件历史审查意见（不要重复提出）===")
        parts.append(
            "\n".join(f"- {c.path}:{c.line} {' '.join(c.body.split())[:200]}" for c in ctx.prior_comments)
        )

    return wrap_untrusted("\n\n".join(parts))


# ---------------------------------------------------------------------------- 消息装配


def build_scan_messages(
    chunk: Chunk,
    *,
    custom_conventions: str = "",
    version: str | None = None,
) -> list[dict[str, str]]:
    """Phase 1 扫描提示（文档 §8.2）。"""
    system = render(load_template("system", version), custom_conventions=custom_conventions or "(空)")
    user = render(
        load_template("scan", version),
        index=chunk.index,
        total=chunk.total,
        path=chunk.path,
        change_type=chunk.change_type,
        added_line_numbers=chunk.added_line_numbers,
        repo_content_block=repo_content_block(chunk.context),
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_verify_messages(
    *,
    path: str,
    line: int,
    category: str,
    title: str,
    body: str,
    repo_content_block: str,
    version: str | None = None,
) -> list[dict[str, str]]:
    """Phase 2 验证提示（文档 §8.3）。"""
    system = render(load_template("system", version), custom_conventions="(空)")
    user = render(
        load_template("verify", version),
        path=path,
        line=line,
        category=category,
        title=title,
        body=body,
        repo_content_block=repo_content_block,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_summary_messages(
    *,
    files_analyzed: int,
    lines_changed: int,
    finding_count: int,
    severity_breakdown: str,
    degrade_notes: str,
    finding_list: str,
    version: str | None = None,
) -> list[dict[str, str]]:
    """Summary 生成提示（阶段一使用确定性渲染，此接口供阶段二切换为 LLM 摘要）。"""
    user = render(
        load_template("summary", version),
        files_analyzed=files_analyzed,
        lines_changed=lines_changed,
        finding_count=finding_count,
        severity_breakdown=severity_breakdown,
        degrade_notes=degrade_notes or "(无)",
        finding_list=finding_list,
    )
    return [{"role": "user", "content": user}]
