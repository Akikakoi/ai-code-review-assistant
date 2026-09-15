"""静态分析编排。

对应开发文档 §4.5 与 §16 阶段二交付物第 2 项：「`static_runner` 接入 Semgrep + 一个
语言原生 linter，diff-aware 过滤」。

三条硬约束：

1. **不执行仓库代码。** 只跑 linter 的静态检查，不做构建、不安装依赖、不联网。
   与 §12.2 的沙箱策略一致；阶段三接沙箱后这里只需换掉执行后端。
2. **只跑变更文件。** 把变更文件按仓库相对路径物化到一个临时目录里再跑工具。
   好处有二：一是避免对全仓库做扫描（成本与仓库体积无关，符合 §10.1）；
   二是本地仓库与裸仓库缓存两种形态共用同一条执行路径。
3. **缺工具就降级，不报错。** 工具没装、没有配置文件、超时 —— 一律进 `skipped` / `timed_out`
   并写进 summary 的降级说明（文档 P7）。

拉进来的配置文件（`tsconfig.json` / eslint 配置 / `pyproject.toml` 等）也一并物化，
否则语言原生 linter 会因为找不到配置而整体报错，把"配置缺失"误报成"代码有问题"。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from acra.analysis.sarif import filter_diff_aware, parse_sarif
from acra.models import FileDiff, Finding, StaticFinding
from acra.repo.symbol_index import language_for_path

#: 需要一并物化的配置文件（存在才复制）
CONFIG_FILES: tuple[str, ...] = (
    "tsconfig.json",
    "tsconfig.base.json",
    "jsconfig.json",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    ".eslintrc",
    ".eslintrc.json",
    ".eslintrc.js",
    ".eslintrc.cjs",
    "pyproject.toml",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "setup.cfg",
    "checkstyle.xml",
    "config/checkstyle/checkstyle.xml",
)

LANGUAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "java": ("semgrep", "checkstyle"),
    "typescript": ("semgrep", "eslint", "tsc"),
    "python": ("semgrep", "ruff", "mypy"),
}


@dataclass(slots=True)
class ToolResult:
    tool: str
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out


@dataclass(slots=True)
class StaticRunReport:
    findings: list[StaticFinding] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    raw_count: int = 0
    dropped_by_cap: int = 0

    def summary_note(self) -> str:
        bits: list[str] = []
        if self.executed:
            bits.append("已执行 " + "、".join(self.executed))
        if self.skipped:
            bits.append("跳过 " + "、".join(self.skipped))
        if self.timed_out:
            bits.append("超时 " + "、".join(self.timed_out))
        return "；".join(bits)


# ---------------------------------------------------------------------------- 输出解析

_ESLINT_LEVEL = {2: "high", 1: "medium", 0: "low"}

#: `src/a.ts(3,5): error TS2322: message`
_PAREN_DIAG_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+)(?:,(?P<col>\d+))?\):\s*"
    r"(?P<level>error|warning|note|info)?\s*(?P<code>[A-Za-z]+\d+)?:?\s*(?P<msg>.+)$"
)
#: `src/a.py:3: error: message` / `[ERROR] src/a.java:3: message`
_COLON_DIAG_RE = re.compile(
    r"^(?:\[(?P<level0>ERROR|WARNING|INFO)\]\s*)?(?P<path>.+?):(?P<line>\d+):\s*"
    r"(?:(?P<level>error|warning|note|info):\s*)?(?P<msg>.+)$"
)


def parse_eslint_json(stdout: str, tool: str = "eslint") -> list[StaticFinding]:
    """ESLint `--format json` 的输出。"""
    if not stdout.strip():
        return []
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    findings: list[StaticFinding] = []
    for entry in payload if isinstance(payload, list) else []:
        path = str(entry.get("filePath") or "")
        for message in entry.get("messages") or []:
            rule_id = str(message.get("ruleId") or message.get("messageId") or "unknown")
            findings.append(
                StaticFinding(
                    tool=tool,
                    rule_id=rule_id,
                    severity=_ESLINT_LEVEL.get(int(message.get("severity") or 1), "medium"),
                    path=path,
                    line=int(message.get("line") or 0),
                    message=" ".join(str(message.get("message") or "").split())[:400],
                    level="error" if int(message.get("severity") or 1) == 2 else "warning",
                )
            )
    return findings


def parse_diagnostics(stdout: str, tool: str) -> list[StaticFinding]:
    """tsc / mypy / checkstyle 的行式诊断输出。"""
    findings: list[StaticFinding] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("Found ", "Success", "File ", "====")):
            continue
        match = _PAREN_DIAG_RE.match(line) or _COLON_DIAG_RE.match(line)
        if not match:
            continue
        groups = match.groupdict()
        path = groups.get("path") or ""
        if not path or "/" not in path and "\\" not in path:
            continue
        level = (groups.get("level") or groups.get("level0") or "warning").lower()
        severity = {"error": "high", "warning": "medium", "note": "low", "info": "low"}.get(
            level, "medium"
        )
        findings.append(
            StaticFinding(
                tool=tool,
                rule_id=str(groups.get("code") or tool),
                severity=severity,
                path=path.strip(),
                line=int(groups.get("line") or 0),
                message=" ".join(str(groups.get("msg") or "").split())[:400],
                level=level,
            )
        )
    return findings


def parse_sarif_output(stdout: str, tool: str) -> list[StaticFinding]:
    if not stdout.strip():
        return []
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError:
        # Semgrep 偶尔会在 SARIF 前打一行进度信息
        start = stdout.find("{")
        if start == -1:
            return []
        try:
            doc = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return []
    if not isinstance(doc, dict):
        return []
    return parse_sarif(doc, default_tool=tool)


# ---------------------------------------------------------------------------- 工具定义


@dataclass(slots=True)
class ToolSpec:
    name: str
    languages: tuple[str, ...]
    command: str
    argv: Callable[[Path, list[str], object], list[str]]
    parse: Callable[[str, str], list[StaticFinding]]
    #: 需要仓库里存在某个配置文件才会启用（返回 False 时进 skipped）
    needs_repo_file: str = ""
    #: 需要该环境变量（留空表示不需要）
    needs_env: str = ""


def _semgrep_argv(workdir: Path, _files: list[str], settings) -> list[str]:
    return [
        "semgrep",
        "scan",
        "--sarif",
        "--quiet",
        "--no-git-ignore",
        "--config",
        settings.acra_semgrep_config,
        str(workdir),
    ]


def _ruff_argv(workdir: Path, files: list[str], _settings) -> list[str]:
    return ["ruff", "check", "--output-format", "sarif", *files]


def _mypy_argv(workdir: Path, files: list[str], _settings) -> list[str]:
    return ["mypy", "--no-error-summary", "--show-column-numbers", *files]


def _eslint_argv(workdir: Path, files: list[str], _settings) -> list[str]:
    return ["eslint", "--format", "json", *files]


def _tsc_argv(workdir: Path, files: list[str], _settings) -> list[str]:
    return ["tsc", "--noEmit", "--pretty", "false", *files]


def _checkstyle_argv(workdir: Path, files: list[str], _settings) -> list[str]:
    config = workdir / "checkstyle.xml"
    return ["checkstyle", "-f", "plain", "-c", str(config), *files]


TOOLS: dict[str, ToolSpec] = {
    "semgrep": ToolSpec(
        name="semgrep",
        languages=(),
        command="semgrep",
        argv=_semgrep_argv,
        parse=parse_sarif_output,
    ),
    "ruff": ToolSpec(
        name="ruff",
        languages=("python",),
        command="ruff",
        argv=_ruff_argv,
        parse=parse_sarif_output,
    ),
    "mypy": ToolSpec(
        name="mypy",
        languages=("python",),
        command="mypy",
        argv=_mypy_argv,
        parse=parse_diagnostics,
    ),
    "eslint": ToolSpec(
        name="eslint",
        languages=("typescript",),
        command="eslint",
        argv=_eslint_argv,
        parse=parse_eslint_json,
    ),
    "tsc": ToolSpec(
        name="tsc",
        languages=("typescript",),
        command="tsc",
        argv=_tsc_argv,
        parse=parse_diagnostics,
    ),
    "checkstyle": ToolSpec(
        name="checkstyle",
        languages=("java",),
        command="checkstyle",
        argv=_checkstyle_argv,
        parse=parse_diagnostics,
        needs_repo_file="checkstyle.xml",
    ),
}


def tools_for(path: str) -> tuple[str, ...]:
    """某文件适用的工具：通用工具 + 该语言的工具。"""
    lang = language_for_path(path)
    specific = [t for t, spec in TOOLS.items() if lang and lang in spec.languages]
    generic = [t for t, spec in TOOLS.items() if not spec.languages]
    return tuple(dict.fromkeys([*generic, *specific]))


# ---------------------------------------------------------------------------- 执行


def resolve_command(name: str) -> str | None:
    """查找可执行文件：先查 PATH，再查**当前解释器所在环境的 bin/Scripts**。

    第二条很关键：工具常常是装在 acra 自己的 venv 里的（`pip install semgrep`），
    而进程被直接以 `.venv/Scripts/acra.exe` 方式拉起时，venv 的 Scripts 目录并不在
    PATH 上 —— 只查 PATH 会得出"工具未安装"的错误结论。
    """
    found = shutil.which(name)
    if found:
        return found

    import sysconfig

    bindir = sysconfig.get_path("scripts")
    if not bindir:
        return None
    base = Path(bindir)
    if not base.is_dir():
        return None
    for suffix in ("", ".exe", ".cmd", ".bat"):
        candidate = base / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    return None


def default_runner(argv: list[str], cwd: Path, timeout: int) -> ToolResult:
    """真实执行：子进程 + 超时。超时不抛异常，交给调用方决定降级。"""
    import subprocess

    tool = argv[0]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return ToolResult(tool=tool, returncode=127, stderr="executable not found")
    except subprocess.TimeoutExpired as exc:
        return ToolResult(
            tool=tool,
            returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"timeout after {timeout}s",
            timed_out=True,
        )
    except OSError as exc:
        return ToolResult(tool=tool, returncode=126, stderr=str(exc))
    return ToolResult(
        tool=tool,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _relativize_path(path: str, workdir: Path, known: list[str]) -> str | None:
    """把工具输出的路径映射回仓库相对路径。

    工具拿到的可能是临时目录的绝对路径、可能是相对 cwd 的路径、也可能只是文件名，
    Windows 上还带盘符且大小写不一。因此按"后缀匹配 + 唯一 basename"来判定，
    与 `postprocess.validator.normalize_path` 同一原则：**只做无歧义的归一化**，
    多义就放弃 —— 宁可漏掉一条静态提示，也不要把结论挂到错误的文件上。
    """
    if not path:
        return None
    cleaned = path.replace("\\", "/").lstrip("/")
    cleaned = re.sub(r"^[A-Za-z]:/", "", cleaned)

    # 1) 工具路径以仓库路径结尾（含绝对路径那种情况）
    for known_path in known:
        if cleaned == known_path or cleaned.endswith("/" + known_path):
            return known_path

    # 2) 按 workdir 相对化
    work = str(workdir).replace("\\", "/").lstrip("/")
    work = re.sub(r"^[A-Za-z]:/", "", work)
    if cleaned.startswith(work + "/"):
        candidate = cleaned[len(work) + 1 :]
        if candidate in known:
            return candidate

    # 3) 工具只报了部分路径：唯一的后缀才算数
    suffix_hits = [k for k in known if k.endswith("/" + cleaned)]
    if len(suffix_hits) == 1:
        return suffix_hits[0]

    # 4) 只报了文件名：唯一同名才算数
    base = cleaned.rsplit("/", 1)[-1]
    base_hits = [k for k in known if k.rsplit("/", 1)[-1] == base]
    if len(base_hits) == 1:
        return base_hits[0]
    return None


def _materialize(workdir: Path, files: list[FileDiff], read_file: Callable[[str], str | None]) -> list[str]:
    """把变更文件写到临时目录，返回成功写入的相对路径列表。"""
    written: list[str] = []
    for file_diff in files:
        if file_diff.is_binary:
            continue
        content = read_file(file_diff.path)
        if not content:
            continue
        target = workdir / file_diff.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(file_diff.path)
    for config in CONFIG_FILES:
        content = read_file(config)
        if not content:
            continue
        target = workdir / config
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return written


def _select_tools(
    files: list[FileDiff],
    settings,
    linters: list[str] | None,
) -> tuple[list[str], list[str]]:
    """选出要跑的工具，以及被跳过的原因。"""
    langs = {language_for_path(f.path) for f in files}
    available: list[str] = []
    for lang in sorted(filter(None, langs)):
        for tool in LANGUAGE_TOOLS.get(lang, ()):
            if tool not in available:
                available.append(tool)

    if linters:
        requested = [t for t in linters if t in TOOLS]
        unknown = [t for t in linters if t not in TOOLS]
        skipped = [f"unknown_linter:{t}" for t in unknown]
        available = [t for t in available if t in requested]
    else:
        # 空列表与 None 同义：不做限制，按语言自动选工具。
        # 把"空"当成"什么都不跑"是个很容易踩的坑 —— 配置里留空本来是"用默认"的意思。
        skipped = []

    if not settings.acra_semgrep_config and "semgrep" in available:
        available.remove("semgrep")
        skipped.append("semgrep:未配置规则集")
    return available, skipped


async def run_static_analysis(
    files: list[FileDiff],
    settings,
    *,
    whitelist: dict[str, set[int]] | None = None,
    read_file: Callable[[str], str | None] | None = None,
    runner: Callable[[list[str], Path, int], ToolResult] | None = None,
    linters: list[str] | None = None,
    ignored_rules: Iterable[str] = (),
) -> StaticRunReport:
    """跑外部 linter，归一化后按变更行过滤。

    返回的 `findings` 已经是 diff-aware 过滤后的结果 —— 存量代码的历史告警不进入
    LLM 上下文（文档 §4.5「重要」）。
    """
    report = StaticRunReport()
    if not settings.acra_static_analysis_enabled:
        report.skipped.append("static_analysis_disabled")
        return report
    if read_file is None:
        report.skipped.append("no_repo_source_reader")
        return report
    if not files:
        report.skipped.append("no_changed_files")
        return report

    selected, skipped = _select_tools(files, settings, linters)
    report.skipped.extend(skipped)

    run = runner or default_runner
    timeout = settings.acra_static_timeout_seconds
    known = [f.path for f in files]

    with tempfile.TemporaryDirectory(prefix="acra-static-") as tmp:
        workdir = Path(tmp)
        written = _materialize(workdir, files, read_file)
        if not written:
            report.skipped.append("nothing_to_scan")
            return report

        present = set(known) | {c for c in CONFIG_FILES if (workdir / c).exists()}
        runnable: list[tuple[str, str]] = []
        for tool in selected:
            # 必须用解析出来的**绝对路径**去执行，不能只把它用在判断上：
            # 工具很可能装在 acra 自己的 venv 里而不在 PATH 上，argv[0] 用裸名字会
            # 直接 FileNotFoundError，然后被误报成"工具未安装"。
            executable = resolve_command(TOOLS[tool].command)
            if not executable:
                report.skipped.append(f"{tool}:未安装")
                continue
            required = TOOLS[tool].needs_repo_file
            if required and required not in present:
                report.skipped.append(f"{tool}:缺少配置文件 {required}")
                continue
            runnable.append((tool, executable))

        # 并发跑：各工具互不依赖，串行会把静态分析拖成整条链路的瓶颈
        tasks = []
        for tool, executable in runnable:
            argv = list(TOOLS[tool].argv(workdir, written, settings))
            argv[0] = executable
            tasks.append(asyncio.to_thread(run, argv, workdir, timeout))
        results = await asyncio.gather(*tasks) if tasks else []

        collected: list[StaticFinding] = []
        for (tool, _executable), result in zip(runnable, results, strict=True):
            if result.timed_out:
                report.timed_out.append(tool)
                continue
            if result.returncode == 127:
                report.skipped.append(f"{tool}:无法启动（{result.stderr.strip() or 'exec failed'}）")
                continue
            report.executed.append(tool)
            collected.extend(TOOLS[tool].parse(result.stdout, tool))

        # 路径归一化必须在临时目录还在的时候做（工具输出是它的绝对路径）
        normalized: list[StaticFinding] = []
        for finding in collected:
            mapped = _relativize_path(finding.path, workdir, known)
            if mapped is None:
                continue
            finding.path = mapped
            normalized.append(finding)

    report.raw_count = len(normalized)
    if whitelist is not None:
        normalized = filter_diff_aware(normalized, whitelist, ignored_rules=ignored_rules)
    normalized = _dedupe(normalized)

    cap = max(0, settings.acra_static_max_findings)
    if len(normalized) > cap:
        report.dropped_by_cap = len(normalized) - cap
        normalized = normalized[:cap]

    report.findings = normalized
    return report


def _dedupe(findings: list[StaticFinding]) -> list[StaticFinding]:
    seen: set[tuple[str, str, int, str]] = set()
    out: list[StaticFinding] = []
    for finding in findings:
        key = (finding.path, finding.rule_id, finding.line, finding.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


# ---------------------------------------------------------------------------- 转成 Finding

#: 规则 ID / 工具名中的关键词 → acra 的类别。
#: 关键词匹配不完美，但比"全部标成 bug"诚实：linter 的绝大多数输出属于可维护性，
#: 只有安全类才需要被单独摘出来。
#:
#: 注意：这组关键词是**回退路径**，只在规则 ID 不带命名空间时使用。
#: 实测踩过：`java.lang.security.audit.formatted-sql-string.formatted-sql-string`
#: 里 `formatted` 含 `format`，被 `_STYLE_HINTS` 抢先命中 ——
#: **一条 SQL 注入被归成了风格问题**。所以先读命名空间（见 `classify_rule`）。
_SECURITY_HINTS = (
    "sqli",
    "sql-injection",
    "injection",
    "xss",
    "csrf",
    "ssrf",
    "secret",
    "crypto",
    "insecure",
    "auth",
    "permission",
    "vulnerab",
    "deserial",
    "hardcoded",
    "traversal",
)
_BUG_HINTS = (
    "null",
    "npe",
    "undefined",
    "unreachable",
    "overflow",
    "index-out-of",
    "arithmetic",
    "unused-result",
    "f821",
    "ts2322",
    "ts2345",
    "ts2531",
    "ts2532",
    "no-undef",
    "no-unused-expressions",
    "eqeqeq",
    "no-fallthrough",
)
_STYLE_HINTS = (
    "style",
    # 刻意不写裸 `format`：它会命中 `formatted-sql-string` 这类安全规则名。
    # 风格规则的真实名字长这样：`line-too-long`、`trailing-whitespace`、`indent`。
    "formatting",
    "naming",
    "line-length",
    "line_too_long",
    "indent",
    "whitespace",
    "trailing",
    "quote",
    "blank",
    "e1",
    "e2",
    "e3",
    "e5",
    "w2",
    "w3",
)

#: Semgrep 的规则 ID 是**带命名空间**的：`<lang>.lang.<category>.<...>`，
#: 其中 `<category>` 就是 Semgrep 自己给出的权威分类。
#: 有权威分类可用时不该再去规则名里猜关键词 —— 猜错会把整条结论的类别弄反。
_SEMGREP_NAMESPACE_CATEGORIES: dict[str, str] = {
    "security": "security",
    "correctness": "bug",
    "best-practice": "maintainability",
    "maintainability": "maintainability",
    "performance": "performance",
    "portability": "maintainability",
    "compatibility": "maintainability",
}

#: 静态结论的置信度：工具"报了什么"很少错，不确定的是"这件事有多重要"。
#: 因此 error 级给 0.8、warning 级给 0.65、note 级给 0.5（note 会被默认门槛过滤掉）。
_CONFIDENCE_BY_SEVERITY = {"blocker": 0.85, "high": 0.80, "medium": 0.65, "low": 0.50, "nit": 0.40}


def classify_rule(tool: str, rule_id: str) -> str:
    """把规则 ID 归到 acra 的某个类别。

    两级判定，**顺序不能反**：

    1. **命名空间段**（Semgrep）：`java.lang.security.audit.formatted-sql-string`
       里的 `security` 就是权威答案；
    2. **关键词回退**（ruff / eslint / tsc 这类 ID 不带类别命名空间，如 `F401`、`no-undef`）。

    反过来的代价是实测过的：`formatted-sql-string` 先被 `format` 这个风格关键词命中，
    于是一条 SQL 注入被标成 `style` —— 而评测口径按"类别同族"判命中，
    结果是**这条结论既不算命中、又被记成一次误报**，静态层看起来完全不可用。
    """
    for segment in rule_id.lower().split("."):
        mapped = _SEMGREP_NAMESPACE_CATEGORIES.get(segment)
        if mapped:
            return mapped

    haystack = f"{tool}.{rule_id}".lower()
    if any(hint in haystack for hint in _SECURITY_HINTS):
        return "security"
    if any(hint in haystack for hint in _BUG_HINTS):
        return "bug"
    if any(hint in haystack for hint in _STYLE_HINTS):
        return "style"
    return "maintainability"


def to_findings(
    findings: Iterable[StaticFinding],
    *,
    confidence_by_severity: dict[str, float] | None = None,
) -> list[Finding]:
    """把静态结论转成可直接输出的 Finding。

    用途是**降级模式**（`--no-llm` 或 LLM 不可用）：文档 §4.2 承诺这种情况下
    "仅展示静态检查结果"，那就必须真的有东西可展示。这类 Finding 会被标记
    `source="static"`，与模型判断的结论在输出里明确区分开。
    """
    table = confidence_by_severity or _CONFIDENCE_BY_SEVERITY
    out: list[Finding] = []
    for static in findings:
        category = classify_rule(static.tool, static.rule_id)
        title = f"[{static.tool}] {static.message[:60]}".strip()
        body = (
            f"静态检查（{static.tool}:{static.rule_id}）在本次变更行上命中：\n"
            f"{static.message}\n\n"
            "该结论由静态分析工具直接给出，未经模型判断，请结合上下文确认。"
        )
        out.append(
            Finding(
                path=static.path,
                line=static.line,
                category=category,
                severity=static.severity if static.severity in table else "medium",
                confidence=table.get(static.severity, 0.65),
                score=0.0,  # 由 ranker 统一评分
                title=title,
                body=body,
                evidence=[f"{static.tool}:{static.rule_id}"],
                evidence_rule_ids=[static.rule_id],
                source="static",
                tool=static.tool,
            )
        )
    return out
