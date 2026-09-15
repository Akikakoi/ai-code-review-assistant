# 0007 · tree-sitter 绑定版本必须与 grammar 的 ABI 对齐

- 状态：已采纳
- 日期：2026-09-13
- 相关：`pyproject.toml`、`src/acra/repo/symbol_index.py::tree_sitter_available`

## 背景

实现 L2 上下文时踩到一个**进程级崩溃**，排查过程值得完整记下来，
因为它看起来完全不像版本问题：

现象：

- `Parser.parse()` 返回正常，`root.type`、`node.children`、遍历都正常；
- 小输入（~2KB）一切正常；
- 输入到 ~23KB 时，访问 `node.start_point` / `node.end_point` / 字节偏移会触发
  Windows fatal exception: access violation，**进程直接死掉**，`try/except` 拦不住。

当时的版本组合：`tree-sitter 0.26.0` + `tree-sitter-java 0.23.5`。

## 诊断路径

刻意做了对照实验，把"解析"和"遍历"分开验证：

| 实验 | 结果 |
| --- | --- |
| 只读 `node.type` 并遍历全部 8024 个节点 | 正常 |
| 加上 `start_point` / `end_point` / `start_byte` / `end_byte` | 崩溃 |
| 单独读 `start_byte` / `end_byte`（节点全程保持引用） | 正常 |
| 逐个属性单独探测（节点列表全程保持引用） | 全部正常 |
| 用 `_load_parser`（lru_cache 的 Parser）做同样的事 | 崩溃 |

关键结论：不是遍历写法、不是对象生命周期、不是缓存策略的问题，
而是**读 `TSNode` 的字段**这一步本身在读错内存 —— 典型的 ABI/结构体布局不匹配。

## 决策

把绑定版本钉在 grammar 对应的 ABI 上：

```toml
"tree-sitter>=0.23,<0.24",
"tree-sitter-java>=0.23,<0.24",
"tree-sitter-typescript>=0.23,<0.24",
```

换成 `tree-sitter 0.23.2` 后，同一个 23KB / 425KB 输入全部正常。

## 后果与防线

1. **版本范围钉死**在 `pyproject.toml`，并写了注释说明为什么不是 `>=`；
2. `tree_sitter_available()` 不再只判断"grammar 能否加载"，
   而是真的解析一小段样本并**触碰那些会踩到 ABI 差异的字段**
   （`start_point` / `end_point` / 字节偏移 / `children`）。
   这样装错版本时表现为"该语言降级为开窗"，而不是随机的进程崩溃；
3. `acra doctor` 会输出 java / typescript 两种 grammar 的可用性；
4. `MAX_PARSE_BYTES`（2MB）之上直接开窗降级：
   超大文件的解析代价高、收益低（变更行通常只占极小一部分）。

## 教训

"小输入能跑通"不能证明解析层是健康的。涉及原生扩展的依赖，
**版本区间要显式对齐并在启动自检里真的走一遍数据路径**，
否则故障形态会是"跑得好好的突然整个进程没了"，极难归因。
