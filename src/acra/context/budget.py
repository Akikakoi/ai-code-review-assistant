"""Token 预算分配。

对应开发文档 §7.3：单块预算默认 16k 输入 tokens，按优先级顺序装配，超预算即截断。

各分区预算占比与截断策略（文档原表）：

| 分区                  | 占比 | 截断策略                                       |
| --------------------- | ---- | ---------------------------------------------- |
| 系统指令 + 输出 Schema | 10%  | 不截断，可精简                                  |
| 静态分析事实           | 10%  | 按与变更行的距离截断                            |
| L2 方法源码            | 45%  | 超出则保留方法头部与变更点附近                   |
| L1 diff                | 20%  | 不截断                                          |
| L3 线索                | 15%  | 超预算整块丢弃                                  |

全局预算：达到 80% 时停止 L3，达到 100% 时终止剩余分块（文档 §7.3 末段）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from acra.models import estimate_tokens

TruncateStrategy = Literal["no_truncate", "head", "head_tail", "focus", "drop"]

DEFAULT_SHARES: dict[str, float] = {
    "system": 0.10,
    "static": 0.10,
    "l2": 0.45,
    "l1": 0.20,
    "l3": 0.15,
}


@dataclass(slots=True)
class Section:
    """待装配的一个上下文分区。"""

    name: str
    text: str = ""
    share: float = 0.0
    strategy: TruncateStrategy = "head_tail"
    #: strategy="focus" 时使用：变更点在文本内的行号（1-based），用于"保留变更点附近"
    focus_line: int = 1
    #: strategy="drop"（如 L3）在超预算时整块丢弃
    priority: int = 0

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(slots=True)
class PackedSections:
    texts: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    total_tokens: int = 0

    def get(self, name: str) -> str:
        return self.texts.get(name, "")

    def note(self) -> str:
        bits: list[str] = []
        if self.truncated:
            bits.append("截断:" + ",".join(self.truncated))
        if self.dropped:
            bits.append("丢弃:" + ",".join(self.dropped))
        return "; ".join(bits)


def truncate_tokens(text: str, max_tokens: int) -> str:
    """按估算的 token 上限做头部截断。"""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    # estimate_tokens 用 len//3，因此 token 上限 → 字符上限的换算是 ×3，留 5% 余量
    max_chars = max(1, int(max_tokens * 3 * 0.95))
    return text[:max_chars]


def truncate_head_tail(text: str, max_tokens: int, focus_line: int = 1) -> str:
    """保留方法头部 + 变更点附近 + 方法收尾，中间用省略标记。

    用于 L2 方法源码：方法很长时，方法签名、变更点附近、以及收尾（资源释放/return）
    最有信息量，中间大段可以牺牲。

    关键约束：**变更点所在行必须被保留**。早期实现"从头截一段、从变更点截一段"，
    结果变更点落在两段之间的空档里被删掉了 —— 那等于把 L2 上下文的核心内容丢了。
    """
    if estimate_tokens(text) <= max_tokens:
        return text

    lines = text.split("\n")
    max_chars = max(1, int(max_tokens * 3 * 0.95))
    if sum(len(x) + 1 for x in lines) <= max_chars:
        return text

    total = len(lines)
    focus = max(0, min(total - 1, focus_line - 1))

    # 1) 头部：方法签名与注解所在
    head_end = 0
    used_head = 0
    head_limit = int(max_chars * 0.30)
    while head_end < total and used_head + len(lines[head_end]) + 1 <= head_limit:
        used_head += len(lines[head_end]) + 1
        head_end += 1

    # 2) 变更点窗口：以 focus 为中心向两侧扩张，保证 focus 一定在区间内
    focus_start = focus
    focus_stop = focus + 1
    used_focus = len(lines[focus]) + 1
    focus_limit = int(max_chars * 0.50)
    while True:
        moved = False
        if focus_start > head_end and used_focus + len(lines[focus_start - 1]) + 1 <= focus_limit:
            focus_start -= 1
            used_focus += len(lines[focus_start]) + 1
            moved = True
        if focus_stop < total and used_focus + len(lines[focus_stop]) + 1 <= focus_limit:
            used_focus += len(lines[focus_stop]) + 1
            focus_stop += 1
            moved = True
        if not moved:
            break

    # 3) 收尾：方法的结束部分
    tail_start = total
    used_tail = 0
    tail_limit = max_chars - used_head - used_focus - 40
    while tail_start > focus_stop and used_tail + len(lines[tail_start - 1]) + 1 <= tail_limit:
        tail_start -= 1
        used_tail += len(lines[tail_start]) + 1

    parts: list[str] = []
    if head_end > 0:
        parts.extend(lines[:head_end])
    if focus_start > head_end:
        parts.append(f"... [省略 {focus_start - head_end} 行] ...")
    parts.extend(lines[focus_start:focus_stop])
    if tail_start > focus_stop:
        parts.append(f"... [省略 {tail_start - focus_stop} 行] ...")
    if tail_start < total:
        parts.extend(lines[tail_start:])

    joined = "\n".join(parts)
    # 兜底：单行超长时（压缩过的 JS、生成代码）上面按行分配预算会失效，
    # 因为唯一的"焦点行"本身就超过整个预算，此时必须硬截断，否则预算形同虚设。
    if len(joined) > max_chars:
        head_keep = max(0, max_chars - 30)
        return joined[:head_keep] + "\n... [本行超长，已硬截断] ..."
    return joined


class TokenBudget:
    """按占比装配上下文分区。"""

    def __init__(self, total: int, shares: dict[str, float] | None = None) -> None:
        self.total = max(0, total)
        self.shares = dict(shares or DEFAULT_SHARES)

    def limit_for(self, name: str) -> int:
        return int(self.total * self.shares.get(name, 0.0))

    def pack(self, sections: Iterable[Section], *, stop_budget: int | None = None) -> PackedSections:
        """按 priority 升序装配，超预算按各自策略处理。"""
        result = PackedSections()
        cap = self.total if stop_budget is None else max(0, min(self.total, stop_budget))
        used = 0

        for section in sorted(sections, key=lambda s: s.priority):
            if not section.text:
                result.texts[section.name] = ""
                continue
            # L3：超预算整块丢弃
            if section.strategy == "drop" and used + section.tokens > cap:
                result.dropped.append(section.name)
                result.texts[section.name] = ""
                continue

            own_limit = self.limit_for(section.name) or self.total
            remaining = cap - used
            allowance = max(0, min(own_limit, remaining))

            text = section.text
            if section.strategy == "no_truncate":
                # 不截断分区仍受剩余预算约束，否则会把其它分区挤没
                if section.tokens > allowance:
                    text = truncate_tokens(text, allowance)
                    if text != section.text:
                        result.truncated.append(section.name)
            elif section.strategy == "head":
                text = truncate_tokens(text, allowance)
                if text != section.text:
                    result.truncated.append(section.name)
            elif section.strategy == "focus":
                text = truncate_head_tail(text, allowance, section.focus_line)
                if text != section.text:
                    result.truncated.append(section.name)
            else:
                text = truncate_head_tail(text, allowance)
                if text != section.text:
                    result.truncated.append(section.name)

            result.texts[section.name] = text
            used += estimate_tokens(text)

        result.total_tokens = used
        return result


@dataclass(slots=True)
class RunBudget:
    """全次审查的总预算（文档 §7.3 末段）。"""

    total: int = 200_000
    used: int = 0

    @property
    def ratio(self) -> float:
        return self.used / self.total if self.total else 0.0

    @property
    def allow_l3(self) -> bool:
        return self.ratio < 0.80 and self.total > 0

    @property
    def exhausted(self) -> bool:
        return self.total > 0 and self.used >= self.total

    def charge(self, tokens: int) -> None:
        self.used += max(0, tokens)

    def remaining(self) -> int:
        return max(0, self.total - self.used)
