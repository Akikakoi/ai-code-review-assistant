"""评估体系（阶段二实现，见开发文档 §11）。

这一层决定项目能不能活下来：没有评估，任何提示词改动都是凭感觉。

阶段二交付物：

- `dataset.py`：从历史 PR 回溯构建基准集（200 正例 + 200 反例起步）；
  缺陷注入脚本（20 类常见缺陷，带 ground truth 行号，用于精确度量漏报）；
- `runner.py`：跑数据集，落 `eval_result` 表；
- `metrics.py`：Precision / Recall / Noise rate / Anchor validity / Verify rejection rate；
- `acra eval run --dataset eval/cases.jsonl --compare-baseline eval/baseline.json`。

提示词版本管理（§11.3）：每次改提示词必须提升 `PROMPT_TEMPLATE_VERSION`、跑全量评估、
Precision 下降超过 2 个百分点不允许合并，并把变更前后指标记入
`docs/prompt-changelog.md`。

指标目标见 §11.2：Precision ≥ 0.60、Recall ≥ 0.35、Noise rate ≤ 0.30、
Anchor validity ≥ 0.98、Verify rejection rate 0.3~0.6。
"""

from __future__ import annotations

PRECISION_TARGET = 0.60
RECALL_TARGET = 0.35
#: §11.2 的 Noise rate 目标。注意它的定义需要平台交互数据（评论是否被理会/resolve），
#: 离线算不出来，因此不计入 `targets()`；离线可算的是"误报率"（FP / 上报数）。
NOISE_RATE_TARGET = 0.30
ACTION_RATE_TARGET = 0.25
ANCHOR_VALIDITY_TARGET = 0.98
AVG_COMMENTS_TARGET = 6.0
P95_LATENCY_TARGET_MS = 150_000
#: §11.2 的 p95 目标是 150 秒
P95_LATENCY_TARGET = P95_LATENCY_TARGET_MS
#: §11.2 的参考区间（不是硬目标）：低于下限说明 Scan 太严，高于上限说明 Verify 可能失效
VERIFY_REJECTION_MIN = 0.30
VERIFY_REJECTION_MAX = 0.60
#: §11.2 的 Recall 命中口径：行号 ±3 且类别相近
RECALL_LINE_TOLERANCE = 3
