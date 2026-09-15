"""高风险文件与类别判定、低价值路径过滤、预算守卫。

对应开发文档 §4.1（跳过条件）、§4.2（预算守卫）、§7.2（L2→L3 升级触发条件）、
§10.2（跳过低价值文件）。

这些判定决定了"看什么、看多深"，对成本与信噪比的影响比调模型更大，因此单独成模块并
逐条单测。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from acra.models import DiffSet, FileDiff

# ---------------------------------------------------------------------------- 低价值路径

DOC_EXTENSIONS = {
    ".md",
    ".mdx",
    ".rst",
    ".txt",
    ".adoc",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
}

LOCK_FILENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "go.sum",
    "gradle.lockfile",
}

GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    ".pb.go",
    "_pb2.py",
    ".generated.ts",
    ".g.dart",
)

GENERATED_DIR_MARKERS = (
    "/dist/",
    "/build/",
    "/out/",
    "/target/",
    "/node_modules/",
    "/__snapshots__/",
    "/vendor/",
    "/.next/",
)

SOURCE_EXTENSIONS = {".java", ".ts", ".tsx", ".js", ".jsx", ".kt", ".py", ".go", ".rs"}


def is_low_value_path(path: str) -> bool:
    """文档、锁文件、生成代码、快照、构建产物 —— 直接跳过（文档 §10.2）。"""
    p = path.replace("\\", "/")
    low = p.lower()
    name = PurePosixPath(low).name
    suffix = PurePosixPath(low).suffix

    if suffix in DOC_EXTENSIONS:
        return True
    if name in LOCK_FILENAMES:
        return True
    if low.endswith(GENERATED_SUFFIXES):
        return True
    if any(marker in f"/{low}" for marker in GENERATED_DIR_MARKERS):
        return True
    return bool(re.search(r"\.(d)\.ts$", low))


def is_doc_only(paths: list[str]) -> bool:
    """纯文档变更（文档 §4.1 跳过条件）。"""
    return bool(paths) and all(is_low_value_path(p) for p in paths)


def is_source_file(path: str) -> bool:
    return PurePosixPath(path.lower()).suffix in SOURCE_EXTENSIONS


# ---------------------------------------------------------------------------- 高风险判定

RISK_CATEGORIES: set[str] = {"bug", "security", "concurrency", "performance"}

TRANSACTION_MARKERS = (
    "@Transactional",
    "TransactionTemplate",
    "PlatformTransactionManager",
    "TransactionSynchronizationManager",
)

CONCURRENCY_MARKERS = (
    "synchronized",
    "ReentrantLock",
    "ReadWriteLock",
    "AtomicInteger",
    "AtomicLong",
    "AtomicReference",
    "volatile",
    "ConcurrentHashMap",
    "CopyOnWriteArrayList",
    "ThreadPoolExecutor",
    "Executors.",
    "CompletableFuture",
    "Semaphore",
    "CountDownLatch",
    "ThreadLocal",
    "@Async",
    "new Thread(",
)

SQL_MARKERS = (
    "select ",
    "select(",
    "insert into",
    "update ",
    "delete from",
    "jdbc",
    "JdbcTemplate",
    "SqlSession",
    "@Select",
    "@Insert",
    "@Update",
    "@Delete",
    "createQuery",
    "nativeQuery",
)

PERMISSION_MARKERS = (
    "@PreAuthorize",
    "@Secured",
    "@RolesAllowed",
    "hasRole",
    "hasAuthority",
    "hasPermission",
    "checkPermission",
    "require_admin",
    "isAdmin",
    "getCurrentUser",
    "SecurityContext",
)

SIGNATURE_CHANGE_MARKERS = ("public ", "protected ", "export ", "def ")

_HIGH_RISK_PATH_RE = re.compile(
    r"(Service|Controller|Repository|Mapper|Dao|Handler|Listener|Consumer|Producer|"
    r"Lock|Queue|Scheduler|Task|Payment|Order|Auth|Security)",
    re.IGNORECASE,
)


def looks_high_risk(file_diff: FileDiff, added_text: str = "") -> bool:
    """高风险文件判定：路径特征 + 变更内容特征。

    用于大 PR 降级时挑出"只审这些文件"（文档 §4.2 预算守卫）。
    """
    text = added_text or file_diff.added_text()
    if _HIGH_RISK_PATH_RE.search(file_diff.path):
        return True
    if any(m in text for m in TRANSACTION_MARKERS):
        return True
    if any(m in text for m in CONCURRENCY_MARKERS):
        return True
    if any(m in text for m in SQL_MARKERS):
        return True
    return bool(any(m in text for m in PERMISSION_MARKERS))


def l3_triggers(file_diff: FileDiff, added_text: str = "") -> list[str]:
    """L2 → L3 升级触发条件（文档 §7.2）。"""
    text = added_text or file_diff.added_text()
    reasons: list[str] = []
    if any(m in text for m in SIGNATURE_CHANGE_MARKERS) and re.search(
        r"\b(public|protected)\b[^;{]*\(", text
    ):
        reasons.append("public_signature_changed")
    if any(m in text for m in CONCURRENCY_MARKERS):
        reasons.append("concurrency_primitive")
    if any(m in text for m in TRANSACTION_MARKERS):
        reasons.append("transaction_annotation")
    if any(m in text for m in SQL_MARKERS):
        reasons.append("sql_touched")
    if any(m in text for m in PERMISSION_MARKERS):
        reasons.append("permission_logic")
    return reasons


# ---------------------------------------------------------------------------- 预算守卫


class GuardResult:
    """预算守卫的结果。

    `notes` 与 `info` 必须分开：前者是**真正的降级**（范围被砍、结论可能不完整），
    后者只是**信息性说明**（跳过了文档类文件、按配置忽略了某些路径）。
    混在一起会让每次运行都被标记成 degraded，把 status 与失败率指标变成噪音。
    """

    __slots__ = ("diff_set", "notes", "info", "truncated")

    def __init__(
        self,
        diff_set: DiffSet,
        notes: list[str],
        info: list[str] | None = None,
        truncated: bool = False,
    ) -> None:
        self.diff_set = diff_set
        self.notes = notes
        self.info = info or []
        self.truncated = truncated


def apply_budget_guard(diff_set: DiffSet, settings, *, ignore_paths: list[str] | None = None) -> GuardResult:
    """预算守卫（文档 §4.2）。

    | 条件                     | 动作                                   |
    | 变更行数 > 3000          | 只审高风险文件 + 说明                   |
    | 变更文件数 > 80          | 同上，并跳过 L3 上下文                  |
    """
    notes: list[str] = []
    info: list[str] = []
    files = diff_set.files

    ignored = [p for p in files if is_low_value_path(p.path)]
    if ignored:
        info.append(f"跳过低价值文件 {len(ignored)} 个")
    files = [f for f in files if not is_low_value_path(f.path)]

    if ignore_paths:
        patterns = [_glob_to_re(p) for p in ignore_paths]
        kept = []
        for f in files:
            if any(r.match(f.path) for r in patterns):
                continue
            kept.append(f)
        if len(kept) != len(files):
            info.append(f"按仓库配置忽略 {len(files) - len(kept)} 个文件")
        files = kept

    # 没有可评论行的文件（纯删除、二进制、纯模式变更）直接跳过
    files = [f for f in files if f.added_line_numbers]

    truncated = False
    if len(files) > settings.acra_max_changed_files:
        files = _prioritize_high_risk(files)
        files = files[: settings.acra_max_changed_files]
        notes.append(f"变更文件数超过 {settings.acra_max_changed_files}，已截断为高风险优先")
        truncated = True

    if sum(f.added_count for f in files) > settings.acra_max_changed_lines:
        files = [f for f in files if looks_high_risk(f)]
        notes.append(
            f"变更行数超过 {settings.acra_max_changed_lines}，已降级为只审高风险文件，并跳过 L3 上下文"
        )
        truncated = True

    return GuardResult(
        DiffSet(
            base_sha=diff_set.base_sha,
            head_sha=diff_set.head_sha,
            merge_base_sha=diff_set.merge_base_sha,
            files=files,
        ),
        notes,
        info,
        truncated,
    )


def _prioritize_high_risk(files: list[FileDiff]) -> list[FileDiff]:
    return sorted(files, key=lambda f: (not looks_high_risk(f), -f.added_count, f.path))


def _glob_to_re(pattern: str) -> re.Pattern[str]:
    """极简 glob → 正则，支持 `**/` 与 `*`。"""
    esc = re.escape(pattern.replace("\\", "/"))
    esc = esc.replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.compile("^" + esc + "$")
