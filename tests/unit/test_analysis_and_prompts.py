"""高风险判定、低价值路径过滤、预算守卫、SARIF 解析与提示词装配的单测。"""

from __future__ import annotations

import json

import pytest

from acra.analysis.risk_rules import (
    apply_budget_guard,
    is_doc_only,
    is_low_value_path,
    l3_triggers,
    looks_high_risk,
)
from acra.analysis.sarif import filter_diff_aware, parse_sarif, to_sarif
from acra.engine.prompt_loader import (
    UNTRUSTED_CLOSE,
    available_versions,
    build_scan_messages,
    load_template,
    repo_content_block,
    wrap_untrusted,
)
from acra.models import (
    Chunk,
    DiffSet,
    FileContext,
    Finding,
    StaticFinding,
)

# ---------------------------------------------------------------------------- 路径过滤


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/guide.rst",
        "package-lock.json",
        "pnpm-lock.yaml",
        "web/dist/app.min.js",
        "src/api/generated.pb.go",
        "src/types/index.d.ts",
        "node_modules/lib/index.js",
        "frontend/.next/build-manifest.json",
        "assets/logo.png",
    ],
)
def test_low_value_paths_are_skipped(path: str) -> None:
    assert is_low_value_path(path) is True


@pytest.mark.parametrize(
    "path",
    ["src/main/java/A.java", "src/app.ts", "lib/util.py", "go.mod", "pom.xml"],
)
def test_source_paths_are_not_skipped(path: str) -> None:
    assert is_low_value_path(path) is False


def test_is_doc_only() -> None:
    assert is_doc_only(["a.md", "b/images/x.png"]) is True
    assert is_doc_only(["a.md", "b.ts"]) is False
    assert is_doc_only([]) is False


# ---------------------------------------------------------------------------- 高风险


def _fd(path: str, added: dict[int, str]):
    from acra.models import ChangeType, FileDiff, Hunk

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


def test_high_risk_by_path_and_content() -> None:
    assert looks_high_risk(_fd("src/OrderService.java", {1: "x"})) is True
    assert looks_high_risk(_fd("src/util.py", {1: "x"})) is False
    assert looks_high_risk(_fd("src/util.py", {1: 'String sql = "select * from t";'})) is True
    assert looks_high_risk(_fd("src/util.py", {1: "synchronized (lock) {"})) is True
    assert looks_high_risk(_fd("src/util.py", {1: "@Transactional"})) is True


def test_l3_triggers() -> None:
    assert "concurrency_primitive" in l3_triggers(_fd("A.java", {1: "AtomicInteger c = new AtomicInteger();"}))
    assert "transaction_annotation" in l3_triggers(_fd("A.java", {1: "@Transactional"}))
    assert "sql_touched" in l3_triggers(_fd("A.java", {1: 'jdbc.query("select 1")'}))
    assert "permission_logic" in l3_triggers(_fd("A.java", {1: "@PreAuthorize(\"hasRole('ADMIN')\")"}))
    assert l3_triggers(_fd("A.java", {1: "int x = 1;"})) == []


# ---------------------------------------------------------------------------- 预算守卫


def _diff_set(*files) -> DiffSet:
    return DiffSet(base_sha="b", head_sha="h", merge_base_sha="m", files=list(files))


def test_guard_skips_low_value_files(settings) -> None:
    result = apply_budget_guard(
        _diff_set(_fd("README.md", {1: "doc"}), _fd("src/A.java", {1: "int x;"})), settings
    )
    assert [f.path for f in result.diff_set.files] == ["src/A.java"]
    # 跳过文档类文件是信息性说明，不是降级 —— 否则每次运行都会被标记成 degraded
    assert any("跳过低价值文件" in n for n in result.info)
    assert result.notes == []
    assert result.truncated is False


def test_guard_skips_files_with_no_commentable_lines(settings) -> None:
    from acra.models import ChangeType, FileDiff

    delete_only = FileDiff(path="src/Gone.java", change_type=ChangeType.DELETE, hunks=[])
    result = apply_budget_guard(_diff_set(delete_only, _fd("src/A.java", {1: "x"})), settings)
    assert [f.path for f in result.diff_set.files] == ["src/A.java"]


def test_guard_respects_ignored_paths(settings) -> None:
    result = apply_budget_guard(
        _diff_set(_fd("src/A.java", {1: "x"}), _fd("src/gen/B.java", {1: "y"})),
        settings,
        ignore_paths=["**/gen/**"],
    )
    assert [f.path for f in result.diff_set.files] == ["src/A.java"]


def test_guard_truncates_to_max_files_keeping_high_risk(settings) -> None:
    settings.acra_max_changed_files = 2
    files = [
        _fd("src/util/Helper.java", {1: "int x;"}),
        _fd("src/OrderService.java", {1: "int y;"}),
        _fd("src/PaymentService.java", {1: "int z;"}),
    ]
    result = apply_budget_guard(_diff_set(*files), settings)
    assert len(result.diff_set.files) == 2
    assert "src/util/Helper.java" not in {f.path for f in result.diff_set.files}
    assert result.truncated is True


def test_guard_degrades_to_high_risk_only_when_lines_exceeded(settings) -> None:
    settings.acra_max_changed_lines = 5
    files = [_fd("src/Helper.java", {i: "int x;" for i in range(1, 20)})]
    result = apply_budget_guard(_diff_set(*files), settings)
    assert result.diff_set.files == []
    assert any("只审高风险文件" in n for n in result.notes)


# ---------------------------------------------------------------------------- SARIF


SARIF_DOC = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep", "rules": [{"id": "java.sqli"}]}},
            "results": [
                {
                    "ruleId": "java.sqli",
                    "level": "error",
                    "message": {"text": "字符串拼接构造 SQL"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/main/java/A.java"},
                                "region": {"startLine": 142},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "java.unused",
                    "level": "warning",
                    "message": {"text": "未使用的变量"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "./src/Other.java"},
                                "region": {"startLine": 9},
                            }
                        }
                    ],
                },
                {"ruleId": "no.location", "message": {"text": "无位置信息"}},
            ],
        }
    ],
}


def test_parse_sarif_normalizes_fields() -> None:
    findings = parse_sarif(SARIF_DOC)
    assert len(findings) == 2  # 无位置的被跳过
    first = findings[0]
    assert (first.tool, first.rule_id, first.path, first.line) == (
        "semgrep",
        "java.sqli",
        "src/main/java/A.java",
        142,
    )
    assert first.severity == "high"  # level=error → high
    assert findings[1].path == "src/Other.java"  # ./ 前缀被清理


def test_filter_diff_aware_only_keeps_changed_lines() -> None:
    findings = parse_sarif(SARIF_DOC)
    whitelist = {"src/main/java/A.java": {142}}
    kept = filter_diff_aware(findings, whitelist)
    assert [f.rule_id for f in kept] == ["java.sqli"]


def test_filter_diff_aware_respects_ignored_rules() -> None:
    findings = parse_sarif(SARIF_DOC)
    whitelist = {"src/main/java/A.java": {142}}
    assert filter_diff_aware(findings, whitelist, ignored_rules=["java.sqli"]) == []


def test_filter_diff_aware_drops_paths_outside_diff() -> None:
    findings = parse_sarif(SARIF_DOC)
    assert filter_diff_aware(findings, {"other/File.java": {1}}) == []


def test_to_sarif_round_trip() -> None:
    finding = Finding(
        path="src/A.java",
        line=11,
        category="bug",
        severity="high",
        confidence=0.8,
        score=0.9,
        title="空值校验被取反",
        body="现象→影响",
    )
    doc = to_sarif([finding])
    assert doc["version"] == "2.1.0"
    result = doc["runs"][0]["results"][0]
    assert result["level"] == "error"
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] == 11
    assert json.dumps(doc)  # 可序列化


# ---------------------------------------------------------------------------- 提示词


def test_prompt_versions_resolvable() -> None:
    assert "1" in available_versions()
    for name in ("system", "scan", "verify", "summary"):
        assert load_template(name) is not None


def test_missing_prompt_raises() -> None:
    from acra.errors import PromptNotFound

    with pytest.raises(PromptNotFound):
        load_template("nonexistent", version="99")


def test_wrap_untrusted_uses_non_code_delimiters_with_content_hash() -> None:
    wrapped = wrap_untrusted("class A { }")
    assert wrapped.startswith("<<<UNTRUSTED_REPO_CONTENT id=")
    assert wrapped.endswith(UNTRUSTED_CLOSE)
    assert wrap_untrusted("a") != wrap_untrusted("b")


def test_scan_messages_isolate_repo_content() -> None:
    """注入防护：仓库内容必须被包在定界符内，且系统提示声明其不可信。"""
    ctx = FileContext(
        path="A.java",
        level=2,
        diff_text="   11|+int x = 1;",
        enclosing_source_text="   10| void pay()",
        imports=["import java.util.List;"],
        static_text="(无)",
    )
    chunk = Chunk(
        index=1,
        total=2,
        path="A.java",
        change_type="modify",
        added_line_numbers={11},
        diff_text=ctx.diff_text,
        context=ctx,
    )
    messages = build_scan_messages(chunk, custom_conventions="禁止在 Controller 里写业务逻辑")
    system, user = messages[0]["content"], messages[1]["content"]

    assert "1/2" in user
    assert "A.java" in user
    assert "11" in user
    assert "<<<UNTRUSTED_REPO_CONTENT" in user
    assert UNTRUSTED_CLOSE in user
    assert "不可信输入" in system
    assert "禁止在 Controller 里写业务逻辑" in system


def test_repo_content_block_includes_all_present_sections() -> None:
    ctx = FileContext(
        path="A.java",
        level=3,
        diff_text="diff",
        enclosing_source_text="src",
        imports=["import x;"],
        static_text="",
    )
    block = repo_content_block(ctx)
    assert "变更所在方法的完整源码" in block
    assert "import 列表" in block
    assert "本次变更 diff" in block
    assert "静态检查结果" in block
    assert "未启用静态检查" in block


def test_static_finding_rendering_format() -> None:
    from acra.context.builder import render_static

    text = render_static(
        [StaticFinding(tool="semgrep", rule_id="java.sqli", severity="high", path="A.java", line=142, message="拼接 SQL")]
    )
    assert text == "- [semgrep:java.sqli] line 142: 拼接 SQL"
