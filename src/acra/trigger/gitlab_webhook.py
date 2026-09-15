"""GitLab webhook 入口（阶段四实现，见文档 §16）。

与 GitHub 的差异（实现时需一并处理）：

- 验签是 `X-Gitlab-Token` 明文比对，不是 HMAC-SHA256；
- 事件类型来自 `X-Gitlab-Event: Merge Request Hook`，`object_attributes.action` 取值
  为 `open` / `update` / `reopen`，与 GitHub 的 `opened` / `synchronize` 不同名；
- `object_attributes.last_commit.id` 才是 head_sha，`target_branch` / `source_branch`
  对应 base/head。

阶段一只保留路由占位，返回 501，避免静默 404 让人误以为配错了地址。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.post("/webhook/gitlab")
async def gitlab_webhook() -> dict[str, str]:
    raise HTTPException(status_code=501, detail="GitLab 适配在阶段四实现（文档 §16 阶段四）")
