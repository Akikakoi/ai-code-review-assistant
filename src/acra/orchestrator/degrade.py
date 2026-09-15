"""降级策略。

对应开发文档 §4.2「失败与降级」：

```
LLM 超时 / 限流  → 指数退避重试 2 次
重试仍失败        → 降级为 static-only 模式，summary 中标注"AI 分析不可用，仅展示静态检查结果"
克隆失败          → 标记失败，不贴任何评论，仅更新 Check Run 为 neutral 并记录日志
沙箱不可用        → 禁用 Agent 工具，退化为单轮无工具分析
```

核心原则（文档 P7）：**失败要可降级，不要静默消失**。因此每一次降级都会在
`DegradeState.notes` 里留下可读原因，并最终出现在 summary 的折叠区里 ——
作者看到"这次没意见"和"这次分析被降级了"是两件完全不同的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DegradeState:
    ai_available: bool = True
    static_only: bool = False
    tools_enabled: bool = True
    publish_allowed: bool = True
    fatal: bool = False
    #: 是否发生了**真正的降级**（区别于信息性说明）
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    def add(self, note: str, *, degraded: bool = True) -> None:
        """追加一条说明。

        `degraded=False` 用于信息性说明（例如"阶段一未启用静态检查"、"跳过文档类文件"）：
        它们需要让作者看到（写进 summary 折叠区），但**不能**把这次运行标记成 degraded，
        否则 `review_run.status` 和失败率类指标会被这类预期内的事实长期污染。
        """
        if note not in self.notes:
            self.notes.append(note)
        if degraded:
            self.degraded = True

    @property
    def mode(self) -> str:
        return "static_only" if self.static_only else "full"

    def summary_note(self) -> str:
        if self.static_only:
            return "AI 分析不可用，仅展示静态检查结果"
        if self.notes:
            return "；".join(self.notes)
        return ""


def on_llm_unavailable(state: DegradeState) -> DegradeState:
    state.ai_available = False
    state.static_only = True
    state.add("AI 分析不可用（模型调用失败或输出无法解析），仅展示静态检查结果")
    return state


def on_partial_chunk_failure(state: DegradeState, failed: int, total: int) -> DegradeState:
    state.add(f"{failed}/{total} 个代码块分析失败，结果可能不完整")
    return state


def on_sandbox_unavailable(state: DegradeState) -> DegradeState:
    state.tools_enabled = False
    state.add("沙箱不可用，已禁用 Agent 工具，退化为单轮无工具分析")
    return state


def on_clone_failure(state: DegradeState) -> DegradeState:
    state.fatal = True
    state.publish_allowed = False
    state.add("代码克隆失败，未产出任何评论，Check Run 置为 neutral")
    return state


def on_budget_exceeded(state: DegradeState, notes: list[str]) -> DegradeState:
    for note in notes:
        state.add(note)
    return state


def check_run_conclusion(state: DegradeState, *, has_high_severity: bool) -> str:
    """Check Run 结论（文档 §4.9）。

    有高优先级问题用 `neutral` 而非 `failure` —— 不阻塞合并是设计决策（ADR 0003）。
    """
    if state.fatal or state.static_only or not state.ai_available:
        return "neutral"
    return "neutral" if has_high_severity else "success"
