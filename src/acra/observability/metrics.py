"""指标埋点。

指标名逐条对应开发文档 §13.3，便于 Grafana 看板按文档直接搭。

`prometheus_client` 未安装时全部退化为 no-op，不影响主链路 —— 观测是横切关注点，
不应该成为审查失败的原因。
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - 依赖是否存在取决于安装方式
    from prometheus_client import Counter, Gauge, Histogram

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False


class _Noop:
    def labels(self, *args: Any, **kwargs: Any) -> _Noop:
        return self

    def inc(self, *args: Any, **kwargs: Any) -> None:
        return None

    def observe(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> None:
        return None


def _counter(name: str, doc: str, labels: list[str]) -> Any:
    return Counter(name, doc, labels) if _AVAILABLE else _Noop()


def _histogram(name: str, doc: str, labels: list[str]) -> Any:
    return Histogram(name, doc, labels) if _AVAILABLE else _Noop()


def _gauge(name: str, doc: str, labels: list[str]) -> Any:
    return Gauge(name, doc, labels) if _AVAILABLE else _Noop()


review_total = _counter("acra_review_total", "审查次数", ["status", "mode"])
review_duration = _histogram("acra_review_duration_seconds", "审查耗时", ["mode"])
findings_total = _counter("acra_findings_total", "产出结论分布", ["category", "severity"])
finding_score = _histogram("acra_finding_score", "评分分布", ["severity"])
verify_verdict_total = _counter("acra_verify_verdict_total", "验证阶段结论分布", ["verdict"])
llm_tokens = _counter("acra_llm_tokens_total", "token 消耗", ["phase", "direction"])
llm_cost = _counter("acra_llm_cost_micros_total", "成本（微元，1e-6 元）", ["phase"])
cache_hit = _counter("acra_cache_hit_total", "缓存命中", ["layer"])
sandbox_exec = _counter("acra_sandbox_exec_total", "沙箱执行", ["result"])
tool_call = _counter("acra_tool_call_total", "工具调用", ["tool", "success"])
degraded_total = _counter("acra_degraded_total", "降级次数", ["reason"])
feedback_total = _counter("acra_feedback_total", "人工反馈", ["verdict"])
anchor_failures = _counter("acra_anchor_failure_total", "锚定失败次数", ["step"])
queue_depth = _gauge("acra_queue_depth", "队列积压", [])


def observe_outcome(outcome) -> None:
    """把一次 ReviewOutcome 埋进指标。"""
    status = "failed" if outcome.error else ("degraded" if outcome.degrade.notes else "succeeded")
    review_total.labels(status=status, mode=outcome.mode).inc()
    review_duration.labels(mode=outcome.mode).observe(outcome.duration_ms / 1000.0)

    for finding in outcome.reported:
        findings_total.labels(category=finding.category, severity=finding.severity).inc()
        finding_score.labels(severity=finding.severity).observe(finding.score)

    for phase, total in outcome.usage.per_phase.items():
        llm_tokens.labels(phase=phase, direction="total").inc(total)
    llm_tokens.labels(phase="all", direction="input").inc(outcome.usage.prompt_tokens)
    llm_tokens.labels(phase="all", direction="output").inc(outcome.usage.completion_tokens)
    if outcome.usage.cost_micros:
        llm_cost.labels(phase="all").inc(outcome.usage.cost_micros)

    for note in outcome.degrade.notes:
        degraded_total.labels(reason=note.split("：", 1)[0][:60]).inc()

    for drop in outcome.dropped:
        if drop.step.startswith("2_"):
            anchor_failures.labels(step=drop.step).inc()


def metrics_asgi_app():
    """挂到 FastAPI 上的 /metrics 子应用。"""
    if not _AVAILABLE:  # pragma: no cover
        return None
    from prometheus_client import make_asgi_app

    return make_asgi_app()


def available() -> bool:
    return _AVAILABLE
