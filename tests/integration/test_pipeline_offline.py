"""端到端链路集成测试（离线）。

文档 §14.2：用本地 `git init` 构造的临时仓库跑完整链路（不依赖网络），断言最终
Finding 集合；用 `respx` 拦截 LLM 的 HTTP 调用，提供固定响应。

这一组测试是"锚定即真相"的核心保障：mock 的 LLM 会同时返回合法结论与三类幻觉结论
（行号不在白名单、路径不存在、枚举非法），断言只有合法的那条能出来。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from acra.orchestrator.pipeline import ReviewOptions, run_review
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set
from acra.trigger.normalize import job_from_cli
from tests.fixtures.repo import make_edge_case_repo, make_java_bug_repo

LLM_URL = "https://api.test.local/v1/chat/completions"


def _llm_response(findings: list[dict]) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "scan-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(findings)},
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 180,
            "total_tokens": 1380,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def _bug_line(repo, base: str, head: str) -> int:
    """用真实的 git 与解析链路找出"空值校验写反"那一行的新文件行号。"""
    handle = gateway.discover_local(repo)
    merge_base = handle.merge_base(base, head)
    head_sha = handle.resolve_commit(head)
    diff_set = build_diff_set(
        handle.diff(merge_base, head_sha),
        base_sha=base,
        head_sha=head_sha,
        merge_base_sha=merge_base,
    )
    fd = next(f for f in diff_set.files if f.path.endswith("OrderService.java"))
    return next(ln for h in fd.hunks for ln, text in h.added if "userId == null" in text)


def _valid_finding(line: int) -> dict:
    return {
        "path": "src/main/java/com/example/order/OrderService.java",
        "line": line,
        "end_line": None,
        "category": "bug",
        "severity": "high",
        "confidence": 0.85,
        "title": "空值校验逻辑被取反，null 入参会抛 NPE",
        "body": (
            "现象：把 || 改成 && 后，userId 为 null 时第一个条件为真、第二个条件会在 null 上调用 "
            "isEmpty()，直接抛 NullPointerException。触发条件：调用方传入 null。影响：支付接口 500，"
            "并且原本应当被拒绝的空 userId 请求会走进 doPay。修复建议：改回 userId == null || userId.isEmpty()。"
        ),
        "suggestion": "if (userId == null || userId.isEmpty()) {",
        "evidence": ["if (userId == null && userId.isEmpty())"],
        "needs_human_judgment": False,
    }


@pytest.mark.asyncio
@respx.mock
async def test_end_to_end_keeps_only_anchorable_findings(settings, tmp_path) -> None:
    repo, base, head = make_java_bug_repo(tmp_path)
    line = _bug_line(repo, base, head)

    route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(
            200,
            json=_llm_response(
                [
                    _valid_finding(line),
                    # 幻觉 1：行号不在白名单
                    {**_valid_finding(9999), "title": "幻觉行号"},
                    # 幻觉 2：路径不在本次变更内
                    {**_valid_finding(line), "path": "src/main/java/com/example/order/Ghost.java"},
                    # 幻觉 3：枚举非法
                    {**_valid_finding(line), "severity": "catastrophic"},
                    # 幻觉 4：行在 hunk 区间内但不是新增行
                    {**_valid_finding(line + 10), "title": "存量行"},
                    # 幻觉 5：confidence 越界
                    {**_valid_finding(line), "confidence": 1.8},
                    # 不是对象，应被忽略
                    "garbage",
                ]
            ),
        )
    )

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head, repo_full_name="local/demo")
    outcome = await run_review(job, settings, options=ReviewOptions(level=2))

    assert route.called
    assert outcome.error is None
    assert outcome.mode == "full"
    assert outcome.files_analyzed == 1
    assert outcome.lines_changed == 1
    assert outcome.raw_count == 6  # 6 个字典项，"garbage" 被忽略

    assert len(outcome.reported) == 1
    finding = outcome.reported[0]
    assert finding.line == line
    assert finding.severity == "high"
    assert finding.category == "bug"
    # 合法结论应当是行级评论（score = 0.85 + 0.20 + 0.10(risk bug) = 1.0 上限）
    assert outcome.findings == [finding]
    assert finding.score == pytest.approx(1.0)

    steps = {d.step for d in outcome.dropped}
    assert "1_schema" in steps
    assert "2_anchor" in steps
    assert len(outcome.dropped) == 5
    assert outcome.usage.prompt_tokens == 1200
    assert outcome.usage.completion_tokens == 180


@pytest.mark.asyncio
@respx.mock
async def test_llm_failure_degrades_without_crashing(settings, tmp_path) -> None:
    """文档 P7：LLM 不可用时降级为 static-only，而不是让这个 PR 没有任何反馈。"""
    repo, base, head = make_java_bug_repo(tmp_path)
    respx.post(LLM_URL).mock(return_value=httpx.Response(503, text="upstream unavailable"))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions())

    assert outcome.error is None
    assert outcome.mode == "static_only"
    assert outcome.degrade.ai_available is False
    assert any("AI 分析不可用" in n for n in outcome.degrade.notes)
    assert outcome.reported == []
    # 降级原因必须出现在 summary 里，让作者知道是"没分析"而不是"没问题"
    assert "AI 分析不可用" in outcome.summary
    assert "降级" in outcome.summary


@pytest.mark.asyncio
@respx.mock
async def test_empty_llm_array_yields_no_findings(settings, tmp_path) -> None:
    repo, base, head = make_java_bug_repo(tmp_path)
    respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=_llm_response([])))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions())

    assert outcome.reported == []
    assert outcome.raw_count == 0
    assert outcome.error is None
    assert "未发现值得修改的问题" in outcome.summary


@pytest.mark.asyncio
@respx.mock
async def test_markdown_fenced_json_is_parsed(settings, tmp_path) -> None:
    """供应商不支持 structured output 时，模型可能带 ```json 围栏。"""
    repo, base, head = make_java_bug_repo(tmp_path)
    line = _bug_line(repo, base, head)
    body = "```json\n" + json.dumps([_valid_finding(line)]) + "\n```"
    payload = _llm_response([])
    payload["choices"][0]["message"]["content"] = body
    respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=payload))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions())

    assert len(outcome.reported) == 1


@pytest.mark.asyncio
@respx.mock
async def test_no_llm_mode_skips_llm_entirely(settings, tmp_path) -> None:
    repo, base, head = make_java_bug_repo(tmp_path)
    route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=_llm_response([])))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions(no_llm=True))

    assert not route.called
    assert outcome.reported == []
    assert outcome.usage.calls == 0
    assert outcome.degrade.static_only is True


@pytest.mark.asyncio
async def test_static_only_mode_surfaces_static_findings(settings, tmp_path, monkeypatch) -> None:
    """文档 §4.2 承诺降级为 static-only 时"仅展示静态检查结果"。

    这条不能只靠"没报错"来验证 —— 必须真的有静态结论出现在输出里，
    否则作者看到的是"没有发现问题"，与"没做 AI 分析"完全相反。
    """
    import json as _json

    from acra.analysis.static_runner import ToolResult

    repo, base, head = make_java_bug_repo(tmp_path)
    settings.acra_static_analysis_enabled = True
    # 本机未必装了 semgrep；这里只验证"工具输出 → 归一化 → 过滤 → 输出"这条链
    monkeypatch.setattr(
        "acra.analysis.static_runner.resolve_command", lambda name: f"/usr/bin/{name}"
    )

    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "semgrep"}},
                "results": [
                    {
                        "ruleId": "java.sqli",
                        "level": "error",
                        "message": {"text": "拼接 SQL 语句"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    # 靠后缀匹配回真实仓库路径（工具只给我们临时目录路径）
                                    "artifactLocation": {"uri": "OrderService.java"},
                                    "region": {"startLine": 11},
                                }
                            }
                        ],
                    }
                ],
            }
        ],
    }

    def runner(argv, cwd, timeout):
        if "semgrep" in argv[0]:
            return ToolResult(tool="semgrep", returncode=0, stdout=_json.dumps(sarif))
        return ToolResult(tool=argv[0], returncode=127, stderr="not found")

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(
        job,
        settings,
        options=ReviewOptions(no_llm=True, linters=["semgrep"]),
        static_runner=runner,
    )

    assert outcome.mode == "static_only"
    assert outcome.degrade.static_only is True
    assert len(outcome.reported) == 1
    finding = outcome.reported[0]
    assert finding.source == "static"
    assert finding.tool == "semgrep"
    assert finding.line == 11
    assert finding.path.endswith("OrderService.java")
    assert "静态" in outcome.summary


@pytest.mark.asyncio
@respx.mock
async def test_content_hash_cache_avoids_second_llm_call(settings, tmp_path) -> None:
    """文档 §10.2：重复审查（重推、retry）应当 100% 命中内容哈希缓存。"""
    from acra.cache.content_hash import MemoryCache

    repo, base, head = make_java_bug_repo(tmp_path)
    line = _bug_line(repo, base, head)
    route = respx.post(LLM_URL).mock(
        return_value=httpx.Response(200, json=_llm_response([_valid_finding(line)]))
    )

    cache = MemoryCache()
    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    first = await run_review(job, settings, options=ReviewOptions(), cache=cache)
    calls_after_first = route.call_count
    second = await run_review(job, settings, options=ReviewOptions(), cache=cache)

    assert calls_after_first == 1
    assert route.call_count == 1  # 第二次完全命中缓存
    assert len(first.reported) == len(second.reported) == 1
    assert cache.hits >= 1


@pytest.mark.asyncio
@respx.mock
async def test_unparsable_llm_output_degrades_to_static_only(settings, tmp_path) -> None:
    """模型返回非法 JSON 时，不抛异常；整次审查降级为 static-only 并写明原因。

    本用例只有一块代码块，且这一块失败了 —— 等价于"全部失败"，因此走 static-only，
    而不是 partial_chunk_failure。这样作者看到的是"这次没分析"，而不是"这次没问题"。
    """
    repo, base, head = make_java_bug_repo(tmp_path)
    payload = _llm_response([])
    payload["choices"][0]["message"]["content"] = "这不是 JSON"
    respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=payload))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions())

    assert outcome.reported == []
    assert outcome.error is None
    assert outcome.mode == "static_only"
    assert outcome.degrade.ai_available is False
    assert any("AI 分析不可用" in n for n in outcome.degrade.notes)
    assert "AI 分析不可用" in outcome.summary


@pytest.mark.asyncio
@respx.mock
async def test_edge_case_repo_is_parsed_and_reported(settings, tmp_path) -> None:
    """rename / 新增 / 纯删除 / CRLF / 二进制混在一起时链路不崩。"""
    repo, base, head = make_edge_case_repo(tmp_path)
    respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=_llm_response([])))

    job = job_from_cli(repo_path=str(repo), base_ref=base, head_ref=head)
    outcome = await run_review(job, settings, options=ReviewOptions())

    assert outcome.error is None
    paths = {f.path for f in outcome.diff_set.files}
    # rename + 内容变更：以新路径出现
    assert "new/LegacyService.java" in paths
    # CRLF 文件的行号未被 \\r 影响
    assert "Crlf.java" in paths
    # 无结尾换行的文件
    assert "NoNewline.java" in paths
    # 二进制文件没有可评论行 → 被过滤
    assert "blob.bin" not in paths
    # 纯文档变更 → 作为低价值路径跳过
    assert "keep.txt" not in paths
    # 纯删除的 Java 文件没有新增行 → 被过滤
    assert "to_delete/Gone.java" not in paths
    assert outcome.chunks >= 1


@pytest.mark.asyncio
async def test_invalid_repo_path_reports_failure_not_exception(settings, tmp_path) -> None:
    job = job_from_cli(repo_path=str(tmp_path / "does-not-exist"))
    outcome = await run_review(job, settings, options=ReviewOptions())
    assert outcome.mode == "failed"
    assert outcome.error is not None
    assert outcome.degrade.fatal is True


@pytest.mark.asyncio
@respx.mock
async def test_persistence_writes_run_and_findings(settings, tmp_path) -> None:
    from acra.store.db import Database
    from acra.store.repository import metrics_summary

    repo, base, head = make_java_bug_repo(tmp_path)
    line = _bug_line(repo, base, head)
    respx.post(LLM_URL).mock(
        return_value=httpx.Response(200, json=_llm_response([_valid_finding(line)]))
    )

    db = Database("", tmp_path / "work")
    db.create_all()
    try:
        job = job_from_cli(
            repo_path=str(repo),
            base_ref=base,
            head_ref=head,
            pr_number=7,
            repo_full_name="local/demo",
        )
        outcome = await run_review(job, settings, options=ReviewOptions(), db=db)
        assert outcome.run_id is not None
        # 静态检查未启用是阶段一的设计，不该把这次运行标记成 degraded
        assert outcome.degrade.degraded is False

        with db.session() as session:
            summary = metrics_summary(session)
            assert summary["runs"] == 1
            assert summary["degraded_runs"] == 0
            assert summary["findings_kept"] == 1
            assert summary["input_tokens"] == 1200

        # 第二次带 pr_number 的运行应识别出上一轮的 head_sha（用于增量审查）
        from acra.store.repository import get_or_create_repository, last_reviewed_sha

        with db.session() as session:
            repo_row = get_or_create_repository(session, full_name="local/demo")
            assert last_reviewed_sha(session, repo_row.id, 7) == outcome.diff_set.head_sha
    finally:
        db.dispose()
