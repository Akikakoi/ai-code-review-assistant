"""降级状态与 Check Run 结论的单测。

文档 §4.2 的降级表 + §4.9 的 Check Run 映射。这里最容易被写错的一点是**把信息性说明
当成降级**：那样每次运行都会被标记成 degraded，"失败率/降级率"这类指标就彻底没用了。
"""

from __future__ import annotations

from acra.orchestrator.degrade import (
    DegradeState,
    check_run_conclusion,
    on_budget_exceeded,
    on_clone_failure,
    on_llm_unavailable,
    on_partial_chunk_failure,
    on_sandbox_unavailable,
)


def test_info_note_does_not_mark_degraded() -> None:
    state = DegradeState()
    state.add("静态检查未执行：static_analysis_disabled", degraded=False)
    state.add("跳过低价值文件 3 个", degraded=False)
    assert state.degraded is False
    assert state.notes == ["静态检查未执行：static_analysis_disabled", "跳过低价值文件 3 个"]
    assert state.ai_available is True


def test_degrade_note_marks_degraded() -> None:
    state = DegradeState()
    state.add("全局 token 预算已耗尽")
    assert state.degraded is True


def test_notes_are_deduplicated_but_flag_is_sticky() -> None:
    state = DegradeState()
    state.add("同一条", degraded=False)
    state.add("同一条")
    assert state.notes == ["同一条"]
    assert state.degraded is True


def test_llm_unavailable_switches_to_static_only() -> None:
    state = on_llm_unavailable(DegradeState())
    assert state.ai_available is False
    assert state.static_only is True
    assert state.degraded is True
    assert state.mode == "static_only"
    assert "AI 分析不可用" in state.summary_note()


def test_partial_chunk_failure_is_degraded() -> None:
    state = on_partial_chunk_failure(DegradeState(), 2, 5)
    assert state.degraded is True
    assert "2/5" in state.notes[0]


def test_sandbox_unavailable_disables_tools_only() -> None:
    state = on_sandbox_unavailable(DegradeState())
    assert state.tools_enabled is False
    assert state.ai_available is True  # 仍做单轮无工具分析
    assert state.degraded is True


def test_clone_failure_is_fatal_and_blocks_publishing() -> None:
    state = on_clone_failure(DegradeState())
    assert state.fatal is True
    assert state.publish_allowed is False
    assert state.degraded is True


def test_budget_exceeded_collects_notes() -> None:
    state = on_budget_exceeded(DegradeState(), ["已降级模型档位", "跳过 L3 上下文"])
    assert state.degraded is True
    assert len(state.notes) == 2


def test_mode_defaults_to_full() -> None:
    assert DegradeState().mode == "full"


# ---------------------------------------------------------------------------- Check Run


def test_check_run_success_when_no_high_severity() -> None:
    assert check_run_conclusion(DegradeState(), has_high_severity=False) == "success"


def test_check_run_neutral_when_high_severity_present() -> None:
    """不用 failure —— 不阻塞合并是设计决策（ADR 0003）。"""
    assert check_run_conclusion(DegradeState(), has_high_severity=True) == "neutral"


def test_check_run_neutral_when_degraded_or_failed() -> None:
    degraded = on_llm_unavailable(DegradeState())
    assert check_run_conclusion(degraded, has_high_severity=False) == "neutral"

    fatal = on_clone_failure(DegradeState())
    assert check_run_conclusion(fatal, has_high_severity=False) == "neutral"


def test_check_run_never_returns_failure() -> None:
    for state in (
        DegradeState(),
        on_llm_unavailable(DegradeState()),
        on_clone_failure(DegradeState()),
        on_sandbox_unavailable(DegradeState()),
    ):
        for has_high in (True, False):
            assert check_run_conclusion(state, has_high_severity=has_high) in {
                "success",
                "neutral",
            }
