"""Redis 缓存 / 幂等键 / 分布式锁（`REDIS_URL` 配置后启用）。

与 `content_hash.MemoryCache` 实现同一个 `Cache` 协议，因此上层不需要分支。
`redis` 包是可选依赖（`pip install acra[redis]`），这里延迟导入，
未安装时抛出可读的 ConfigError 而不是 ImportError。
"""

from __future__ import annotations

from acra.cache.content_hash import DEFAULT_TTL_SECONDS
from acra.errors import ConfigError


class RedisCache:
    def __init__(self, redis_url: str, *, prefix: str = "acra:l1:") -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:  # pragma: no cover
            raise ConfigError(
                "已配置 REDIS_URL 但未安装 redis 包，请执行：pip install 'acra[redis]'"
            ) from exc
        self._client = aioredis.from_url(redis_url, decode_responses=True)
        self.prefix = prefix

    async def get(self, key: str) -> str | None:
        return await self._client.get(self.prefix + key)

    async def set(self, key: str, value: str, ttl: int = DEFAULT_TTL_SECONDS) -> None:
        await self._client.set(self.prefix + key, value, ex=ttl or None)

    async def incr_with_ttl(self, key: str, ttl: int) -> int:
        """计数器 + 过期，用于每日预算与限流。"""
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            result = await pipe.execute()
        return int(result[0])

    async def acquire_lock(self, key: str, ttl: int = 900) -> bool:
        """单 PR 同时只允许一个审查任务（文档 §4.2 并发与限流）。"""
        return bool(await self._client.set(f"acra:lock:{key}", "1", nx=True, ex=ttl))

    async def release_lock(self, key: str) -> None:
        await self._client.delete(f"acra:lock:{key}")

    async def aclose(self) -> None:
        await self._client.aclose()
