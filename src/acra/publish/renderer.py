"""summary 与评论正文渲染、CLI 三种输出格式。

对应开发文档 §4.9。

- `render_comment_body`：单条行级评论正文，必须写清"为什么这是问题"（P4 可解释）；
- `render_summary`：按 §4.9 的 Summary 结构，含"分析范围与降级说明"折叠区；
- `render_text` / `render_json` / `render_sarif`：CLI 输出（`--format`）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from acra.analysis.sarif import to_sarif
from acra.models import SEVERITY_RANK, Finding

SEVERITY_LABEL = {
    "blocker": "阻断",
    "high": "高",
    "medium": "中",
    "low": "低",
    "nit": "细枝",
}

CATEGORY_LABEL = {
    "bug": "缺陷",
    "security": "安全",
    "concurrency": "并发",
    "performance": "性能",
    "maintainability": "可维护性",
    "test": "测试",
    "style": "风格",
}

SOURCE_LABEL = {
    "llm": "模型分析",
    "static": "静态工具（未经模型判断）",
}

FOOTER = "<sub>由 acra 生成 · 回复 `/acra ignore` 可跳过本 PR 后续审查</sub>"

#: 机器可读的标识：渲染后不可见，用来判断"这条 review 是不是本工具发的"。
#:
#: **必须同时出现在 summary 与行级评论里。** `GithubPublisher.existing_review_for_head`
#: 靠它做幂等 —— 如果只有 `FOOTER` 里那句人类可见的中文（"由 acra 生成"），
#: 那么改一次文案就会让幂等**静默失效**，同一 head_sha 每次重跑都再发一条 review。
#: 实测确认过：E2E 里 summary 从未包含过这个标记，幂等当时是靠中文那半撑着的。
BOT_MARKER = "<!-- acra:review -->"


@dataclass(slots=True)
class SummaryInputs:
    files_analyzed: int = 0
    lines_changed: int = 0
    context_level_max: int = 2
    static_tools: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    degrade_notes: list[str] = field(default_factory=list)
    mode: str = "full"
    ai_available: bool = True
    l3_used: bool = False


def render_comment_body(finding: Finding) -> str:
    """行级评论正文。"""
    origin = "" if finding.source == "llm" else f" · 来源：{SOURCE_LABEL.get(finding.source, finding.source)}"
    lines: list[str] = [
        f"**严重度：{SEVERITY_LABEL.get(finding.severity, finding.severity)}**"
        f" · {CATEGORY_LABEL.get(finding.category, finding.category)}"
        f" · 置信度 {finding.confidence:.2f}{origin}",
        "",
        f"**{finding.title}**",
        "",
        finding.body,
    ]
    if finding.suggestion:
        lines += ["", "```suggestion", finding.suggestion.rstrip(), "```"]
    if finding.evidence:
        lines += ["", "<details><summary>依据</summary>", ""]
        for item in finding.evidence[:6]:
            lines.append(f"- `{item}`")
        lines += ["", "</details>"]
    lines += ["", BOT_MARKER]
    return "\n".join(lines)


def review_comments_payload(findings: Iterable[Finding]) -> list[dict[str, Any]]:
    """GitHub `POST /pulls/{n}/reviews` 的 comments 数组。"""
    return [
        {
            "path": f.path,
            "line": f.line,
            "side": "RIGHT",
            "body": render_comment_body(f),
        }
        for f in findings
    ]


def render_summary(
    findings: Iterable[Finding],
    inputs: SummaryInputs,
    *,
    title: str = "## 代码审查摘要",
) -> str:
    """§4.9 的 Summary 结构。"""
    findings = list(findings)
    counts = _severity_counts(findings)
    total = len(findings)

    if total == 0:
        lines = [title, "", "本次变更未发现值得修改的问题。"]
    else:
        breakdown = " / ".join(
            f"{SEVERITY_LABEL[k]} {v}" for k, v in counts.items() if v
        )
        lines = [title, "", f"共发现 {total} 个问题（{breakdown}），已按重要性排序。", ""]

        high = [f for f in findings if f.severity in ("blocker", "high")]
        if high:
            lines.append("### 高优先级")
            for f in high:
                lines.append(f"- `{f.path}:{f.line}` {f.title}")
            lines.append("")

        others = [f for f in findings if f.severity not in ("blocker", "high")]
        if others:
            lines.append("<details><summary>其他问题</summary>")
            lines.append("")
            for f in others:
                lines.append(f"- `{f.path}:{f.line}` {f.title}")
            lines += ["", "</details>"]

    lines += ["", _scope_block(inputs), "", FOOTER, BOT_MARKER]
    return "\n".join(lines)


def _severity_counts(findings: list[Finding]) -> dict[str, int]:
    ordered = sorted(findings, key=lambda f: SEVERITY_RANK.get(f.severity, 9))
    counts: dict[str, int] = {}
    for f in ordered:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _scope_block(inputs: SummaryInputs) -> str:
    level_desc = {
        1: "L1（仅 diff）",
        2: "L1 + L2（未触发仓库级检索）",
        3: "L1 + L2 + L3（含仓库级线索）",
    }.get(inputs.context_level_max, f"L{inputs.context_level_max}")

    lines = [
        "<details><summary>本次分析范围与降级说明</summary>",
        "",
        f"- 分析文件 {inputs.files_analyzed} 个，变更行 {inputs.lines_changed} 行",
        f"- 已启用上下文层级：{level_desc}",
        f"- 静态检查：{'、'.join(inputs.static_tools) if inputs.static_tools else '未启用'}",
        f"- AI 分析：{'正常' if inputs.ai_available else '不可用（已降级）'}",
        f"- 运行模式：{inputs.mode}",
        f"- 成本：输入 {inputs.input_tokens:,} tokens / 输出 {inputs.output_tokens:,} tokens",
    ]
    if inputs.cost_micros:
        lines.append(f"- 成本估算：{inputs.cost_micros / 1_000_000:.4f} 元")
    if inputs.degrade_notes:
        lines.append("- 降级说明：" + "；".join(inputs.degrade_notes))
    lines += ["", "</details>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- CLI 输出


def render_text(
    findings: list[Finding],
    *,
    summary_inputs: SummaryInputs | None = None,
    dropped_count: int = 0,
    head_sha: str = "",
) -> str:
    """给人看的纯文本输出。"""
    out: list[str] = []
    if head_sha:
        out.append(f"acra · head={head_sha[:12]}")
        out.append("")

    if not findings:
        out.append("未发现值得修改的问题。")
    for f in sorted(findings, key=lambda x: x.sort_key()):
        sev = SEVERITY_LABEL.get(f.severity, f.severity)
        out.append(f"[{sev}] {f.path}:{f.line}  ({f.category}, conf={f.confidence:.2f}, score={f.score:.2f})")
        out.append(f"      {f.title}")
        for line in f.body.split("\n"):
            if line.strip():
                out.append(f"      {line}")
        if f.suggestion:
            out.append("      --- 建议改写 ---")
            for line in f.suggestion.split("\n"):
                out.append(f"      {line}")
        out.append("")

    if summary_inputs is not None:
        out.append(
            f"共 {len(findings)} 条结论；候选被丢弃 {dropped_count} 条；"
            f"上下文最高层级 L{summary_inputs.context_level_max}；"
            f"tokens 输入 {summary_inputs.input_tokens} / 输出 {summary_inputs.output_tokens}"
        )
        if summary_inputs.degrade_notes:
            out.append("降级：" + "；".join(summary_inputs.degrade_notes))
    return "\n".join(out)


def render_json(
    findings: list[Finding],
    *,
    summary_inputs: SummaryInputs | None = None,
    dropped: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "findings": [f.to_dict() for f in sorted(findings, key=lambda x: x.sort_key())],
    }
    if summary_inputs is not None:
        payload["stats"] = {
            "files_analyzed": summary_inputs.files_analyzed,
            "lines_changed": summary_inputs.lines_changed,
            "context_level_max": summary_inputs.context_level_max,
            "input_tokens": summary_inputs.input_tokens,
            "output_tokens": summary_inputs.output_tokens,
            "cost_micros": summary_inputs.cost_micros,
            "mode": summary_inputs.mode,
            "ai_available": summary_inputs.ai_available,
            "degrade_notes": summary_inputs.degrade_notes,
            "static_tools": summary_inputs.static_tools,
        }
    if dropped is not None:
        payload["dropped"] = dropped
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_sarif_json(findings: list[Finding]) -> str:
    return json.dumps(to_sarif(findings), ensure_ascii=False, indent=2)


def severity_exit_hit(findings: Iterable[Finding], threshold: str) -> bool:
    """`--fail-on` 判定：是否存在严重度 ≥ threshold 的结论。"""
    limit = SEVERITY_RANK.get(threshold, 9)
    return any(SEVERITY_RANK.get(f.severity, 9) <= limit for f in findings)
