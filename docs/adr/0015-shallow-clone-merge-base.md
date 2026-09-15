# 0015 · 浅克隆下 merge-base 必然失败：远端克隆路径从未被真实执行过

- 状态：已采纳
- 日期：2026-09-15
- 相关：`src/acra/repo/gateway.py`、`src/acra/orchestrator/pipeline.py`、`scripts/e2e_webhook.py`、ADR 0010 / 0013 / 0014

## 背景

第 5 项清单要求验证「验签 → 入队 → 审查」的真实链路。第一次真实投递（`action=opened`）
被接受、入队成功，然后 worker 里的审查在 `_prepare_diff` 第一步就失败了。

## 实验

按 §4.3 的克隆策略（裸仓库缓存 + `fetch --depth=1` 全分支）建仓后，
两个分支的 tip 之间**没有共同祖先**：

```
fetch --no-tags --depth=1 origin '+refs/heads/*:refs/remotes/origin/*'
→ 远端跟踪 origin/feature、origin/main 都在，且 <root>/shallow 存在（浅克隆）

git merge-base refs/remotes/origin/main refs/remotes/origin/feature
→ rc=1，stdout 为空，stderr 为空
```

而 `_prepare_diff` 的第一步就是 `merge_base(base, head)` —— 于是**远端克隆路径在真实
PR 上必然失败**，整次审查记成 `failed`。这条路径正是 webhook worker 走的那条。

## 为什么从来没被发现

| 路径 | 历史 | 结果 |
| --- | --- | --- |
| CLI（本地仓库） | 完整 | merge-base 正常 —— 本地怎么测都不会发现 |
| 单测 | 注入的假 handle / 本地夹具 | 根本不走远端克隆 |
| webhook worker（远端 `clone_url`） | **depth=1 截断** | 必然失败，且只在真实投递时才会走到 |

这与 0010（语料触达不到那一层）、0013（进程边界的隐形变量）、0014（零件齐了合起来没验过）
是同一类问题，而且是最难发现的那种：**单测全绿、CLI 全通，只有生产入口是坏的**。

## 决策

**一、`RepoHandle` 增加 `is_shallow` 与 `deepen(depth)`。**
`is_shallow` 看 `<root>/shallow` 是否存在；`deepen` 复用同一条 fetch 命令把深度提到 N，
失败返回 False（不抛），由调用方决定继续加深还是放弃。

**二、逐级加深而不是一次拉全量。** `DEEPEN_STEPS = (50, 200, 1000)`：
默认 `depth=1` 是为了省网络（§4.3 的初衷），只有真的需要共同祖先时才加深；
上限 1000 避免在大仓库上退化成全量克隆。

**三、加深成功记为信息性说明，不是降级。** 它说明的是
"这次为了拿到历史多花了多少网络"，不是"分析范围被砍" —— 混进 `notes` 会把
每次远端审查都标记成 degraded，让故障率指标变成噪音。

**四、加深到上限仍失败就显式失败。** 超大仓库 + 很老的 base 分支确实可能超出 1000，
这种情况下失败是诚实的行为（写明原因），而不是退回"只审不溯"的静默近似。

## 后果与已知限制

- 真实链路验证通过后，webhook 路径在**浅克隆可达**的仓库上可用。
  本地验证用的是本地路径当 `clone_url`（本机 git HTTPS 出不去，见 `scripts/e2e_webhook.py`
  顶部的偏差说明）—— 但被验证的是同一条代码路径，差异只在传输层。
- `--depth` 的上限意味着极老分支仍可能失败，此时报错信息会说明是"加深后仍无法计算
  merge-base"，而不是模糊的 GitError。
- 顺带记录：本机环境里 `git` 的 HTTPS 出不去（系统级 `http.proxy` 指向失效端口），
  所以**远端克隆只能靠 SSH 或由部署环境自行打通**。这不是代码缺陷，但会持续影响
  本机的端到端验证方式，已写进环境技能。
