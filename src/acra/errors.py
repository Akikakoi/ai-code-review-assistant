"""领域异常。

降级策略见开发文档 §4.2：不同失败类型对应不同的降级动作，因此需要区分异常类型，
而不是笼统地抛 RuntimeError。
"""

from __future__ import annotations


class AcraError(Exception):
    """所有领域异常的基类。"""


class ConfigError(AcraError):
    """配置缺失或非法（如未配置 LLM_API_KEY）。"""


class GitError(AcraError):
    """git 命令失败。"""


class CloneError(GitError):
    """克隆 / fetch 失败 —— 不贴任何评论，仅把 Check Run 置 neutral 并记日志。"""


class LLMError(AcraError):
    """LLM 调用的统一异常。"""


class LLMUnavailable(LLMError):
    """重试耗尽后仍不可用 —— 触发 static-only 降级。"""


class LLMResponseError(LLMError):
    """响应无法解析为约定结构（JSON 解析失败 / 结构不符）。"""


class BudgetExceeded(AcraError):
    """预算熔断。"""


class SandboxUnavailable(AcraError):
    """沙箱不可用 —— 禁用 Agent 工具，退化为单轮无工具分析。"""


class PromptNotFound(AcraError):
    """提示词模板缺失。"""
