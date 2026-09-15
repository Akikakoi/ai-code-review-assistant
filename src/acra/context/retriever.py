"""L3 仓库级检索（阶段二/三实现）。

开发文档 §7.6 的设计约束，此处先固化接口与判定规则，避免阶段三改动上层：

- 仅当仓库规模 < 5000 文件时启用向量检索；更大规模改用符号名精确匹配 + 路径相似度，
  避免检索本身成为瓶颈；
- 索引内容 = 每个方法的自然语言摘要（轻量模型预生成）+ 方法签名；
- 以"变更方法摘要"为 query，取 top-3 且相似度 > 0.75，过滤掉自己；
- 注入时明确标注为"约定参照"，不是待审代码。

阶段一默认不启用（§7.1：L3 按需触发、默认关闭），因此这里返回空结果而非抛异常，
让上层逻辑可以直接调用。
"""

from __future__ import annotations

from acra.models import CallSite, CodeSlice, FileDiff, PriorComment

VECTOR_SEARCH_MAX_FILES = 5000
SIMILARITY_THRESHOLD = 0.75
TOP_K = 3


class Retriever:
    """L3 线索检索器骨架。"""

    def __init__(self, handle=None, settings=None) -> None:
        self.handle = handle
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return False

    async def callers_of(self, symbol: str) -> list[CallSite]:
        return []

    async def similar_implementations(self, file_diff: FileDiff, summary: str) -> list[CodeSlice]:
        return []

    async def prior_comments(self, path: str) -> list[PriorComment]:
        return []
