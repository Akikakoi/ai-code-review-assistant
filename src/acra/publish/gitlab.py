"""GitLab 输出（阶段四实现，见文档 §16 阶段四交付物）。

GitLab 与 GitHub 的模型差异，需要在实现时一并处理：

- 行级评论走 `POST /projects/:id/merge_requests/:iid/discussions`，定位参数是
  `position.base_sha / head_sha / new_path / new_line`，比 GitHub 多一次显式定位；
- 没有 Check Run，对应概念是 Commit Status（`/statuses/:sha`），只有
  pending/running/success/failed/canceled 五态，**没有 neutral** ——
  需要把"无高优先级问题"和"降级"都映射为 success，并在描述里说明，避免误阻塞；
- 签名校验走 `X-Gitlab-Token` 明文比对，不是 HMAC。

阶段一只提供占位实现，保证 `trigger` / `publish` 的分支结构完整。
"""

from __future__ import annotations

from typing import Any


class GitlabPublisher:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError("GitLab 适配在阶段四实现（文档 §16 阶段四）")

    async def create_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError
