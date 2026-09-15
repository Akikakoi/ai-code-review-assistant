# 0012 · 发布层的真实缺口不是"未验证"，是"没人调用"

- 状态：已采纳
- 日期：2026-09-15
- 相关：`src/acra/publish/github.py`、`src/acra/publish/factory.py`、`src/acra/github_app.py`、`src/acra/cli.py`、`src/acra/main.py`、`docs/adr/0010`

## 背景

`GithubPublisher` 的单测很齐：`event` 必须是 `COMMENT`、行级评论必须带 `side: RIGHT`、
同 head_sha 幂等跳过、Check Run 结论映射、5xx 重试、403 限流、422 让调用方降级 ——
每一条设计决策都有断言，全部用 `respx` 拦截，离线可跑。

但**生产代码里没有任何一处构造过 `GithubPublisher`**：

- `cli.review` 调 `run_review(...)` 时不传 `publisher`；
- `main.py` 的 webhook 内联 worker 不传 `publisher`；
- `acra worker` 的 handler 也不传 `publisher`。

于是 `pipeline.run_review` 里那段 `if publisher is not None and ...` 永远不成立。
**即使 webhook 验签 100% 正确、队列与编排全部工作，全链路的终点也只是"没发评论"。**

这比 ADR 0010 记的那一类更隐蔽。0010 是"语料触达不到那一层"（接口在、逻辑在，
只是没有能触发它的样本）；而这里是**根本没有人调用它** —— 单测越齐全，
这个缺口越不容易被看出来，因为"测试全绿"会被读成"这一层是好的"。

同一个缺口还有第二个面：`RepoHandle.token` 早就实现了 `http.extraHeader` 注入
（文档 §4.4），但 `pipeline._open_repo` 也从不传 token —— 私有仓库连克隆都过不去。

## 决策

**一、App 认证单独成模块**：`src/acra/github_app.py`。
App ID + 私钥 → RS256 签 JWT → 换 installation token，按 installation 缓存，
剩余不足 5 分钟时提前刷新。理由不必展开，但两个细节要留档：

- JWT 的 `iat` 必须**回拨 60 秒**。客户端时钟快于 GitHub 时，不回拨会被判
  `iat in the future`，而且报错信息不会告诉你这是时钟问题；
- GitHub 对 App JWT 的有效期有 10 分钟硬上限，这里取 9 分钟。

**二、凭据解析与 publisher 生命周期收进一处**：`src/acra/publish/factory.py`。
入口侧只剩 `resolve_access` + `async with open_publisher`。

关键取舍：**未配置凭据时产出 `None`，而不是抛异常** —— 这正是 pipeline 里
"不发布"的语义（本地 `--dry-run` 就是它）。但 `GithubAccess` 带上 `source` 与 `error`，
把「本来就不用发」（`source=none`）与「本该能发却没发成」（`error` 非空）分开：
后者是故障，必须在日志与总结里看得见。这两件事在 summary 上长得一样，
但处置方式完全相反。

**三、接进所有该发布的入口**：webhook 内联 worker、`acra worker`，
并给 CLI 加 `--publish` / `--repo-full-name` —— 本地模式也必须能走一遍真实发布，
否则"发布链路可用"这件事只能靠线上流量来证明。

**四、克隆与发布共用同一份凭据**：`run_review(clone_token=...)`。
私有仓库的克隆发生在发布之前，两者生命周期并不重合，所以 `clone_token` 是独立参数，
不从 publisher 里反查。

**五、保留静态 token 逃生口**：`ACRA_GITHUB_TOKEN`（`source=static_token`）。
它存在的唯一理由是让没有 App 的环境（本地调试、CI）也能把发布链路真跑一遍。
生产仍走 App。**这个口子必须显式存在**，否则"没有 App 就没法验证发布"
会长期把这一层留在未验证状态 —— 而它已经这样留了很久。

## 后果与已知限制

- **App JWT 上限 10 分钟**、installation token 上限 1 小时：两者都是平台硬限制，
  刷新逻辑必须存在，不能"取一次用到底"。
- token 缓存是**进程内**的。多副本部署时每个副本各自换取一次，这是可接受的
  （换取本身很便宜），但如果将来打 GitHub 限流，这里是第一个要改的地方。
- 静态 token 优先级高于 App。**这是刻意的**：有人在 `.env` 里写了 token，
  就该用它 —— 否则"明明配了却还是走 App"会变成一个很难查的问题。
- `GithubAccess` 的字段被日志与总结引用，改名会让排查时看不到凭据来源，
  因此有一个测试专门守字段集合。
- **尚未完成**：App 认证与发布链路目前只有离线（`respx`）验证。
  真实 App 凭据下的"建 PR → 发 review → 断言评论 → 关 PR"（文档 §14.4）
  需要外部凭据，列入待办；在它跑通之前，**不能声称发布层已端到端可用**。
