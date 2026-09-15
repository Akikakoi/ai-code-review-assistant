"""链路追踪。

开发文档 §13.3：每次 `ReviewRun` 生成一个 trace，span 覆盖克隆、diff 解析、静态分析、
每块 Scan、每条 Verify、每次工具调用、发布；`trace_id` 与 `job_id` 关联，
便于从一条差评反查全过程。

设计：优先使用 OpenTelemetry（配置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后启用）；
未配置时退化为一个极轻量的进程内 span 记录器，只做计时与结构化日志，
不引入额外依赖、不影响主链路。
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

logger = logging.getLogger("acra.trace")


@dataclass(slots=True)
class Span:
    name: str
    started: float
    attributes: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


class Tracer:
    """最小可用追踪器。配置了 OTLP 端点时把 span 记为结构化日志（接入真实 exporter 时替换）。"""

    def __init__(self, enabled: bool = False, job_id: str | None = None) -> None:
        self.enabled = enabled
        self.job_id = job_id
        self.spans: list[Span] = []

    @contextlib.contextmanager
    def span(self, name: str, **attributes: str) -> Iterator[Span]:
        started = time.monotonic()
        record = Span(name=name, started=started, attributes=dict(attributes))
        try:
            yield record
        finally:
            record.duration_ms = int((time.monotonic() - record.started) * 1000)
            self.spans.append(record)
            if self.enabled:
                logger.info(
                    "span job=%s name=%s duration_ms=%d attrs=%s",
                    self.job_id,
                    record.name,
                    record.duration_ms,
                    record.attributes,
                )

    def summary(self) -> list[dict[str, object]]:
        return [
            {"name": s.name, "duration_ms": s.duration_ms, **s.attributes} for s in self.spans
        ]


def make_tracer(settings, job_id: str | None = None) -> Tracer:
    return Tracer(enabled=bool(settings.otel_exporter_otlp_endpoint), job_id=job_id)
