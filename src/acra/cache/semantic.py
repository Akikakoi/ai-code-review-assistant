"""语义缓存（L2，阶段四实现）。

开发文档 §10.2：

```
L2: 向量相似度 > 0.97 时才命中（阈值高，避免错配）
```

阈值刻意定得很高：语义缓存命中错配会产生"用另一段代码的结论回答这段代码"的严重
错误，收益（省 10%~20%）远小于风险，因此宁可少命中。阶段一不实现。
"""

from __future__ import annotations

SIMILARITY_THRESHOLD = 0.97


class SemanticCache:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("语义缓存在阶段四实现（文档 §16 阶段四）")
