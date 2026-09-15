"""8 步校验流水线。

开发文档 §9.1 定义了 8 步。当前实现状态（阶段二）：

```
1  Schema 校验        字段齐全、枚举合法、长度合规          ← 已实现（含字段投影）
2  锚定校验           path 在本次变更文件列表中              ← 已实现
                      line ∈ added_line_numbers
3  类别校验           category 与 severity 的组合合理性     ← 已实现（超限则压到上限）
4  存量校验           该行是否属于本次新增                  ← 已实现（锚定 + 证据溯源）
5  交叉验证           若 evidence 引用了静态规则 ID…        ← 已实现（需静态分析开启）
6  重复校验           与已保留的 Finding 比对 → 合并        ← 见 dedupe.py
7  历史校验           命中 prior_comments 中已提过的        ← 阶段三（L3 上下文）
8  门槛过滤           confidence < threshold → 丢弃         ← 见 ranker.py
```

锚定失败即丢弃，**不做"降级为文件级评论"的妥协**（文档 P2）—— 那会成为噪音温床。

两处对 Schema 的工程化偏离（都在此显式记录，原则是**可修正的偏差就修正，只有幻觉才丢弃**）：

**一、字段投影。** 合并阶段会在 `raw` 上写入 `_merged_sources` 等私有键，模型也常额外输出
`reason` / `line_content` 之类的噪声键。校验时先做一次字段投影（只取 Schema 声明的字段、
剔除 `_` 前缀私有键）再严格校验，避免这些与结论实质无关的附加键把一条好结论整条打死。

**二、枚举先归一化再严格校验。** 模型的类别命名自由度很大，实测中出现过把"FileInputStream
未关闭"标成 `category=resource` 的情况 —— 结论完全正确、行号也锚得住，却因为自创了类别名
被整条丢弃。这不是幻觉，是可修复的命名偏差。因此 `normalize_enums` 先按同义词表把类别 /
严重度映射到合法取值（并记录原值供审计），映射后仍不合法的才丢弃。

枚举的"严格"保留在最终判定上，而不是保留在模型用词上。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from acra.models import (
    SEVERITY_RANK,
    Category,
    DiffSet,
    DropRecord,
    RawFinding,
    Severity,
)
from acra.repo.line_mapper import resolve_anchor

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "engine" / "schemas" / "finding.schema.json"

VALID_CATEGORIES = frozenset(c.value for c in Category)
VALID_SEVERITIES = frozenset(s.value for s in Severity)

#: 类别同义词 → 合法取值。只收窄语义明确、不会误判的映射；模棱两可的一律不收。
CATEGORY_SYNONYMS: dict[str, str] = {
    # 资源/空值/逻辑类，本质都是 bug
    "resource": "bug",
    "resource_leak": "bug",
    "resource_management": "bug",
    "memory_leak": "bug",
    "leak": "bug",
    "null": "bug",
    "null_safety": "bug",
    "null_pointer": "bug",
    "npe": "bug",
    "logic": "bug",
    "logic_error": "bug",
    "correctness": "bug",
    "error_handling": "bug",
    "exception": "bug",
    "exception_handling": "bug",
    "reliability": "bug",
    "edge_case": "bug",
    "boundary": "bug",
    "off_by_one": "bug",
    "crash": "bug",
    "data_integrity": "bug",
    # 安全
    "vulnerability": "security",
    "injection": "security",
    "sql_injection": "security",
    "sqli": "security",
    "xss": "security",
    "auth": "security",
    "authentication": "security",
    "authorization": "security",
    "access_control": "security",
    "crypto": "security",
    "privacy": "security",
    "secrets": "security",
    # 并发
    "thread_safety": "concurrency",
    "threadsafety": "concurrency",
    "race_condition": "concurrency",
    "race": "concurrency",
    "deadlock": "concurrency",
    "synchronization": "concurrency",
    "atomicity": "concurrency",
    "visibility": "concurrency",
    # 性能
    "perf": "performance",
    "efficiency": "performance",
    "n_plus_one": "performance",
    "n+1": "performance",
    "latency": "performance",
    "scalability": "performance",
    # 可维护性
    "readability": "maintainability",
    "design": "maintainability",
    "complexity": "maintainability",
    "duplication": "maintainability",
    "refactor": "maintainability",
    "refactoring": "maintainability",
    "code_smell": "maintainability",
    "codesmell": "maintainability",
    "documentation": "maintainability",
    "docs": "maintainability",
    "dead_code": "maintainability",
    "coupling": "maintainability",
    # 测试
    "testing": "test",
    "coverage": "test",
    "unit_test": "test",
    # 风格
    "formatting": "style",
    "format": "style",
    "naming": "style",
    "convention": "style",
    "conventions": "style",
    "lint": "style",
    "consistency": "style",
}

#: 严重度同义词 → 合法取值
SEVERITY_SYNONYMS: dict[str, str] = {
    "critical": "blocker",
    "fatal": "blocker",
    "severe": "blocker",
    "emergency": "blocker",
    "p0": "blocker",
    "major": "high",
    "error": "high",
    "p1": "high",
    "moderate": "medium",
    "warning": "medium",
    "warn": "medium",
    "p2": "medium",
    "normal": "medium",
    "minor": "low",
    "p3": "low",
    "info": "nit",
    "informational": "nit",
    "trivial": "nit",
    "suggestion": "nit",
    "p4": "nit",
}


def _normalize_value(
    value: str, valid: frozenset[str], synonyms: dict[str, str]
) -> tuple[str, str | None]:
    """返回 (归一化后的值, 原值或 None)。"""
    cleaned = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if cleaned in valid:
        return cleaned, None
    mapped = synonyms.get(cleaned)
    if mapped is not None:
        return mapped, value
    return value, None


def _note(raw: RawFinding, text: str) -> None:
    """把一次"修正"记录到 Finding 上，便于事后审计与提示词迭代。"""
    raw.raw.setdefault("_adjustments", []).append(text)


def normalize_enums(raw: RawFinding) -> list[str]:
    """把类别 / 严重度归一化到合法枚举，并记录被改写的原值（供审计与提示词迭代）。"""
    notes: list[str] = []

    category, category_original = _normalize_value(raw.category, VALID_CATEGORIES, CATEGORY_SYNONYMS)
    if category_original is not None:
        raw.category = category
        raw.raw["_category_original"] = category_original
        notes.append(f"category:{category_original}->{category}")

    severity, severity_original = _normalize_value(raw.severity, VALID_SEVERITIES, SEVERITY_SYNONYMS)
    if severity_original is not None:
        raw.severity = severity
        raw.raw["_severity_original"] = severity_original
        notes.append(f"severity:{severity_original}->{severity}")

    for note in notes:
        _note(raw, note)
    return notes


# ---------------------------------------------------------------------------- 静态佐证匹配


def available_rule_ids(static_findings: Iterable) -> dict[str, str]:
    """本次静态分析真实产出的规则 ID → 工具名。"""
    return {
        str(getattr(sf, "rule_id", "")): str(getattr(sf, "tool", ""))
        for sf in static_findings
        if getattr(sf, "rule_id", "")
    }


def match_evidence_rule_ids(raw: RawFinding, available: Mapping[str, str]) -> list[str]:
    """找出 evidence 里真实存在的静态规则 ID。

    被第 3 步（blocker 是否配得上）与第 5 步（交叉验证）共用 —— 第 3 步在流水线上
    早于第 5 步，不能依赖第 5 步写入的 `_evidence_rule_ids`。
    """
    matched: list[str] = []
    for item in raw.evidence:
        for rule_id, tool in available.items():
            if rule_id in item or f"{tool}:{rule_id}" in item:
                matched.append(rule_id)
                break
    return list(dict.fromkeys(matched))


# ---------------------------------------------------------------------------- 第 3 步


#: 类别 → 该类别下**自洽的最高严重度**。
#: 超出即压到上限，而不是丢弃：结论本身可能成立，只是严重度标签不自洽
#: （"风格问题"不可能是 blocker），标签是可以修正的，结论不该因此消失。
MAX_SEVERITY_BY_CATEGORY: dict[str, str] = {
    Category.STYLE.value: Severity.LOW.value,
    Category.MAINTAINABILITY.value: Severity.MEDIUM.value,
    Category.TEST.value: Severity.HIGH.value,
}

#: 自称 blocker 所需的最低置信度（有静态工具佐证时不受此限）
BLOCKER_MIN_CONFIDENCE = 0.80


def check_category_severity(raw: RawFinding, *, has_tool_support: bool = False) -> list[str]:
    """第 3 步：类别校验 —— category 与 severity 的组合合理性。"""
    notes: list[str] = []

    cap = MAX_SEVERITY_BY_CATEGORY.get(raw.category)
    if cap and SEVERITY_RANK.get(raw.severity, 9) < SEVERITY_RANK.get(cap, 9):
        # SEVERITY_RANK 数值越小越严重，所以"比上限更严重"是 <
        notes.append(f"severity:{raw.severity}->{cap}(category={raw.category} 的上限)")
        raw.severity = cap

    if (
        raw.severity == Severity.BLOCKER.value
        and not has_tool_support
        and raw.confidence < BLOCKER_MIN_CONFIDENCE
    ):
        notes.append(
            f"severity:blocker->high(confidence={raw.confidence:.2f}"
            f"<{BLOCKER_MIN_CONFIDENCE} 且无静态工具佐证)"
        )
        raw.severity = Severity.HIGH.value

    for note in notes:
        _note(raw, note)
    return notes


# ---------------------------------------------------------------------------- 第 4 步


#: 看起来像"代码片段"而不是"一句话描述 / 规则 ID"的 evidence
_CODE_LIKE_RE = re.compile(r"[;{}()=]|//|/\*|\bif\b|\breturn\b|\bfor\b|\bnew\b")

#: evidence 完全对不上时的降权系数
EVIDENCE_MISMATCH_PENALTY = 0.6


def _squash(text: str) -> str:
    """去掉全部空白后比较：diff 与 evidence 的缩进/换行必然不一致。"""
    return re.sub(r"\s+", "", text)


def check_existing_code(raw: RawFinding, file_diff) -> list[str]:
    """第 4 步：存量校验。

    锚定（第 2 步）已经保证"行号属于本次新增"。这一步再确认**证据也来自本次新增**：
    若 evidence 里全是代码片段，却没有任何一条能在本文件的新增行里找到，
    说明证据链指向的是存量代码 —— 典型形态是"这个 PR 顺带让我看见了老问题"。

    处理方式是降权而非丢弃：仍存在"新代码引用了老结构"的可能，只是模型的引用不准。
    """
    code_like = [e for e in raw.evidence if _CODE_LIKE_RE.search(e)]
    if not code_like:
        return []

    added = _squash(file_diff.added_text())
    if not added:
        return []

    hits = [e for e in code_like if _squash(e) and _squash(e) in added]
    if hits:
        return []

    raw.confidence = round(raw.confidence * EVIDENCE_MISMATCH_PENALTY, 4)
    raw.raw["_evidence_not_in_diff"] = True
    note = f"evidence_not_in_added_lines:{len(code_like)} 条代码证据均未命中新增行"
    _note(raw, note)
    return [note]


@lru_cache(maxsize=1)
def load_finding_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(load_finding_schema())


def _project(raw: RawFinding) -> dict[str, Any]:
    """从 RawFinding 投影出 Schema 声明的字段。"""
    properties = load_finding_schema().get("properties", {})
    source = {
        "path": raw.path,
        "line": raw.line,
        "end_line": raw.end_line,
        "category": raw.category,
        "severity": raw.severity,
        "confidence": raw.confidence,
        "title": raw.title,
        "body": raw.body,
        "suggestion": raw.suggestion,
        "evidence": list(raw.evidence),
        "needs_human_judgment": raw.needs_human_judgment,
    }
    return {k: v for k, v in source.items() if k in properties}


def validate_schema(raw: RawFinding) -> tuple[bool, str]:
    """第 1 步：Schema 校验。"""
    errors = sorted(_validator().iter_errors(_project(raw)), key=lambda e: list(e.path))
    if not errors:
        return True, ""
    first = errors[0]
    location = ".".join(str(p) for p in first.path) or "<root>"
    return False, f"{location}: {first.message}"


def normalize_path(path: str, candidates: Iterable[str]) -> str | None:
    """把模型给出的路径归一化到本次变更文件列表中的真实路径。

    只做无歧义的归一化（前缀、分隔符、唯一后缀 / 唯一 basename），
    不做模糊匹配 —— 匹配不上就丢弃，锚定校验必须可靠（文档 P2）。
    """
    candidates = list(candidates)
    if not candidates:
        return None
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    for prefix in ("a/", "b/"):
        if cleaned.startswith(prefix):
            stripped = cleaned[len(prefix) :]
            if stripped in candidates:
                return stripped
    if cleaned in candidates:
        return cleaned

    suffix_hits = [c for c in candidates if c.endswith("/" + cleaned) or c == cleaned]
    if len(suffix_hits) == 1:
        return suffix_hits[0]

    basename = cleaned.rsplit("/", 1)[-1]
    base_hits = [c for c in candidates if c.rsplit("/", 1)[-1] == basename]
    if len(base_hits) == 1:
        return base_hits[0]
    return None


@dataclass(slots=True)
class ValidationResult:
    kept: list[RawFinding]
    dropped: list[DropRecord]

    @property
    def anchor_validity(self) -> float:
        total = len(self.kept) + len(self.dropped)
        return len(self.kept) / total if total else 1.0


def validate_candidates(
    candidates: list[RawFinding],
    diff_set: DiffSet,
    *,
    static_findings: Iterable = (),
    extra_paths: Iterable[str] = (),
) -> ValidationResult:
    """执行 §9.1 中的第 1~4 步。

    第 5 步（交叉验证）需要独立调用 `cross_validate_findings`；第 8 步（门槛过滤）
    由 `apply_threshold` 在**交叉验证与去重之后**执行 —— 文档 §9.1 的顺序是
    1 schema → 2 锚定 → 3 类别 → 4 存量 → 5 交叉 → 6 去重 → 7 历史 → 8 门槛。

    早期实现为了"省排序成本"把第 8 步提前到这里，顺序不合规，也导致离线扫阈值
    （评估集校准 `ACRA_CONFIDENCE_THRESHOLD`）没法干净地重放。现在位置正确。
    """
    by_path = diff_set.by_path()
    candidates_paths = list(by_path) + list(extra_paths)
    available = available_rule_ids(static_findings)
    kept: list[RawFinding] = []
    dropped: list[DropRecord] = []

    for raw in candidates:
        # 第 0 步（本项目新增，见模块文档第二处偏离）：枚举归一化。
        # 先给模型的自创命名一次改过自新的机会，再谈"越界即丢弃"。
        notes = normalize_enums(raw)
        if notes:
            raw.raw["_normalized"] = notes

        ok, reason = validate_schema(raw)
        if not ok:
            dropped.append(DropRecord("1_schema", reason, raw))
            continue

        resolved = normalize_path(raw.path, candidates_paths)
        if resolved is None:
            dropped.append(DropRecord("2_anchor", f"path_not_in_diff:{raw.path}", raw))
            continue
        raw.path = resolved

        file_diff = by_path.get(resolved)
        if file_diff is None:
            dropped.append(DropRecord("2_anchor", "path_only_in_static_scope", raw))
            continue

        anchor = resolve_anchor(file_diff, raw.line)
        if not anchor.ok:
            dropped.append(DropRecord("2_anchor", f"{anchor.detail}:line={raw.line}", raw))
            continue
        raw.line = int(anchor.line or raw.line)

        # 第 3 步：类别校验（category × severity 的自洽性）
        check_category_severity(
            raw,
            has_tool_support=bool(match_evidence_rule_ids(raw, available)),
        )

        # 第 4 步：存量校验（证据必须来自本次新增）
        check_existing_code(raw, file_diff)

        if raw.end_line is not None and (
            raw.end_line < raw.line or raw.end_line not in file_diff.added_line_numbers
        ):
            # end_line 越出新增区间就退化回单行 —— 不因此丢弃整条结论
            _note(raw, f"end_line:{raw.end_line}->null(越出新增行区间)")
            raw.end_line = None

        if raw.severity not in SEVERITY_RANK:
            raw.severity = Severity.MEDIUM.value

        kept.append(raw)

    return ValidationResult(kept=kept, dropped=dropped)


# ---------------------------------------------------------------------------- 第 8 步


def apply_threshold(
    candidates: list[RawFinding],
    threshold: float | None,
) -> tuple[list[RawFinding], list[DropRecord]]:
    """第 8 步：门槛过滤（`confidence < threshold` → 丢弃）。

    刻意与 `validate_candidates` 分开，位置放在**交叉验证与去重之后**（文档 §9.1）。
    两个理由：

    1. 顺序合规 —— 一条候选应该先被交叉验证、去重，最后才谈门槛；
    2. 评估层要"过门槛之前的完整候选池"才能离线扫不同阈值
       （见 `acra eval sweep-threshold`），把门槛混在第 1~4 步里就没法干净地重放。
    """
    if threshold is None:
        return list(candidates), []
    kept: list[RawFinding] = []
    dropped: list[DropRecord] = []
    for raw in candidates:
        if raw.confidence < threshold:
            dropped.append(DropRecord("8_threshold", f"confidence<{threshold}", raw))
        else:
            kept.append(raw)
    return kept, dropped


# ---------------------------------------------------------------------------- 第 5 步


#: 裸规则代码的形态（ruff `F401`、tsc `TS2322` 之类）
_RULE_CODE_RE = re.compile(r"^[A-Z]{1,5}\d{2,5}$")


def claims_static_rule(item: str, tool_names: frozenset[str]) -> bool:
    """这条 evidence 是否在**声称引用某条静态规则**。

    判定看**形态**，不看它有没有匹配上 —— 这正是原实现缺的那一步。

    实测踩过：模型引用了一行真实存在、可定位的拼接 SQL 作为证据
    （`sql = "select * from orders where id = '" + str(order_id) + "'"`），
    却因为本次静态结果里没有对应规则被 `confidence *= 0.5`，
    一条 `security / blocker / 0.9` 的结论掉到 0.45、被第 8 步门槛丢弃 ——
    **一条真实的 SQL 注入因此没有到达作者**，而报告上只显示"0 条结论"。

    取舍方向：**宁可少惩罚，也不要静默丢掉一条真结论**。
    漏掉一次惩罚的代价是"多留了一条可能不可靠的结论"（作者看得见、能判断），
    而错罚一次的代价是"一条真问题永久消失"（没人看得见）。
    """
    text = str(item).strip().strip("`").strip()
    if not text:
        return False
    if _CODE_LIKE_RE.search(text):
        # 已经是明显的代码片段，不可能是规则引用
        return False
    lowered = text.lower()
    if any(lowered.startswith(f"{tool}:") for tool in tool_names):
        return True
    return bool(_RULE_CODE_RE.match(text))


def cross_validate_findings(
    findings: list[RawFinding],
    static_findings: Iterable,
    *,
    penalize_unmatched: bool = True,
) -> list[RawFinding]:
    """第 5 步：交叉验证。

    > 若 evidence 中引用了静态规则 ID，则校验该规则确实在本次输出中；否则视为编造证据
    > → confidence *= 0.5

    惩罚的触发条件是"**声称**引用了静态规则"（见 `claims_static_rule`），
    而不是"evidence 非空"。这条区别是实测补上的：把引用真实代码也算成编造证据，
    会让正确的结论被降权到门槛以下、静默消失。

    只有真的开启了静态分析（`static_findings` 非空）时才会施加惩罚：
    阶段/仓库未启用静态检查时"没有规则可比对"，那是配置事实，不是模型编造证据。
    `evidence_rule_ids` 也是 §9.2 评分函数中"+0.15 有静态工具佐证"的依据。
    """
    available = available_rule_ids(static_findings)
    tool_names = frozenset(str(t).lower() for t in available.values())

    for raw in findings:
        matched = match_evidence_rule_ids(raw, available)
        raw.raw["_evidence_rule_ids"] = matched
        # 只惩罚"声称引用了静态规则、但本次没有这条规则"的结论。
        # 引用真实代码作为证据不在此列 —— 原来那句 `raw.evidence and not matched`
        # 会把"引用代码"一起打成"编造证据"，实测因此丢掉了真结论。
        claimed = [e for e in raw.evidence if claims_static_rule(e, tool_names)]
        if penalize_unmatched and claimed and not matched and available:
            raw.confidence = round(raw.confidence * 0.5, 4)
            raw.raw["_evidence_unverified"] = True
            _note(raw, f"confidence*0.5(声称引用静态规则 {claimed} 但本次无此结果)")
    return findings
    return findings
