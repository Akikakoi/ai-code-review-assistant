"""核心数据结构。

对应开发文档 §4.3（Hunk / FileDiff）、§5.3（Finding JSON 契约）、§4.4（FileContext）、
§7.5（Symbol / CallSite）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

# --------------------------------------------------------------------------------------
# 枚举与权重
# --------------------------------------------------------------------------------------


class ChangeType(StrEnum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"
    BINARY = "binary"


class Category(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    CONCURRENCY = "concurrency"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    TEST = "test"
    STYLE = "style"


class Severity(StrEnum):
    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NIT = "nit"


#: 排序用：数值越小越靠前（文档 §9.3 排序键的第二顺位）
SEVERITY_RANK: dict[str, int] = {
    Severity.BLOCKER.value: 0,
    Severity.HIGH.value: 1,
    Severity.MEDIUM.value: 2,
    Severity.LOW.value: 3,
    Severity.NIT.value: 4,
}

#: 评分加成（文档 §9.2）
SEVERITY_BOOST: dict[str, float] = {
    Severity.BLOCKER.value: 0.30,
    Severity.HIGH.value: 0.20,
    Severity.MEDIUM.value: 0.10,
    Severity.LOW.value: 0.0,
    Severity.NIT.value: 0.0,
}

#: Finding 的来源。静态工具直接产出、未经模型判断的结论必须能被区分出来 ——
#: 作者看到"[静态] 未使用的导入"和"模型认为这里有并发问题"时，判断方式完全不同。
FindingSource = Literal["llm", "static"]

Verdict = Literal["confirmed", "rejected", "uncertain"]
TriggerSource = Literal["webhook", "cli", "manual", "schedule"]
RunMode = Literal["full", "incremental", "static_only"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "degraded"]


# --------------------------------------------------------------------------------------
# 仓库与 diff
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Hunk:
    """一个 unified diff hunk。文档 §4.3。"""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str = ""
    added: list[tuple[int, str]] = field(default_factory=list)  # (新文件行号, 内容)
    removed: list[tuple[int, str]] = field(default_factory=list)  # (旧文件行号, 内容)

    @property
    def context_anchor(self) -> int:
        """hunk 在新文件中的起始行，用于取邻接上下文。"""
        return self.new_start

    def old_to_new_offset(self) -> int:
        return self.new_start - self.old_start


@dataclass(slots=True)
class FileDiff:
    """单个文件的变更。文档 §4.3。"""

    path: str
    change_type: ChangeType
    old_path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def added_line_numbers(self) -> set[int]:
        """整个系统的关键工件：合法行号白名单。"""
        out: set[int] = set()
        for h in self.hunks:
            for ln, _ in h.added:
                out.add(ln)
        return out

    @property
    def removed_line_numbers(self) -> set[int]:
        out: set[int] = set()
        for h in self.hunks:
            for ln, _ in h.removed:
                out.add(ln)
        return out

    @property
    def added_count(self) -> int:
        return sum(len(h.added) for h in self.hunks)

    @property
    def removed_count(self) -> int:
        return sum(len(h.removed) for h in self.hunks)

    def added_text(self) -> str:
        lines = [text for h in self.hunks for _, text in h.added]
        return "\n".join(lines)

    def unified_text(self, context_lines: int = 0) -> str:
        """还原为可读的 diff 文本（仅用已解析出的信息，不再调用 git）。"""
        out: list[str] = [f"--- a/{self.old_path or self.path}", f"+++ b/{self.path}"]
        for h in self.hunks:
            out.append(
                f"@@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@"
            )
            for _, text in h.removed:
                out.append("-" + text)
            for _, text in h.added:
                out.append("+" + text)
        return "\n".join(out)


@dataclass(slots=True)
class DiffSet:
    """一次审查的完整 diff 视图。"""

    base_sha: str
    head_sha: str
    merge_base_sha: str
    files: list[FileDiff] = field(default_factory=list)

    @property
    def total_added_lines(self) -> int:
        return sum(f.added_count for f in self.files)

    @property
    def total_removed_lines(self) -> int:
        return sum(f.removed_count for f in self.files)

    def by_path(self) -> dict[str, FileDiff]:
        return {f.path: f for f in self.files}

    def whitelist(self) -> dict[str, set[int]]:
        return {f.path: f.added_line_numbers for f in self.files}

    def filter_paths(self, paths: set[str]) -> DiffSet:
        return DiffSet(
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            merge_base_sha=self.merge_base_sha,
            files=[f for f in self.files if f.path in paths],
        )


# --------------------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ReviewJob:
    """规范化的审查任务。文档 §4.1 / §4.2。"""

    job_id: str
    platform: str = "github"
    repo_full_name: str = "local/repo"
    repo_path: str = "."
    pr_number: int | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    trigger_source: TriggerSource = "cli"
    force_full: bool = False
    remote_url: str | None = None
    installation_id: int | None = None
    delivery_id: str | None = None


# --------------------------------------------------------------------------------------
# 静态分析
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class StaticFinding:
    """归一化后的静态分析结论（SARIF 子集）。文档 §4.5。"""

    tool: str
    rule_id: str
    severity: str
    path: str
    line: int
    message: str
    level: str = "warning"


# --------------------------------------------------------------------------------------
# 符号
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SymbolSpan:
    """变更行所属的语法节点（方法 / 类）。文档 §4.4 L2、§7.5。"""

    name: str
    kind: str  # method | constructor | function | class | interface | enum | record | lambda
    start_line: int
    end_line: int
    signature: str = ""
    parent: str | None = None
    source: str = ""
    truncated: bool = False

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.source)


@dataclass(slots=True)
class Symbol:
    name: str
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str | None = None


@dataclass(slots=True)
class CallSite:
    symbol: str
    caller_path: str
    caller_line: int
    snippet: str = ""


@dataclass(slots=True)
class TypeSig:
    name: str
    path: str
    signature: str


@dataclass(slots=True)
class PriorComment:
    path: str
    line: int
    body: str


@dataclass(slots=True)
class CodeSlice:
    path: str
    start_line: int
    end_line: int
    source: str
    score: float = 0.0


# --------------------------------------------------------------------------------------
# 上下文与分块
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class FileContext:
    """文档 §4.4。"""

    path: str
    level: int
    diff_text: str = ""
    enclosing_symbols: list[SymbolSpan] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    referenced_types: list[TypeSig] = field(default_factory=list)
    callers: list[CallSite] = field(default_factory=list)
    similar_impls: list[CodeSlice] = field(default_factory=list)
    prior_comments: list[PriorComment] = field(default_factory=list)
    static_findings: list[StaticFinding] = field(default_factory=list)
    token_estimate: int = 0
    degraded: list[str] = field(default_factory=list)
    source_lines: list[str] = field(default_factory=list)

    # 已按 token 预算装配完毕、可直接注入提示词的文本块（由 context_builder 填充）
    enclosing_source_text: str = ""
    static_text: str = ""
    l3_text: str = ""
    packed_note: str = ""
    #: 本次装配中真正被截断的分区名（l1/l2/static）。必须是"本次装配"的结果，
    #: 不能从 degraded 里反推 —— 否则会继承父上下文的截断状态，导致分块永远无法合并。
    truncated_sections: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Chunk:
    """一个待扫描的代码块。文档 §7.4。"""

    index: int
    total: int
    path: str
    change_type: str
    added_line_numbers: set[int]
    diff_text: str
    context: FileContext
    token_estimate: int = 0
    truncated: bool = False


@dataclass(slots=True)
class ReviewContext:
    """一次审查的全局上下文。"""

    head_sha: str
    risk_categories: set[str] = field(default_factory=set)
    prior_comments: list[PriorComment] = field(default_factory=list)
    static_findings: list[StaticFinding] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Finding
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RawFinding:
    """LLM 直接产出的候选结论（LLM 层）。文档 §5.3。"""

    path: str
    line: int
    category: str
    severity: str
    confidence: float
    title: str
    body: str
    end_line: int | None = None
    suggestion: str | None = None
    evidence: list[str] = field(default_factory=list)
    needs_human_judgment: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RawFinding:
        return cls(
            path=str(d.get("path", "")),
            line=int(d.get("line", 0) or 0),
            category=str(d.get("category", "")),
            severity=str(d.get("severity", "")),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            title=str(d.get("title", "")),
            body=str(d.get("body", "")),
            end_line=d.get("end_line"),
            suggestion=d.get("suggestion"),
            evidence=list(d.get("evidence") or []),
            needs_human_judgment=bool(d.get("needs_human_judgment", False)),
            raw=d,
        )

    @property
    def dedupe_key(self) -> tuple[str, int, str]:
        return (self.path, self.line, self.category)


@dataclass(slots=True)
class Finding:
    """通过校验、可输出的结论。文档 §5.2 finding 表。"""

    path: str
    line: int
    category: str
    severity: str
    confidence: float
    score: float
    title: str
    body: str
    end_line: int | None = None
    suggestion: str | None = None
    evidence: list[str] = field(default_factory=list)
    evidence_rule_ids: list[str] = field(default_factory=list)
    needs_human_judgment: bool = False
    verify_verdict: str | None = None
    verify_reason: str | None = None
    merged_sources: int = 1
    published: bool = False
    published_comment_id: int | None = None
    #: "llm"（模型判断）或 "static"（静态工具直出，未经模型判断）
    source: str = "llm"
    #: 静态工具结论对应的规则 ID（source="static" 时有值）
    tool: str | None = None
    #: 展示优先级折扣（1.0 = 不打折）。目前由 Verify 的 uncertain 判定设置：
    #: 它影响排序与是否进入折叠区，**不影响 confidence**（真值估计）。
    score_penalty: float = 1.0

    @classmethod
    def from_raw(cls, raw: RawFinding, *, score: float) -> Finding:
        return cls(
            path=raw.path,
            line=raw.line,
            end_line=raw.end_line,
            category=raw.category,
            severity=raw.severity,
            confidence=raw.confidence,
            score=score,
            title=raw.title,
            body=raw.body,
            suggestion=raw.suggestion,
            evidence=list(raw.evidence),
            needs_human_judgment=raw.needs_human_judgment,
        )

    def sort_key(self) -> tuple[float, int, int]:
        """文档 §9.3：score 降序 → severity 名次升序 → 行号升序。"""
        return (-self.score, SEVERITY_RANK.get(self.severity, 9), self.line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "category": self.category,
            "severity": self.severity,
            "confidence": round(self.confidence, 4),
            "score": round(self.score, 4),
            "title": self.title,
            "body": self.body,
            "suggestion": self.suggestion,
            "evidence": self.evidence,
            "evidence_rule_ids": self.evidence_rule_ids,
            "needs_human_judgment": self.needs_human_judgment,
            "verify_verdict": self.verify_verdict,
            "merged_sources": self.merged_sources,
            "source": self.source,
            "tool": self.tool,
        }


@dataclass(slots=True)
class DropRecord:
    """被校验丢弃的候选，用于可解释性与指标统计（文档 §9.1 八步流水线）。"""

    step: str
    reason: str
    raw: RawFinding | dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        raw = self.raw.raw if isinstance(self.raw, RawFinding) else dict(self.raw)
        return {"step": self.step, "reason": self.reason, "raw": raw}


# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """廉价 token 估算。

    不引入 tiktoken（避免与供应商绑定）。中英混排代码的经验系数约为 3.2 字符/token，
    取 3 作为保守上界，宁可高估也不要超预算。
    """
    if not text:
        return 0
    return max(1, len(text) // 3)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
