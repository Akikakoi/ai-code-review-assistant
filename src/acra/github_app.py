"""GitHub App 认证：App JWT → installation token。

发布（文档 §4.9）走 REST API、私有仓库克隆（文档 §4.4）走 `http.extraHeader`，
两者需要的都是 **installation token**，而不是 App ID 本身。

单独成模块的理由：installation token 有效期 1 小时，而一次大 PR 的
"克隆 → 扫描 → 验证 → 发布"完全可能跨过这个边界。把"签 JWT / 换 token / 到期前刷新"
收在一处，调用方只拿到一个字符串，不必各自记这件事。

约束（都是安全取舍）：
- 私钥只从文件读，**不进日志、不进异常信息**；
- App JWT 有效期受 GitHub 硬限制（上限 10 分钟），这里取 9 分钟并留 60s 时钟偏移；
- 拿到 token 后按 installation 缓存，剩余不足 `REFRESH_MARGIN_SECONDS` 时提前换新，
  避免"取的时候还有效、用的时候过期了"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from acra.errors import AcraError

logger = logging.getLogger("acra.github_app")

DEFAULT_API_BASE = "https://api.github.com"

#: GitHub 对 App JWT 的有效期上限是 10 分钟
JWT_TTL_SECONDS = 540
JWT_CLOCK_SKEW_SECONDS = 60
#: installation token 名义有效期 1 小时；剩余不足该值就提前换新
REFRESH_MARGIN_SECONDS = 300


class GithubAppError(AcraError):
    pass


@dataclass(slots=True)
class _CachedToken:
    token: str
    expires_at: float


class GithubAppAuth:
    """App 身份：签 JWT → 换 installation token → 缓存并在过期前刷新。"""

    def __init__(
        self,
        app_id: str | int,
        private_key_path: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        installation_id: int | str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not app_id:
            raise GithubAppError("缺少 GITHUB_APP_ID")
        if not private_key_path:
            raise GithubAppError("缺少 GITHUB_APP_PRIVATE_KEY_PATH")

        self.app_id = str(app_id)
        self.private_key_path = Path(private_key_path)
        self.default_installation_id = (
            int(installation_id) if str(installation_id or "").strip() else None
        )
        self._cache: dict[int, _CachedToken] = {}
        self._client = httpx.AsyncClient(
            base_url=api_base.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "acra/0.1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GithubAppAuth:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 私钥 / JWT

    def _read_private_key(self) -> str:
        if not self.private_key_path.exists():
            # 只报路径，不报内容
            raise GithubAppError(f"App 私钥文件不存在：{self.private_key_path}")
        return self.private_key_path.read_text(encoding="utf-8")

    def app_jwt(self, *, now: float | None = None) -> str:
        """签一个 App JWT（RS256）。`now` 可注入，便于测试时钟边界。"""
        issued = now if now is not None else time.time()
        payload = {
            # 回拨 60s 抵消客户端与服务端的时钟偏移，否则 GitHub 会判 "iat in the future"
            "iat": int(issued) - JWT_CLOCK_SKEW_SECONDS,
            "exp": int(issued) + JWT_TTL_SECONDS,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self._read_private_key(), algorithm="RS256")

    # ------------------------------------------------------------------ token

    async def installation_token(
        self, installation_id: int | str | None = None, *, now: float | None = None
    ) -> str:
        iid = self._resolve_installation_id(installation_id)
        current = now if now is not None else time.time()

        cached = self._cache.get(iid)
        if cached and cached.expires_at - REFRESH_MARGIN_SECONDS > current:
            return cached.token

        token, expires_at = await self._fetch_installation_token(iid)
        # 供应商没给 expires_at 时保守按 1 小时算
        self._cache[iid] = _CachedToken(
            token=token, expires_at=expires_at or (current + 3600 - REFRESH_MARGIN_SECONDS)
        )
        logger.info("已换取 installation token（installation=%s）", iid)
        return token

    def _resolve_installation_id(self, override: int | str | None) -> int:
        raw = override if override not in (None, "") else self.default_installation_id
        if raw in (None, ""):
            raise GithubAppError(
                "缺少 installation id：webhook 事件里是 installation.id，"
                "CLI 本地发布需要 GITHUB_APP_INSTALLATION_ID"
            )
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise GithubAppError(f"installation id 不是整数：{raw!r}") from exc

    async def _fetch_installation_token(self, installation_id: int) -> tuple[str, float | None]:
        response = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.app_jwt()}"},
        )
        if response.status_code >= 400:
            raise GithubAppError(
                f"换取 installation token 失败（HTTP {response.status_code}）："
                f"{response.text[:300]}"
            )
        data = response.json()
        token = data.get("token")
        if not token:
            raise GithubAppError("GitHub 未返回 installation token")
        return token, _parse_expiry(data.get("expires_at"))


def _parse_expiry(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
