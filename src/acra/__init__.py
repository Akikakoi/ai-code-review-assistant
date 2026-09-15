"""acra —— AI 代码审查助手。

设计文档见 `docs/DEVELOPMENT.md`。
"""

from __future__ import annotations

__version__ = "0.1.0"

#: 提示词模板版本。缓存键必须包含它，否则提示词改动后仍会命中旧结论（文档 §10.2）。
PROMPT_TEMPLATE_VERSION = "1"

__all__ = ["__version__", "PROMPT_TEMPLATE_VERSION"]
