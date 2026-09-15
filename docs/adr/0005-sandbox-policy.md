# 0005 · 沙箱策略：Docker + `--network none`，不可用时降级并记录

- 状态：已采纳（阶段三启用）
- 日期：2026-09-13
- 相关：`sandbox/Dockerfile`、`sandbox/policy.json`、`src/acra/analysis/static_runner.py`、文档 §12.2

## 背景

静态分析工具与 `run_test` 都要执行"仓库里的代码"。被审查的代码是不可信输入源，
这一点必须在架构上当作与"用户输入"同等对待。

## 决策

所有需要执行代码的环节都跑在容器里：

```
--network none                 禁用网络（硬要求：既是防数据外传，也是防依赖下载）
--read-only + tmpfs /tmp       代码只读挂载，构建产物落 tmpfs
--memory 2g --cpus 2 --pids-limit 256
--user 1000:1000               非 root
禁止 --privileged、禁止挂载 docker socket
--timeout 120                  超时即放弃该工具，不阻塞主链路
```

## 理由

- 审查进程持有的 token 只有 `contents:read` + 评论写权限（§12.1），
  即便被提示词注入也无法推送代码 —— 沙箱是第二道防线；
- 静态分析工具的依赖（JDK、Node、Semgrep 规则集）体积大且需要联网更新，
  放进镜像一次构建、每次只读挂载，比每次拉取快且可控。

## 后果

- **阶段一沙箱默认关闭**（`ACRA_SANDBOX_ENABLED=false`）。阶段一不执行仓库代码
  （静态分析未接入、`run_test` 未实现），因此不需要隔离能力；先跑通闭环再上隔离。
- CI 环境本身已在容器内时，退化为"子进程 + rlimit + 独立工作目录"，
  并把该降级带来的风险写进运行手册（文档 §12.2 明确要求记录）。
- `run_test` 在 Phase 1 默认关闭；Phase 2 仅当模型明确说明"需要验证某假设"时才允许。
