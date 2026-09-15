"""GitHub 输出：提交 review、写 Check Run。

对应开发文档 §4.9。

关键约束（都是设计决策，不是实现细节）：

- **单次 Review 提交**：一次 API 调用同时完成 summary 与行级评论，避免多次请求之间
  出现半成品状态；
- `event` 固定 `COMMENT`，**绝不使用 `REQUEST_CHANGES`** —— 不阻塞合并（ADR 0003）；
- `side` 固定 `RIGHT`，行号必须是新文件侧；
- 幂等：提交前查询该 head_sha 是否已有本 App 的 review，有则跳过（文档 §4.9 要点）；
- Check Run 结论映射：无高优先级 → `success`；有高优先级 → `neutral`（不用 `failure`）；
  失败或降级 → `neutral` + 说明。
- 发布失败不阻塞主链路，退避重试（文档 §17 风险表）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from acra.errors import AcraError

DEFAULT_API_BASE = "https://api.github.com"
BOT_REVIEW_MARKERS = ("<!-- acra:review -->", "由 acra 生成")

CONFLICT_STATUS = {409, 422}


class GithubError(AcraError):
    pass


class GithubPublisher:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = DEFAULT_API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        if not token:
            raise GithubError("缺少 GitHub token")
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=api_base.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "acra/0.1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GithubPublisher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last = GithubError(f"GitHub 网络错误：{type(exc).__name__}: {exc}")
                await asyncio.sleep(min(8.0, 1.0 * 2**attempt))
                continue

            if resp.status_code >= 500:
                last = GithubError(f"GitHub {resp.status_code}：{resp.text[:300]}")
                await asyncio.sleep(min(8.0, 1.0 * 2**attempt))
                continue
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                raise GithubError(f"GitHub 限流：{resp.text[:300]}")
            if resp.status_code >= 400:
                raise GithubError(f"GitHub {resp.status_code}：{resp.text[:400]}")
            return resp
        raise last or GithubError("GitHub 请求失败")

    # ------------------------------------------------------------------ review

    async def list_reviews(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
        )
        data = resp.json()
        return data if isinstance(data, list) else []

    async def existing_review_for_head(
        self, owner: str, repo: str, pr_number: int, head_sha: str
    ) -> dict[str, Any] | None:
        """幂等检查：该 head_sha 上是否已有本 App 发布的 review。"""
        try:
            reviews = await self.list_reviews(owner, repo, pr_number)
        except GithubError:
            return None
        for review in reviews:
            body = review.get("body") or ""
            if review.get("commit_id") == head_sha and any(m in body for m in BOT_REVIEW_MARKERS):
                return review
        return None

    async def create_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        head_sha: str,
        body: str,
        comments: list[dict[str, Any]],
        event: str = "COMMENT",  # 固定 COMMENT，绝不 REQUEST_CHANGES
        idempotent: bool = True,
    ) -> dict[str, Any]:
        if idempotent:
            existing = await self.existing_review_for_head(owner, repo, pr_number, head_sha)
            if existing:
                return {"skipped": True, "reason": "already_reviewed", "review_id": existing.get("id")}

        payload: dict[str, Any] = {
            "commit_id": head_sha,
            "body": body,
            "event": event,
        }
        if comments:
            payload["comments"] = comments

        resp = await self._request(
            "POST", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", json=payload
        )
        data = resp.json()
        return {
            "skipped": False,
            "review_id": data.get("id"),
            "html_url": data.get("html_url"),
            "comments": len(comments),
        }

    # ------------------------------------------------------------------ check run

    async def create_check_run(
        self,
        owner: str,
        repo: str,
        *,
        head_sha: str,
        name: str = "acra / 代码审查",
        conclusion: str = "neutral",
        title: str = "",
        summary: str = "",
        details_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title or name, "summary": summary[:65000]},
        }
        if details_url:
            payload["details_url"] = details_url
        resp = await self._request("POST", f"/repos/{owner}/{repo}/check-runs", json=payload)
        return resp.json()


def conclusion_for(has_high: bool, failed_or_degraded: bool) -> str:
    """Check Run 结论映射（文档 §4.9）。"""
    if failed_or_degraded:
        return "neutral"
    return "neutral" if has_high else "success"
