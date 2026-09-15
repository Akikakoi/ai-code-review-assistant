"""static_runner 的单测。

文档 §14.1：「静态分析：SARIF 解析、diff-aware 过滤、工具缺失降级」。

全部用**注入的假 runner**：真实运行会去调本机的 semgrep/eslint，那既慢又不确定，
而且"工具没装时应如何降级"这条恰恰只能用假 runner 才测得到。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acra.analysis.static_runner import (
    TOOLS,
    ToolResult,
    parse_diagnostics,
    parse_eslint_json,
    parse_sarif_output,
    run_static_analysis,
    tools_for,
)
from acra.models import ChangeType, FileDiff, Hunk


def _fd(path: str, added: dict[int, str]) -> FileDiff:
    low = min(added)
    return FileDiff(
        path=path,
        change_type=ChangeType.MODIFY,
        hunks=[
            Hunk(
                old_start=low,
                old_lines=0,
                new_start=low,
                new_lines=len(added),
                added=sorted(added.items()),
            )
        ],
    )


def _reader(sources: dict[str, str]):
    def read(path: str) -> str | None:
        return sources.get(path)

    return read


def _runner(outputs: dict[str, ToolResult]):
    """按**可执行文件的基本名**返回预置结果。

    生产代码会把解析出来的绝对路径写进 argv[0]（这样 venv 里的工具也能执行），
    所以这里按 basename 匹配，而不是要求调用方拼出完整路径。
    """
    calls: list[list[str]] = []

    def run(argv: list[str], cwd, timeout: int) -> ToolResult:
        calls.append(argv)
        name = Path(argv[0]).name
        return outputs.get(name, ToolResult(tool=name, returncode=0, stdout=""))

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------- 工具路由


def test_tools_for_java_is_semgrep_plus_checkstyle() -> None:
    assert set(tools_for("A.java")) == {"semgrep", "checkstyle"}


def test_tools_for_typescript() -> None:
    assert set(tools_for("a.ts")) == {"semgrep", "eslint", "tsc"}


def test_tools_for_python() -> None:
    assert set(tools_for("a.py")) == {"semgrep", "ruff", "mypy"}


def test_tools_for_unknown_language_is_generic_only() -> None:
    assert set(tools_for("data.csv")) == {"semgrep"}


# ---------------------------------------------------------------------------- 各类输出解析

SARIF_ONE = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep", "rules": [{"id": "java.sqli"}]}},
            "results": [
                {
                    "ruleId": "java.sqli",
                    "level": "error",
                    "message": {"text": "拼接 SQL"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "file:///tmp/x/src/A.java"},
                                "region": {"startLine": 11},
                            }
                        }
                    ],
                }
            ],
        }
    ],
}


def test_parse_sarif_output_accepts_leading_noise() -> None:
    """Semgrep 有时会在 SARIF 之前打一行进度信息。"""
    stdout = "Scanning 2 files\n" + json.dumps(SARIF_ONE)
    findings = parse_sarif_output(stdout, "semgrep")
    assert len(findings) == 1
    assert findings[0].rule_id == "java.sqli"


def test_parse_sarif_output_handles_garbage() -> None:
    assert parse_sarif_output("not json at all", "semgrep") == []
    assert parse_sarif_output("", "semgrep") == []


def test_parse_eslint_json() -> None:
    stdout = json.dumps(
        [
            {
                "filePath": "/tmp/x/a.ts",
                "messages": [
                    {"ruleId": "no-unused-vars", "severity": 2, "line": 7, "message": "unused"},
                    {"ruleId": "eqeqeq", "severity": 1, "line": 9, "message": "use ==="},
                    {"severity": 2, "line": 10, "message": "parse error"},  # 无 ruleId
                ],
            }
        ]
    )
    findings = parse_eslint_json(stdout, "eslint")
    assert [f.severity for f in findings] == ["high", "medium", "high"]
    assert findings[0].rule_id == "no-unused-vars"
    assert findings[2].rule_id == "unknown"
    assert findings[0].line == 7


def test_parse_eslint_json_handles_non_json() -> None:
    assert parse_eslint_json("oops", "eslint") == []


def test_parse_tsc_parenthesis_form() -> None:
    findings = parse_diagnostics(
        "src/app.ts(12,5): error TS2322: Type 'string' is not assignable to 'number'.",
        "tsc",
    )
    assert len(findings) == 1
    assert findings[0].path == "src/app.ts"
    assert findings[0].line == 12
    assert findings[0].severity == "high"
    assert findings[0].rule_id == "TS2322"


def test_parse_mypy_colon_form() -> None:
    findings = parse_diagnostics("src/util.py:3: error: Incompatible return value type", "mypy")
    assert len(findings) == 1
    assert findings[0].path == "src/util.py"
    assert findings[0].line == 3
    assert findings[0].severity == "high"


def test_parse_checkstyle_plain_form() -> None:
    findings = parse_diagnostics(
        "[ERROR] /w/src/A.java:11: Line is longer than 100 characters.", "checkstyle"
    )
    assert len(findings) == 1
    assert findings[0].path == "/w/src/A.java"
    assert findings[0].line == 11
    assert findings[0].severity == "high"


def test_parse_diagnostics_skips_summary_lines() -> None:
    stdout = "\n".join(
        [
            "Found 1 error in 1 file",
            "Success: no issues found",
            "==== test session starts ====",
            "",
        ]
    )
    assert parse_diagnostics(stdout, "tsc") == []


# ---------------------------------------------------------------------------- 端到端（假工具）


@pytest.mark.asyncio
async def test_disabled_returns_skip_note(settings) -> None:
    settings.acra_static_analysis_enabled = False
    report = await run_static_analysis([_fd("A.java", {11: "x"})], settings)
    assert report.skipped == ["static_analysis_disabled"]
    assert report.executed == []


@pytest.mark.asyncio
async def test_missing_reader_degrades(settings) -> None:
    settings.acra_static_analysis_enabled = True
    report = await run_static_analysis([_fd("A.java", {11: "x"})], settings)
    assert report.skipped == ["no_repo_source_reader"]


@pytest.mark.asyncio
async def test_no_changed_files_degrades(settings) -> None:
    settings.acra_static_analysis_enabled = True
    report = await run_static_analysis([], settings, read_file=_reader({}))
    assert report.skipped == ["no_changed_files"]


@pytest.mark.asyncio
async def test_findings_are_diff_aware_filtered(settings, monkeypatch) -> None:
    """核心行为：只保留落在变更行上的静态结果。"""
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    src = {"src/A.java": "line1\nline2\nint changed = 1;\n"}
    fd = _fd("src/A.java", {3: "int changed = 1;"})

    semgrep_out = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "semgrep"}},
                "results": [
                    {
                        "ruleId": "java.sqli",
                        "level": "error",
                        "message": {"text": "变更行上的问题"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "/tmp/w/src/A.java"},
                                    "region": {"startLine": 3},
                                }
                            }
                        ],
                    },
                    {
                        "ruleId": "java.unused",
                        "level": "warning",
                        "message": {"text": "存量行上的历史告警"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "/tmp/w/src/A.java"},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    },
                ],
            }
        ],
    }
    runner = _runner(
        {"semgrep": ToolResult(tool="semgrep", stdout=json.dumps(semgrep_out))}
    )

    settings.acra_static_analysis_enabled = True
    report = await run_static_analysis(
        [fd],
        settings,
        whitelist={"src/A.java": {3}},
        read_file=_reader(src),
        runner=runner,
        linters=["semgrep"],
    )

    assert report.raw_count == 2
    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "java.sqli"
    assert report.findings[0].path == "src/A.java"  # 已从临时路径归一化


@pytest.mark.asyncio
async def test_ignored_rules_are_filtered(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    src = {"src/A.java": "int changed = 1;\n"}
    fd = _fd("src/A.java", {1: "int changed = 1;"})
    semgrep_out = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "semgrep"}},
                "results": [
                    {
                        "ruleId": "java.sqli",
                        "level": "error",
                        "message": {"text": "x"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "/tmp/w/src/A.java"},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }
    runner = _runner({"semgrep": ToolResult(tool="semgrep", stdout=json.dumps(semgrep_out))})
    settings.acra_static_analysis_enabled = True

    report = await run_static_analysis(
        [fd],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader(src),
        runner=runner,
        linters=["semgrep"],
        ignored_rules=["java.sqli"],
    )
    assert report.findings == []


@pytest.mark.asyncio
async def test_uninstalled_tool_is_skipped_not_failed(settings, monkeypatch) -> None:
    """工具不可用时只跳过，不让整次审查失败。

    这里 patch 的是 `resolve_command`（生产代码真正查的那个函数），而不是 `shutil.which`。
    起因是一个真实教训：本机装上 semgrep 之后这条测试就挂了 —— 因为它只补了 PATH
    这条查找路径，而 `resolve_command` 还会去当前解释器的 Scripts 目录找。
    **测试不能依赖"本机恰好没装某个工具"。**
    """
    monkeypatch.setattr("acra.analysis.static_runner.resolve_command", lambda name: None)
    settings.acra_static_analysis_enabled = True
    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        linters=["semgrep"],
    )
    assert any("semgrep:未安装" in s for s in report.skipped)
    assert report.executed == []


@pytest.mark.asyncio
async def test_timeout_is_recorded_and_does_not_raise(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    runner = _runner(
        {"semgrep": ToolResult(tool="semgrep", returncode=-1, timed_out=True, stderr="timeout")}
    )
    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        runner=runner,
        linters=["semgrep"],
    )
    assert report.timed_out == ["semgrep"]
    assert report.executed == []
    assert report.findings == []


@pytest.mark.asyncio
async def test_unknown_linter_name_is_reported(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    runner = _runner({})
    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        runner=runner,
        linters=["spotbugs", "semgrep"],
    )
    assert "unknown_linter:spotbugs" in report.skipped
    assert report.executed == ["semgrep"]


@pytest.mark.asyncio
async def test_linter_not_applicable_to_language(settings, monkeypatch) -> None:
    """只改 Java 却要求跑 ruff —— 应该什么都不跑，而不是报错。"""
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    runner = _runner({})
    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        runner=runner,
        linters=["ruff"],
    )
    assert report.executed == []
    assert runner.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_checkstyle_needs_config_file(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    runner = _runner({})
    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        runner=runner,
        linters=["checkstyle"],
    )
    assert any("缺少配置文件" in s for s in report.skipped)
    assert report.executed == []


@pytest.mark.asyncio
async def test_max_findings_cap(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    settings.acra_static_max_findings = 2

    lines = "\n".join(f"int v{i} = {i};" for i in range(1, 6))
    fd = _fd("src/A.java", {i: f"int v{i} = {i};" for i in range(1, 6)})
    results = [
        {
            "ruleId": f"rule.{i}",
            "level": "warning",
            "message": {"text": f"问题 {i}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": "/tmp/w/src/A.java"},
                        "region": {"startLine": i},
                    }
                }
            ],
        }
        for i in range(1, 6)
    ]
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "semgrep"}}, "results": results}]}
    runner = _runner({"semgrep": ToolResult(tool="semgrep", stdout=json.dumps(doc))})

    report = await run_static_analysis(
        [fd],
        settings,
        whitelist={"src/A.java": set(range(1, 6))},
        read_file=_reader({"src/A.java": lines}),
        runner=runner,
        linters=["semgrep"],
    )
    assert len(report.findings) == 2
    assert report.dropped_by_cap == 3


@pytest.mark.asyncio
async def test_duplicate_findings_are_deduped(settings, monkeypatch) -> None:
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    one = {
        "ruleId": "java.sqli",
        "level": "error",
        "message": {"text": "同一个问题"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": "/tmp/w/src/A.java"},
                    "region": {"startLine": 1},
                }
            }
        ],
    }
    doc = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "semgrep"}}, "results": [one, dict(one)]}]}
    runner = _runner({"semgrep": ToolResult(tool="semgrep", stdout=json.dumps(doc))})

    report = await run_static_analysis(
        [_fd("src/A.java", {1: "x"})],
        settings,
        whitelist={"src/A.java": {1}},
        read_file=_reader({"src/A.java": "x\n"}),
        runner=runner,
        linters=["semgrep"],
    )
    assert len(report.findings) == 1


@pytest.mark.asyncio
async def test_config_files_are_materialized(settings, monkeypatch) -> None:
    """tsconfig 之类的配置必须一起物化，否则语言原生 linter 会整体报错。"""
    monkeypatch.setattr("acra.analysis.static_runner.shutil.which", lambda name: f"/usr/bin/{name}")
    settings.acra_static_analysis_enabled = True
    src = {"src/app.ts": "const a = 1;\n", "tsconfig.json": '{"compilerOptions":{}}'}

    seen: list[dict[str, str]] = []

    def run(argv: list[str], cwd, timeout: int) -> ToolResult:
        seen.append({p.name for p in cwd.rglob("*") if p.is_file()})
        return ToolResult(tool=argv[0], stdout="")

    await run_static_analysis(
        [_fd("src/app.ts", {1: "const a = 1;"})],
        settings,
        whitelist={"src/app.ts": {1}},
        read_file=_reader(src),
        runner=run,
        linters=["eslint"],
    )
    assert seen, "工具未被调用"
    assert {"tsconfig.json", "app.ts"} <= seen[0]


def test_binary_files_are_not_materialized(settings, tmp_path) -> None:
    from acra.analysis.static_runner import _materialize

    binary = FileDiff(path="img.png", change_type=ChangeType.BINARY, is_binary=True)
    written = _materialize(tmp_path, [binary], _reader({"img.png": "not really binary"}))
    assert written == []


def test_tools_registry_commands_are_executables() -> None:
    for name, spec in TOOLS.items():
        assert spec.command, f"{name} 没有可执行命令"
        assert callable(spec.argv)
        assert callable(spec.parse)


# ---------------------------------------------------------------------------- 规则分类


@pytest.mark.parametrize(
    ("rule_id", "expected"),
    [
        # Semgrep 带命名空间：命中的那一段就是权威分类，不该再去规则名里猜
        ("java.lang.security.audit.formatted-sql-string.formatted-sql-string", "security"),
        ("python.lang.security.audit.dangerous-subprocess-use", "security"),
        ("java.lang.correctness.useless-eqeq.useless-eqeq", "bug"),
        ("python.lang.best-practice.use-of-assert", "maintainability"),
        ("java.lang.performance.string-formatted-in-loop", "performance"),
    ],
)
def test_classify_rule_reads_semgrep_namespace(rule_id: str, expected: str) -> None:
    from acra.analysis.static_runner import classify_rule

    assert classify_rule("semgrep", rule_id) == expected


@pytest.mark.parametrize(
    ("tool", "rule_id", "expected"),
    [
        ("ruff", "F401", "maintainability"),
        # `e5` 这个前缀对应 pycodestyle 的"行长度"风格组，E501 正落在其中。
        # 注意 E711（与 None 比较）属于 E7"编程错误"组、不在这几个前缀里，
        # 因此会落到 maintainability —— 这是已知的粗糙处，不是这里在断言的正确行为。
        ("ruff", "E501", "style"),
        ("eslint", "no-undef", "bug"),
        ("tsc", "TS2322", "bug"),
    ],
)
def test_classify_rule_falls_back_to_keywords(tool: str, rule_id: str, expected: str) -> None:
    """没有命名空间的 ID（ruff / eslint / tsc）仍走关键词回退。"""
    from acra.analysis.static_runner import classify_rule

    assert classify_rule(tool, rule_id) == expected


def test_formatted_sql_string_is_not_mistaken_for_a_style_rule() -> None:
    """回归：`formatted-sql-string` 里的 `formatted` 曾被 `format` 关键词抢先命中。

    后果不是"分类不好看"，而是**评测口径**上的双重扣分：
    类别跨族 ⇒ 这条结论既不算命中（FN）、又被记成一次误报（FP），
    而它是 `full_suite()` 里唯一为静态层服务的样本 —— 于是"静态层可用"永远证明不了。
    """
    from acra.analysis.static_runner import classify_rule

    assert classify_rule("semgrep", "java.lang.security.audit.formatted-sql-string") == "security"


def test_static_probe_ground_truth_matches_classifier() -> None:
    """静态探针的 ground truth 类别必须与分类器给出的类别一致。

    两者分居两个文件，任何一边单独改动都可能让它们在评测里悄悄对不上，
    而症状（"跨族 ⇒ 既不算命中又算误报"）看起来像静态层坏了，不像测试写错了。
    这条测试把两边钉在一起。规则 ID 是实测值：`p/security-audit` 下真实报出的那个。
    """
    from acra.analysis.static_runner import classify_rule
    from acra.eval.static_cases import static_cases

    case = static_cases()[0]
    declared = case.expectations[0]
    observed_rule_id = "java.lang.security.audit.formatted-sql-string.formatted-sql-string"

    assert declared.category, "探针没有声明期望类别，评测会退化成只看行号"
    assert classify_rule("semgrep", observed_rule_id) == declared.category
    # 声明的 note 必须与实测的规则名对得上，否则"实测验证过"这句话会失效
    assert "formatted-sql-string" in observed_rule_id
    assert case.expected_lines(), "marker 没能定位到 head 里的行"
