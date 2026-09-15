# 0014 · 发布链路的首次真实端到端验证，及它找出的三个问题

- 状态：已采纳
- 日期：2026-09-15
- 相关：`src/acra/publish/github.py`、`src/acra/publish/renderer.py`、`src/acra/postprocess/validator.py`、`scripts/e2e_publish.py`、ADR 0010 / 0012

## 背景

ADR 0012 把 `publisher` 接进了 webhook worker 与 CLI，但当时只有 `respx` 拦截的离线测试。
本轮补上文档 §14.4 要求的真实验证：**在真实仓库上建 PR → 真发一次 review → 从平台侧回读断言 → 关 PR**。
实现为 `scripts/e2e_publish.py`，可重复执行。

第一次跑通就找出三个问题，**全部是离线测试结构上覆盖不到的**。

## 发现一：细粒度 PAT 建不了 Check Run（文档自相矛盾，实测为准）

GitHub 文档自己在两处说法不一致：checks 的写权限写着 "only available to GitHub Apps"，
而同一页的 fine-grained token 小节又列出细粒度 PAT 需要 `Checks: write`。

实测（细粒度 PAT，权限含 `Checks: Read and write`）：

```
POST /repos/{owner}/{repo}/check-runs -> 403
{"message":"Resource not accessible by personal access token"}
```

**结论：Check Run 只能由 GitHub App（或其 installation token，含 Actions 的
`GITHUB_TOKEN`）创建。** 验证主体是用户 PAT 时，review 评论照常发布，
Check Run 必然失败。

这条不致命，因为 `_publish` 早就把 Check Run 单独包在 `try/except` 里、
失败只记 `check_run_error`，不会连带丢掉评论 —— 这个容错设计在这里第一次真正生效。

**顺带修掉的诊断缺陷**：`check_run_error` 原本只记异常类型（`"GithubError"`），
403 与 5xx 在排查时长得一模一样。现在连消息一起记。

## 发现二：summary 里从来没有幂等标记

`existing_review_for_head` 用 `BOT_REVIEW_MARKERS = ("<!-- acra:review -->", "由 acra 生成")`
判断"这条 review 是不是本工具发的"。但实测回读 PR 时，**在 summary 里找不到
`<!-- acra:review -->`** —— `render_summary` 只加了 `FOOTER`，而 FOOTER 里那句
"由 acra 生成"是人类可见的中文。

也就是说幂等**当时是靠文案那半撑着的**。改一次文案 → 幂等静默失效 →
同一 `head_sha` 每次重跑都再发一条 review，而"重复发评论"正是最容易被投诉的行为。

修法：summary 也带上 `BOT_MARKER`。它是渲染后不可见的 HTML 注释，
正是为这种用途存在的。并用一个测试把"summary"与"幂等判据"钉在一起 ——
两者分居两个文件，任何一边改动都可能让它们悄悄对不上。

## 发现三（最严重）：交叉验证把"引用真实代码"当成了"编造证据"

第 5 步的文档写的是：

> 若 evidence 中引用了**静态规则 ID**，则校验该规则确实在本次输出中；否则视为编造证据 → `confidence *= 0.5`

实现却是：

```python
if penalize_unmatched and raw.evidence and not matched and available:
    raw.confidence *= 0.5
```

—— 只要 evidence 非空且没匹配上任何规则就惩罚，**不管它是不是在引用规则**。

实测后果：模型报了一条 `security / blocker / confidence 0.9` 的 SQL 注入，
引用的证据是那一行**真实存在、可定位**的拼接 SQL：

```
sql = "select * from orders where id = '" + str(order_id) + "'"
```

本次静态结果里没有对应的规则 ID，于是置信度被砍半到 0.45，
**低于第 8 步门槛 0.5 被整条丢弃** —— 一条真实的 SQL 注入没有到达作者。
而报告上只显示"0 条结论"，没有任何地方提示"有一条被误判成了编造证据"。

修法：惩罚的触发条件从"evidence 非空"收紧为"**声称**引用了静态规则"
（新增 `claims_static_rule`，看形态不看匹配结果：`tool:rule` 前缀，或裸规则代码）。

**取舍方向写在这里，因为它决定以后遇到同类边界怎么选：**

> 宁可少惩罚，也不要静默丢掉一条真结论。
> 漏掉一次惩罚的代价是"多留了一条可能不可靠的结论"—— 作者看得见、能判断；
> 错罚一次的代价是"一条真问题永久消失"—— 没人看得见。

## 后果与已知限制

- `scripts/e2e_publish.py` 会真实建 PR、发评论、然后关 PR 并删分支。
  **它会留下痕迹**：PR 记录在仓库里（已关闭），review 与评论也在。
  因此它只应在测试仓库或本仓库上跑，不要指向生产仓库。
- 脚本已加 `--keep` 以便人工查看现场；默认自动收尾。收尾失败时会打印手工清理命令，
  不会静默留下一个开着 PR。
- 删除远端分支走 `git push --delete`（SSH），不走 API：
  细粒度 PAT 只有 `Contents: read`，删 ref 需要 write。
- 本轮仍未验证：**GitHub App 形态下的发布**（需要 App 凭据）。
  PAT 形态已端到端验证通过；App 形态的差异主要在 token 获取与 Check Run 能力，
  而 Check Run 正是 App 才能建的那个 —— 所以"App 形态能建 Check Run"这件事
  目前仍是文档结论，不是实测结论。
