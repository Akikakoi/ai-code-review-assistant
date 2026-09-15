"""按任务解析 GitHub 凭据并构造 publisher。

`GithubPublisher` 只要一个 token 字符串，但 token 有两个来源：
App 换取的 installation token（生产），或配置里的静态 token（本地/CI 做一次性验证）。
把"取凭据 → 建 publisher → 用完关掉"收在这一层，入口侧只剩两行。

未配置任何凭据时产出 `None` —— 这正是 pipeline 里"不发布"的语义（本地 `--dry-run` 就是它）。
但这条路径**必须留下痕迹**：否则"凭据配错了所以没发评论"会表现成"这次没什么可发的"，
而这两件事在总结里完全不同（前者是故障，后者是正常结果）。
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from acra.github_app import GithubAppAuth, GithubAppError
from acra.publish.github import GithubPublisher

logger = logging.getLogger("acra.publish.factory")

#: 供调用方判断"这次该不该有评论发出去"，用于区分故障与正常
SOURCE_APP = "app"
SOURCE_STATIC = "static_token"
SOURCE_NONE = "none"


@dataclass(slots=True)
class GithubAccess:
    token: str | None = None
    source: str = SOURCE_NONE
    installation_id: int | None = None
    #: 让访问目标跟着凭据一起走：GHES 与测试端点都靠它，调用方不必再传一次
    api_base: str = "https://api.github.com"
    #: 取凭据失败的原因（非空即代表"本该能发但没发成"）
    error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.token)


# 复用一个 App 客户端：它内部按 installation 缓存 token，重建会让缓存失效、
# 每来一个任务都多换一次 token（既慢又更容易撞上限流）。
_APP_AUTH_CACHE: dict[tuple[str, str, str], GithubAppAuth] = {}


def _app_auth(settings) -> GithubAppAuth:
    api_base = settings.github_api_base
    key = (str(settings.github_app_id), str(settings.github_app_private_key_path), str(api_base))
    auth = _APP_AUTH_CACHE.get(key)
    if auth is None:
        auth = GithubAppAuth(
            app_id=settings.github_app_id,
            private_key_path=settings.github_app_private_key_path,
            api_base=api_base,
            installation_id=settings.github_app_installation_id or None,
        )
        _APP_AUTH_CACHE[key] = auth
    return auth


async def resolve_access(settings, job) -> GithubAccess:
    """解析本次任务可用的 GitHub 凭据。任何失败都返回带 `error` 的无效凭据，不抛异常。"""
    api_base = settings.github_api_base
    if settings.acra_github_token:
        return GithubAccess(
            token=settings.acra_github_token, source=SOURCE_STATIC, api_base=api_base
        )

    if not (settings.github_app_id and settings.github_app_private_key_path):
        return GithubAccess(api_base=api_base)

    installation_id = job.installation_id or settings.github_app_installation_id or None
    try:
        token = await _app_auth(settings).installation_token(installation_id)
    except GithubAppError as exc:
        logger.warning("GitHub App 凭据不可用：%s", exc)
        return GithubAccess(source=SOURCE_APP, api_base=api_base, error=str(exc))

    try:
        iid = int(installation_id) if installation_id not in (None, "") else None
    except (TypeError, ValueError):
        iid = None
    return GithubAccess(token=token, source=SOURCE_APP, installation_id=iid, api_base=api_base)


@contextlib.asynccontextmanager
async def open_publisher(access: GithubAccess) -> AsyncIterator[GithubPublisher | None]:
    """按凭据产出 publisher；无凭据时产出 None（= 不发布）。"""
    if not access.token:
        yield None
        return
    publisher = GithubPublisher(access.token, api_base=access.api_base)
    try:
        yield publisher
    finally:
        await publisher.aclose()


def reset_app_auth_cache() -> None:
    """测试用：清掉复用的 App 客户端。"""
    _APP_AUTH_CACHE.clear()


def describe_credentials(settings) -> tuple[str, str]:
    """不发起任何请求地报告发布凭据状态，供 `acra doctor` 使用。

    为什么值得单列一行：凭据有没有配，直接决定"到底会不会有评论发出去"。
    这件事此前只能靠读运行日志反推 —— 而 `acra doctor` 正是用来回答这类问题的。
    """
    if settings.acra_github_token:
        return SOURCE_STATIC, "已配置静态 token（本地/CI 验证用）"

    if not (settings.github_app_id and settings.github_app_private_key_path):
        return SOURCE_NONE, "未配置（审查结果只在本地产出，不会发布评论）"

    from pathlib import Path

    key_path = Path(settings.github_app_private_key_path)
    if not key_path.exists():
        return SOURCE_APP, f"私钥文件不存在：{key_path}"
    if not settings.github_app_installation_id:
        return SOURCE_APP, "缺少 GITHUB_APP_INSTALLATION_ID（CLI 发布时必填）"
    return (
        SOURCE_APP,
        f"App {settings.github_app_id} / installation {settings.github_app_installation_id}",
    )
