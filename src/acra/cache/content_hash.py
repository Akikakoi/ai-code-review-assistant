"""内容哈希缓存（L1）。

对应开发文档 §10.2：

```
L1: sha256(model_id + prompt_template_version + chunk_content_hash)
L2: 向量相似度 > 0.97 时才命中（阈值高，避免错配）
```

**必须包含 `prompt_template_version`** —— 提示词改了，旧结果立即失效，
否则会长期返回一份用旧标准生成的结论（文档 §10.2 末段）。

阶段一提供两个后端：进程内 LRU（默认，零依赖）与 Redis（`REDIS_URL` 配置后启用）。
缓存 TTL 默认 7 天；缓存内容包含代码片段，因此支持按仓库关闭（文档 §12.4）。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from acra import PROMPT_TEMPLATE_VERSION

DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def content_hash(*parts: str) -> str:
    payload = "\x00".join(parts)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def cache_key(*, model_id: str, chunk_content_hash: str, version: str | None = None) -> str:
    """L1 缓存键。"""
    return "acra:l1:" + content_hash(
        model_id, version or PROMPT_TEMPLATE_VERSION, chunk_content_hash
    )


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int = DEFAULT_TTL_SECONDS) -> None: ...


class MemoryCache:
    """进程内 LRU 缓存。测试与单机 CLI 用。"""

    def __init__(self, max_entries: int = 512, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self.max_entries = max_entries
        self.ttl = ttl
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, value = entry
        if expires_at and expires_at < time.time():
            self._store.pop(key, None)
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return value

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        ttl = self.ttl if ttl is None else ttl
        expires_at = time.time() + ttl if ttl > 0 else 0.0
        self._store[key] = (expires_at, value)
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class NullCache:
    """显式关闭缓存时使用（例如按仓库关闭，文档 §12.4）。"""

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        return None
