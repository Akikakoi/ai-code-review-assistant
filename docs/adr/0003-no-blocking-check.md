# 0003 · 是否阻塞合并：不阻塞，不用 REQUEST_CHANGES

- 状态：已采纳
- 日期：2026-09-13
- 相关：`src/acra/publish/github.py`、`src/acra/orchestrator/degrade.py::check_run_conclusion`、文档 §4.9

## 背景

一个会自动"请求修改"的机器人在早期必然误报，而误报会直接阻塞团队的发版节奏。
被关掉一次，就再也不会被打开。

## 决策

- 提交 review 时 `event` **固定为 `COMMENT`**，代码中不存在 `REQUEST_CHANGES` 分支；
- Check Run 结论只在 `success` 与 `neutral` 之间取值，**永不使用 `failure`**；
- 不设 required check，不阻断合并。

## 理由

唯一的最终成功标准是"团队没有关闭这个机器人"（文档 §1.4）。
合并的否决权必须留在人手里；机器人只负责把信息送到人眼前。

## 后果

- 机器人无法强制任何事，效果完全取决于信噪比 —— 因此 §9 的降噪流水线不是可选项；
- `--fail-on` 只存在于 **CLI 模式**，供使用方在自己的 CI 里主动选择门禁强度，
  平台侧行为不受其影响；
- 用 `neutral` 表达"有问题但没有阻塞力"，配合 `title` / `summary` 说明严重度。
