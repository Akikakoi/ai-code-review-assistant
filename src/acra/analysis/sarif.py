"""SARIF 子集解析与 diff-aware 过滤。

对应开发文档 §4.5：所有静态分析工具的输出统一归一化为内部 `StaticFinding`
（SARIF 子集），关键字段 `rule_id` / `severity` / `path` / `line` / `message` / `tool`。

diff-aware 过滤（文档 §4.5「重要」）：只保留行号落在 `added_line_numbers` 内的结果。
存量代码的历史告警不进入 LLM 上下文 —— 既省 token，也避免"这个 PR 一个没改的问题被
反复提"。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from acra.models import StaticFinding

_SARIF_LEVEL_MAP = {
    "error": "high",
    "warning": "medium",
    "note": "low",
    "none": "low",
}


def _first_location(result: dict[str, Any]) -> tuple[str, int]:
    path = ""
    line = 0
    locations = result.get("locations") or []
    if locations:
        loc = locations[0]
        phys = loc.get("physicalLocation") or {}
        artifact = phys.get("artifactLocation") or {}
        path = artifact.get("uri") or artifact.get("uriBaseId") or ""
        region = phys.get("region") or {}
        start = region.get("startLine")
        line = int(start) if isinstance(start, int) else 0
    if not path:
        for key in ("analysisTarget", "partialFingerprints"):
            _ = result.get(key)
    return path, line


def _clean_uri(uri: str) -> str:
    uri = uri.replace("\\", "/")
    for prefix in ("file:///", "file://"):
        if uri.startswith(prefix):
            uri = uri[len(prefix) :]
    if uri.startswith("./"):
        uri = uri[2:]
    return uri.lstrip("/") if uri.startswith("/") else uri


def parse_sarif(doc: dict[str, Any], *, default_tool: str = "unknown") -> list[StaticFinding]:
    """解析 SARIF 2.1.0 文档为 StaticFinding 列表。"""
    findings: list[StaticFinding] = []
    for run in doc.get("runs") or []:
        tool = ((run.get("tool") or {}).get("driver") or {}).get("name") or default_tool
        rules: dict[str, dict[str, Any]] = {}
        for rule in ((run.get("tool") or {}).get("driver") or {}).get("rules") or []:
            rid = rule.get("id")
            if rid:
                rules[rid] = rule

        for result in run.get("results") or []:
            rule_id = str(result.get("ruleId") or (result.get("rule") or {}).get("id") or "")
            path, line = _first_location(result)
            level = str(result.get("level") or "")
            if not level:
                rule = rules.get(rule_id) or {}
                level = str((rule.get("defaultConfiguration") or {}).get("level") or "warning")
            message = (result.get("message") or {}).get("text") or ""
            if not path:
                continue
            findings.append(
                StaticFinding(
                    tool=tool,
                    rule_id=rule_id or "unknown",
                    severity=_SARIF_LEVEL_MAP.get(level, "medium"),
                    path=_clean_uri(path),
                    line=line,
                    message=" ".join(str(message).split())[:400],
                    level=level or "warning",
                )
            )
    return findings


def filter_diff_aware(
    findings: Iterable[StaticFinding],
    whitelist: dict[str, set[int]],
    *,
    ignored_rules: Iterable[str] = (),
) -> list[StaticFinding]:
    """只保留落在变更行上的静态结果。"""
    ignored = set(ignored_rules)
    out: list[StaticFinding] = []
    for f in findings:
        if f.rule_id in ignored:
            continue
        lines = whitelist.get(f.path)
        if not lines or f.line not in lines:
            continue
        out.append(f)
    return out


def to_sarif(findings: Iterable, *, version: str = "0.1.0") -> dict[str, Any]:
    """把内部 Finding 渲染为 SARIF 2.1.0 输出（CLI --format sarif）。"""
    from acra.models import SEVERITY_RANK

    level_map = {"blocker": "error", "high": "error", "medium": "warning", "low": "note", "nit": "note"}
    results: list[dict[str, Any]] = []
    rules: dict[str, dict[str, Any]] = {}
    for f in findings:
        rid = f"acra/{f.category}"
        rules.setdefault(rid, {"id": rid, "name": f.category, "shortDescription": {"text": f.category}})
        results.append(
            {
                "ruleId": rid,
                "level": level_map.get(f.severity, "warning"),
                "message": {"text": f"**{f.title}**\n\n{f.body}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.path},
                            "region": {
                                "startLine": max(1, f.line),
                                **({"endLine": f.end_line} if f.end_line else {}),
                            },
                        }
                    }
                ],
                "properties": {
                    "severity": f.severity,
                    "confidence": f.confidence,
                    "score": f.score,
                    "severityRank": SEVERITY_RANK.get(f.severity, 9),
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "acra", "version": version, "rules": list(rules.values())}},
                "results": results,
            }
        ],
    }
