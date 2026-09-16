"""L3 仓库级检索。

对应开发文档 §7.6 与 ADR 0006。三类线索各自回答一个问题：

| 线索 | 回答的问题 | 来源 |
| --- | --- | --- |
| 反向引用 `callers` | 这个改动影响了谁（影响面） | 仓库源码 |
| 相似实现 `similar` | 项目既有约定长什么样 | 仓库源码 |
| 历史评论 `prior_comments` | 这条是不是已经提过了 | 结论库（由 pipeline 提供） |

## 策略与限制必须如实上报

ADR 0006 规定按仓库规模二选一：文件数 < 5000 用向量检索（"每个方法的自然语言摘要 +
签名"建索引），≥ 5000 用符号名精确匹配 + 路径相似度。

**向量路径尚未实现** —— 它需要 embedding 提供方，以及一次"为每个方法生成摘要"的
预生成（那是另一条独立的成本与依赖）。当前任何规模都走符号匹配，`notes` 会写明
"向量策略未实现、已退化为符号匹配"。

这件事必须写出来，因为 `L3 跑了但没找到线索` 与 `L3 根本没按预期跑`
在最终报告上长得**完全一样**（都是"没有 L3 内容"），而处置方式相反：
前者可以接受，后者是缺陷。宁可多一行说明，也不静默退化。

## 反向引用为什么不查符号索引

ADR 0006 说反向引用"走符号索引、O(1) 命中"，但那个索引只对**变更文件及其引用类型**
按需建立（§7.5 的 L2 预算），不是全仓索引 —— 拿它查调用方只会稳定地返回空，
而"空"会被读成"没有调用方"。

因此这里改为在**有配额**的文件集合上做符号名精确匹配，候选按路径邻近度排序
（同目录 → 同顶层目录 → 其余），并把"配额是否用尽"如实上报：
用尽意味着线索可能不完整，那是降级；只是没找到，则不是。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acra.context.vector import MethodVectorIndex
from acra.models import CallSite, CodeSlice, FileDiff, SymbolSpan
from acra.repo.symbol_index import find_enclosing_spans, language_for_path

#: ADR 0006：文件数低于该阈值"本应"走向量检索
VECTOR_SEARCH_MAX_FILES = 5000
#: 只注入相似度高于该值的相似实现（ADR 0006 的阈值，两条策略共用同一语义）
SIMILARITY_THRESHOLD = 0.75
#: top-K（ADR 0006）
TOP_K_SIMILAR = 3

STRATEGY_VECTOR = "vector"
STRATEGY_SYMBOL = "symbol"

_DEFINITION_MARKERS = (
    "def ",
    "function ",
    "class ",
    "public ",
    "private ",
    "protected ",
    "static ",
    "void ",
    "async ",
    "func ",
)

#: 拿这些名字去找"相似实现"没有信息量：每个类里都有一个 `__init__`，
#: 于是"相似实现"会稳定地返回一堆构造器，把 token 花在最没有区分度的东西上。
#: 实测踩过：变更点落在 `__init__` 里（例如加了一行加锁）时，
#: 三条 top-3 相似实现全是各文件的 `__init__`，匹配度还都是 1.0。
_UNINFORMATIVE_NAMES = frozenset(
    {
        "__init__",
        "__new__",
        "__call__",
        "__enter__",
        "__exit__",
        "__repr__",
        "__str__",
        "main",
        "setup",
        "teardown",
    }
)


def is_informative_name(name: str) -> bool:
    """这个方法名值得拿去检索吗。"""
    if not name or name.lower() in _UNINFORMATIVE_NAMES:
        return False
    if name.startswith("__") and name.endswith("__"):
        return False
    return len(name) >= 3


def _normalize_name(name: str) -> str:
    return name.replace("_", "").lower()


def name_similarity(candidate: str, target: str) -> float:
    """两个符号名的相似度（0~1）。

    符号匹配是**精确**的，所以只在这两种情形给分：

    - 归一化后完全相同（忽略大小写与下划线）→ 1.0
    - 一方是另一方的严格前后缀且共同长度 ≥ 4 → 0.8
      （`findByStatus` / `findByStatusWithLock` 这种"加了后缀的同一族"）

    沿用 `SIMILARITY_THRESHOLD` 做门槛，与向量路径共用"只注入足够像的"语义。
    """
    a, b = _normalize_name(candidate), _normalize_name(target)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return 0.8
    return 0.0


def _looks_like_definition(line: str, name: str) -> bool:
    """`head` 里出现修饰符/关键字，说明是在声明而不是在调用。"""
    head = line.split(name, 1)[0]
    return any(marker in head for marker in _DEFINITION_MARKERS)


@dataclass(slots=True)
class L3Clues:
    callers: list[CallSite] = field(default_factory=list)
    similar: list[CodeSlice] = field(default_factory=list)
    #: 信息性说明（用了哪条策略、能力边界在哪）。**不是**降级：
    #: "小仓库本应走向量"是设计，"向量没实现所以走符号"也是事实，
    #: 都不是"本次分析出了问题"。
    notes: list[str] = field(default_factory=list)
    #: 线索可能不完整（扫描配额用尽）。**这才是降级** —— 范围被砍、结论可能不全。
    truncated: bool = False


class Retriever:
    """L3 线索检索器。"""

    def __init__(
        self,
        handle=None,
        settings=None,
        *,
        head_sha: str = "",
        repo_files: list[str] | None = None,
        vector_index: MethodVectorIndex | None = None,
    ) -> None:
        self.handle = handle
        self.settings = settings
        self.head_sha = head_sha
        self.repo_files = list(repo_files or [])
        self.vector_index = vector_index
        self._source_cache: dict[str, list[str]] = {}
        self._candidates_truncated = False
        self._vector_state: tuple[bool, str] | None = None

    # ---------------------------------------------------------------- 策略

    @property
    def enabled(self) -> bool:
        return bool(self.handle and self.settings and self.head_sha and self.repo_files)

    @property
    def strategy(self) -> str:
        """ADR 0006 按仓库规模选定的**应然**策略。"""
        if len(self.repo_files) < VECTOR_SEARCH_MAX_FILES:
            return STRATEGY_VECTOR
        return STRATEGY_SYMBOL

    def _vector_usable(self) -> bool:
        """向量路径是否真的可用（配置开启 + 后端就绪）。结果按实例缓存。"""
        if self._vector_state is None:
            if self.vector_index is None:
                self._vector_state = (False, "未启用（ACRA_L3_VECTOR_ENABLED=false）")
            else:
                ok, why = self.vector_index.backend.available()
                self._vector_state = (ok, why)
        return self._vector_state[0]

    @property
    def effective_strategy(self) -> str:
        """实际能用的策略。向量不可用时退化为符号匹配。"""
        return STRATEGY_VECTOR if self._vector_usable() else STRATEGY_SYMBOL

    @property
    def max_scan_files(self) -> int:
        return max(1, int(getattr(self.settings, "acra_l3_max_scan_files", 400)))

    @property
    def max_callers(self) -> int:
        return max(1, int(getattr(self.settings, "acra_l3_max_callers", 8)))

    @property
    def max_similar(self) -> int:
        return max(1, int(getattr(self.settings, "acra_l3_max_similar", TOP_K_SIMILAR)))

    # ---------------------------------------------------------------- 读取

    def _lines(self, path: str) -> list[str]:
        cached = self._source_cache.get(path)
        if cached is None:
            cached = self.handle.file_lines(self.head_sha, path)
            self._source_cache[path] = cached
        return cached

    def _candidates(self, primary_path: str) -> list[str]:
        """候选文件按路径邻近度排序：同目录 → 同顶层目录 → 其余。"""
        primary_dir = primary_path.rsplit("/", 1)[0] if "/" in primary_path else ""
        top = primary_path.split("/", 1)[0]

        def rank(path: str) -> tuple[int, str]:
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            if directory == primary_dir:
                return (0, path)
            if path.split("/", 1)[0] == top:
                return (1, path)
            return (2, path)

        usable = [
            p
            for p in self.repo_files
            if p != primary_path and language_for_path(p)
        ]
        ordered = sorted(usable, key=rank)
        if len(ordered) > self.max_scan_files:
            self._candidates_truncated = True
            ordered = ordered[: self.max_scan_files]
        return ordered

    # ---------------------------------------------------------------- 检索

    def clues(self, file_diff: FileDiff, spans: list[SymbolSpan]) -> L3Clues:
        """取一次 L3 线索。任何失败都退化为"没有线索 + 说明"，不抛异常。"""
        clues = L3Clues()
        if not self.enabled:
            clues.notes.append("L3 未启用（缺少仓库句柄、repo 文件清单或 head_sha）")
            return clues

        if not self._vector_usable():
            _, why = self._vector_state or (False, "")
            clues.notes.append(
                f"L3 向量检索不可用（{why}），已退化为符号名匹配"
                f"（仓库 {len(self.repo_files)} 个文件）"
            )

        names = self._symbol_names(spans)
        if not names:
            clues.notes.append(f"L3 未取到可检索的符号名（{file_diff.path}），跳过线索检索")
            return clues

        try:
            candidates = self._candidates(file_diff.path)
            callers = self._callers(names, file_diff.path, candidates)
            if self._vector_usable():
                similar, vector_notes = self._similar_via_vector(file_diff, spans)
                clues.notes.extend(vector_notes)
                if not similar:
                    # 向量无命中时退回符号匹配：有线索总比没有强，
                    # 退回这件事本身也写进说明，不静默。
                    clues.notes.append("L3 向量无命中，已退回符号名匹配")
                    similar = self._similar(file_diff.path, names, candidates)
            else:
                similar = self._similar(file_diff.path, names, candidates)
        except Exception as exc:  # noqa: BLE001 - 线索是增强项，失败不该影响主链路
            clues.notes.append(f"L3 检索失败（{type(exc).__name__}），本次无 L3 线索")
            return clues

        clues.callers = callers
        clues.similar = similar
        clues.truncated = self._candidates_truncated
        if self._candidates_truncated:
            clues.notes.append(
                f"L3 候选文件按配额截断为 {self.max_scan_files} 个，线索可能不完整"
            )
        if callers or similar:
            clues.notes.append(f"L3 线索：调用方 {len(callers)} 条 / 相似实现 {len(similar)} 条")
        else:
            clues.notes.append(
                f"L3 已执行但未找到调用方或相似实现（扫描 {len(candidates)} 个候选文件）"
            )
        return clues

    def _similar_via_vector(
        self, file_diff: FileDiff, spans: list[SymbolSpan]
    ) -> tuple[list[CodeSlice], list[str]]:
        """向量策略的相似实现：增量建索引 → 以变更方法文本为 query。"""
        notes: list[str] = []
        embedded, build_notes = self.vector_index.build(
            self.repo_files,
            self._lines,
            max_files=max(1, int(getattr(self.settings, "acra_l3_max_index_files", 2000))),
        )
        notes.extend(build_notes)
        if embedded:
            notes.append(f"向量索引：本次嵌入 {embedded} 个方法（增量，hash 未变不重嵌）")
        else:
            notes.append("向量索引：本次无新增方法需要嵌入（全部命中缓存）")
        similar = self.vector_index.query(file_diff, spans, top_k=self.max_similar)
        notes.append(f"向量检索策略={self.vector_index.backend.name}")
        return similar, notes

    @staticmethod
    def _symbol_names(spans: list[SymbolSpan]) -> list[str]:
        out: list[str] = []
        for span in spans:
            if (
                span.kind in ("method", "function", "constructor")
                and is_informative_name(span.name)
                and span.name not in out
            ):
                out.append(span.name)
        return out[:6]

    def _callers(self, names: list[str], primary: str, candidates: list[str]) -> list[CallSite]:
        """在候选文件里找调用点：只认"名字后面跟左括号"且不在定义行上。"""
        del primary  # 候选集合已排除自身；保留参数是为了让调用点读起来明确
        found: list[CallSite] = []
        for path in candidates:
            if len(found) >= self.max_callers:
                break
            for lineno, line in enumerate(self._lines(path), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith(("//", "#", "*", "/*")):
                    continue
                hit = next(
                    (
                        name
                        for name in names
                        if name in stripped
                        and f"{name}(" in stripped
                        and not _looks_like_definition(stripped, name)
                    ),
                    None,
                )
                if hit is None:
                    continue
                found.append(
                    CallSite(
                        symbol=hit,
                        caller_path=path,
                        caller_line=lineno,
                        snippet=stripped[:160],
                    )
                )
                if len(found) >= self.max_callers:
                    break
        return found

    def _similar(self, primary: str, names: list[str], candidates: list[str]) -> list[CodeSlice]:
        """找同名（或同族名）方法的既有实现，按相似度与路径邻近度排序取 top-K。"""
        scored: list[tuple[float, int, CodeSlice]] = []
        primary_dir = primary.rsplit("/", 1)[0] if "/" in primary else ""

        for path in candidates:
            lines = self._lines(path)
            if not lines:
                continue
            # 先廉价预筛：这个文件里出现过这些名字再解析，避免为每个候选付解析成本
            hot = [i for i, line in enumerate(lines, 1) if any(n in line for n in names)]
            if not hot:
                continue
            spans, _ = find_enclosing_spans("\n".join(lines), set(hot), path=path)
            for span in spans:
                if span.kind not in ("method", "function", "constructor"):
                    continue
                best = max((name_similarity(span.name, n) for n in names), default=0.0)
                if best < SIMILARITY_THRESHOLD:
                    continue
                directory = path.rsplit("/", 1)[0] if "/" in path else ""
                proximity = 0 if directory == primary_dir else 1
                scored.append(
                    (
                        best,
                        proximity,
                        CodeSlice(
                            path=path,
                            start_line=span.start_line,
                            end_line=span.end_line,
                            source=span.source,
                            score=best,
                        ),
                    )
                )

        scored.sort(key=lambda item: (-item[0], item[1], item[2].path))
        return [item[2] for item in scored[: self.max_similar]]
