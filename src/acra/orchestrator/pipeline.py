"""端到端编排。

对应开发文档 §3.3「一次审查的完整时序」。CLI（`acra review`）、webhook worker、
手动 API 三条路径都调用这里的 `run_review`，保证行为一致。

顺序与文档 §3.3 对齐：

```
1  解析 refs
2  幂等 → 增量判定（上次审过的 head_sha）
3  预算守卫
4  merge-base
5  diff → FileDiff[]（含行号白名单）
6  静态分析（阶段一关闭）→ diff-aware 过滤
7  context_builder: L1 + L2（L3 按条件）
8  chunker: 语法边界 + token 预算
9  Scan
10 Verify（阶段一关闭，透传）
11 validator: Schema + 锚定
12 ranker: 去重合并 → 门槛 → 分级 → 排序 → 截断至 K 条
13 publisher: 提交 review + Check Run
14 store: 落库 ReviewRun / Finding，记录 token 与耗时
```

失败不静默：任何一步降级都会进入 `DegradeState.notes`，最终出现在 summary 的折叠区。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from acra.analysis import risk_rules
from acra.analysis.static_runner import run_static_analysis
from acra.analysis.static_runner import to_findings as to_static_findings
from acra.context.builder import ContextBuilder
from acra.context.chunker import chunk_file
from acra.engine.llm_client import LLMClient, Usage
from acra.engine.merge import merge_candidates
from acra.engine.prompt_loader import repo_content_block
from acra.engine.scan import scan_chunks
from acra.engine.verify import verify_candidates
from acra.errors import AcraError, CloneError, ConfigError, GitError
from acra.models import (
    Chunk,
    DiffSet,
    DropRecord,
    Finding,
    RawFinding,
    ReviewContext,
    ReviewJob,
    StaticFinding,
)
from acra.orchestrator import budget as budget_mod
from acra.orchestrator.degrade import (
    DegradeState,
    on_clone_failure,
    on_llm_unavailable,
    on_partial_chunk_failure,
)
from acra.postprocess.ranker import RankResult, assign_scores, order_and_truncate
from acra.postprocess.validator import (
    apply_threshold,
    cross_validate_findings,
    validate_candidates,
)
from acra.publish.renderer import (
    SummaryInputs,
    render_summary,
    review_comments_payload,
)
from acra.repo import gateway
from acra.repo.diff_parser import build_diff_set
from acra.repo.symbol_index import SymbolIndex

logger = logging.getLogger("acra.pipeline")


@dataclass(slots=True)
class ReviewOptions:
    """一次审查的运行选项，与 CLI 参数一一对应（文档 §6.3）。"""

    dry_run: bool = False
    no_llm: bool = False
    level: int | None = None
    shadow: bool | None = None
    linters: list[str] | None = None
    force_full: bool = False


@dataclass(slots=True)
class ReviewOutcome:
    job: ReviewJob
    mode: str = "full"
    diff_set: DiffSet | None = None
    rank: RankResult = field(default_factory=RankResult)
    dropped: list[DropRecord] = field(default_factory=list)
    raw_count: int = 0
    chunks: int = 0
    context_level_max: int = 2
    usage: Usage = field(default_factory=Usage)
    degrade: DegradeState = field(default_factory=DegradeState)
    static_tools: list[str] = field(default_factory=list)
    duration_ms: int = 0
    summary: str = ""
    publish_result: dict[str, Any] | None = None
    run_id: int | None = None
    error: str | None = None
    #: Verify 阶段的驳回率（§11.2 目标区间 0.3~0.6；未启用时为 0）
    verify_rejection_rate: float = 0.0
    #: 通过校验与合并、**尚未经过第 8 步门槛**的完整候选池。
    #: 评估层用它离线重放不同 `ACRA_CONFIDENCE_THRESHOLD`，零额外模型成本。
    candidate_pool: list[dict[str, Any]] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return self.rank.line_comments

    @property
    def reported(self) -> list[Finding]:
        return self.rank.all_reported

    @property
    def files_analyzed(self) -> int:
        return len(self.diff_set.files) if self.diff_set else 0

    @property
    def lines_changed(self) -> int:
        return self.diff_set.total_added_lines if self.diff_set else 0

    def summary_inputs(self) -> SummaryInputs:
        return SummaryInputs(
            files_analyzed=self.files_analyzed,
            lines_changed=self.lines_changed,
            context_level_max=self.context_level_max,
            static_tools=self.static_tools,
            input_tokens=self.usage.prompt_tokens,
            output_tokens=self.usage.completion_tokens,
            cost_micros=self.usage.cost_micros,
            degrade_notes=list(self.degrade.notes),
            mode=self.mode,
            ai_available=self.degrade.ai_available,
            l3_used=self.context_level_max >= 3,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job.job_id,
            "mode": self.mode,
            "files_analyzed": self.files_analyzed,
            "lines_changed": self.lines_changed,
            "chunks": self.chunks,
            "raw_candidates": self.raw_count,
            "findings_kept": len(self.findings),
            "findings_reported": len(self.reported),
            "context_level_max": self.context_level_max,
            "dropped": len(self.dropped),
            "usage": self.usage.to_dict(),
            "degrade_notes": self.degrade.notes,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "run_id": self.run_id,
            "publish": self.publish_result,
            "error": self.error,
            "verify_rejection_rate": round(self.verify_rejection_rate, 4),
            "sources": {
                "llm": sum(1 for f in self.reported if f.source == "llm"),
                "static": sum(1 for f in self.reported if f.source == "static"),
            },
        }


# ---------------------------------------------------------------------------- 主流程


async def run_review(
    job: ReviewJob,
    settings,
    *,
    options: ReviewOptions | None = None,
    db=None,
    cache=None,
    publisher=None,
    llm_client: LLMClient | None = None,
    custom_conventions: str = "",
    repo_config=None,
    static_runner=None,
    clone_token: str | None = None,
) -> ReviewOutcome:
    """一次审查的编排入口。

    `repo_config` 为 `store.repository.RepoConfigView`；不传且给了 `db` 时会按仓库自动加载。
    它决定 max_comments / 置信度门槛 / 忽略路径与规则 / 启用的 linter（文档 §12.5）。

    `static_runner` 用于注入静态分析的工具执行后端（测试用；生产走真实子进程）。

    `publisher` 为 None 时**不发布**（本地 `--dry-run` 即此语义）。`clone_token` 单独传入
    而不是从 publisher 里反查：私有仓库的克隆发生在发布之前，两者生命周期并不重合。
    """
    started = time.monotonic()
    opts = options or ReviewOptions(job.force_full)
    outcome = ReviewOutcome(job=job)
    outcome.context_level_max = opts.level or min(settings.acra_context_level_max, 3)

    # ---- 1. 仓库接入 ----
    try:
        handle = await asyncio.to_thread(_open_repo, job, settings, token=clone_token)
    except (GitError, CloneError) as exc:
        on_clone_failure(outcome.degrade)
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.mode = "failed"
        outcome.duration_ms = _ms(started)
        logger.warning("仓库接入失败：%s", outcome.error)
        return outcome

    # ---- 2. refs 与增量判定 ----
    try:
        diff_set, mode, notes, info = await asyncio.to_thread(
            _prepare_diff, handle, job, settings, db, opts
        )
    except GitError as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.mode = "failed"
        outcome.duration_ms = _ms(started)
        logger.warning("diff 计算失败：%s", outcome.error)
        return outcome

    outcome.diff_set = diff_set
    outcome.mode = mode
    for note in notes:
        outcome.degrade.add(note)
    for note in info:
        outcome.degrade.add(note, degraded=False)

    # ---- 2.5 仓库配置 ----
    # 文档 §12.5：max_comments / threshold / ignored_paths / enabled_linters /
    # ignored_rules / custom_conventions 都由仓库配置驱动。阶段一没接上，阶段二补。
    if repo_config is None and db is not None:
        try:
            from acra.store.repository import load_repo_config

            with db.session() as session:
                repo_config = load_repo_config(session, _resolve_repository(session, job))
        except Exception as exc:  # noqa: BLE001 - 配置读不到就用默认值
            logger.debug("读取仓库配置失败（用默认值）：%s", exc)
    if repo_config is not None:
        if repo_config.ignored_paths:
            guard = risk_rules.apply_budget_guard(
                diff_set, settings, ignore_paths=repo_config.ignored_paths
            )
            diff_set = guard.diff_set
            outcome.diff_set = diff_set
            for note in guard.notes:
                outcome.degrade.add(note)
            for note in guard.info:
                outcome.degrade.add(note, degraded=False)
        if not custom_conventions and repo_config.custom_conventions:
            custom_conventions = repo_config.custom_conventions

    # ---- 3. 预算守卫 ----
    if db is not None:
        try:
            from acra.store.repository import daily_cost_micros

            with db.session() as session:
                daily = daily_cost_micros(session)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取日成本失败（忽略）：%s", exc)
            daily = 0
        decision = budget_mod.evaluate(settings, diff_set=diff_set, daily_cost_micros=daily)
        for note in decision.notes:
            outcome.degrade.add(note)
        if not decision.allowed:
            outcome.mode = "paused"
            outcome.duration_ms = _ms(started)
            return outcome
        if decision.static_only:
            opts.no_llm = True

    # ---- 4. 静态分析 ----
    head_sha = diff_set.head_sha
    # 空列表与 None 同义：留空表示"按语言自动选工具"，而不是"什么都不跑"
    linter_filter = opts.linters or (
        repo_config.enabled_linters if repo_config else None
    )
    static_report = await run_static_analysis(
        diff_set.files,
        settings,
        whitelist=diff_set.whitelist(),
        read_file=lambda path: handle.show_file(head_sha, path),
        linters=linter_filter or None,
        ignored_rules=repo_config.ignored_rules if repo_config else (),
        runner=static_runner,
    )
    static_findings: list[StaticFinding] = static_report.findings
    outcome.static_tools = list(static_report.executed)
    if static_report.raw_count:
        outcome.degrade.add(
            f"静态检查原始命中 {static_report.raw_count} 条，"
            f"按变更行过滤后保留 {len(static_findings)} 条"
            + (f"（另有 {static_report.dropped_by_cap} 条因超出上限被截断）" if static_report.dropped_by_cap else ""),
            degraded=False,
        )
    if static_report.skipped:
        # "静态分析未启用/工具没装"是配置事实，不是本次分析被降级
        outcome.degrade.add("静态检查未执行：" + "、".join(static_report.skipped), degraded=False)
    if static_report.timed_out:
        outcome.degrade.add("静态检查超时：" + "、".join(static_report.timed_out))

    # ---- 5. 上下文与分块 ----
    chunks, context_notes = await asyncio.to_thread(
        _build_chunks, handle, diff_set, settings, static_findings, opts
    )
    for note in context_notes:
        outcome.degrade.add(note)
    outcome.chunks = len(chunks)

    # ---- 6. 全局预算 ----
    from acra.context.budget import RunBudget

    run_budget = RunBudget(total=settings.acra_total_input_token_budget)
    if not chunks:
        outcome.degrade.add("没有需要分析的代码块（可能全部被跳过或过滤）", degraded=False)

    # ---- 7. Scan ----
    client = llm_client
    owns_client = False
    if client is None and not opts.no_llm:
        try:
            client = LLMClient(settings)
            owns_client = True
        except Exception as exc:  # noqa: BLE001
            outcome.degrade.add(f"LLM 客户端初始化失败：{exc}")

    try:
        if opts.no_llm or client is None:
            candidates: list[RawFinding] = []
            if opts.no_llm:
                outcome.degrade.add("显式跳过模型分析（--no-llm），仅运行静态检查", degraded=False)
            else:
                outcome.degrade.add("未配置 LLM_API_KEY，已退化为纯静态分析模式")
            # 模式与降级标记必须如实反映"没有做 AI 分析"，否则 summary 与 Check Run
            # 会把一次纯静态检查说成一次完整审查。
            outcome.degrade.static_only = True
            outcome.mode = "static_only"
        elif not settings.llm_configured:
            raise ConfigError("未配置 LLM_API_KEY")
        else:
            kept_chunks, budget_note = budget_mod.truncate_chunks_to_budget(
                chunks, run_budget.remaining()
            )
            if budget_note:
                outcome.degrade.add(budget_note)
            scan = await scan_chunks(
                kept_chunks,
                client=client,
                settings=settings,
                custom_conventions=custom_conventions,
                cache=cache,
                run_budget=run_budget,
            )
            outcome.raw_count = len(scan.findings)
            if scan.all_failed:
                on_llm_unavailable(outcome.degrade)
                outcome.mode = "static_only"
            elif scan.failed_chunks:
                on_partial_chunk_failure(
                    outcome.degrade, len(scan.failed_chunks), len(kept_chunks)
                )
            candidates = scan.findings
        outcome.usage = client.usage if client is not None else Usage()

        # ---- 8~11. 有 LLM 时才走"候选 → 验证 → 校验 → 交叉验证" ----
        # static-only 模式（显式 --no-llm，或 LLM 不可用降级）直接使用静态工具的结论。
        # 这一步不是可选项：文档 §4.2 承诺"降级为 static-only 模式，summary 中标注
        # 仅展示静态检查结果" —— 如果这里不把静态结论转成 Finding，那句标注就是空话，
        # 作者看到的会是"没有发现问题"，而这与实际发生的事（没做 AI 分析）完全相反。
        static_only = opts.no_llm or outcome.degrade.static_only
        if static_only:
            findings = to_static_findings(static_findings)
            outcome.candidate_pool = [
                {
                    "path": f.path,
                    "line": f.line,
                    "category": f.category,
                    "severity": f.severity,
                    "confidence": round(f.confidence, 4),
                    "verify_verdict": None,
                    "needs_human_judgment": bool(f.needs_human_judgment),
                    "evidence_rule_ids": list(f.evidence_rule_ids),
                    "source": "static",
                }
                for f in findings
            ]
            if findings:
                outcome.degrade.add(
                    f"降级模式下直接展示 {len(findings)} 条静态检查结论（未经模型判断）",
                    degraded=False,
                )
        else:
            # ---- 8. Verify（收敛）----
            verify = await verify_candidates(
                candidates,
                client=client,
                settings=settings,
                diff_set=diff_set,
                contexts=_verify_contexts(chunks),
                run_budget=run_budget,
            )
            # Verify 的状态汇报永远是信息性的：真正出问题时会由 verify.degraded 单独上报
            outcome.degrade.add(verify.summary_note(), degraded=False)
            outcome.verify_rejection_rate = verify.rejection_rate
            for note in verify.degraded:
                outcome.degrade.add(note)
            candidates = verify.kept
            verify_dropped = list(verify.dropped)

            findings = _to_findings_from_candidates(
                candidates,
                diff_set=diff_set,
                static_findings=static_findings,
                outcome=outcome,
                repo_config=repo_config,
            )
            # Verify 驳回的记录与校验丢弃的记录一起对外可见，便于排查"为什么没报"
            outcome.dropped = verify_dropped + list(outcome.dropped)

        # ---- 12. 评分 → 分级 → 截断 ----
        ctx = ReviewContext(
            head_sha=diff_set.head_sha,
            risk_categories=set(risk_rules.RISK_CATEGORIES),
            static_findings=static_findings,
        )
        outcome.rank = order_and_truncate(
            findings,
            max_comments=(
                repo_config.max_comments if repo_config else settings.acra_max_comments
            ),
            ctx=ctx,
        )
    except ConfigError as exc:
        outcome.degrade.add(str(exc))
        outcome.degrade.static_only = True
        outcome.mode = "static_only"
    except AcraError as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.warning("审查失败：%s", outcome.error)
    finally:
        if owns_client and client is not None:
            await client.aclose()

    # ---- 13. summary ----
    outcome.summary = render_summary(outcome.reported, outcome.summary_inputs())

    # 分析耗时在落库前定稿：发布耗时不算进分析延迟（文档 §10.3 的 p95 口径）
    outcome.duration_ms = _ms(started)

    # ---- 14. 落库 ----
    if db is not None:
        try:
            outcome.run_id = await asyncio.to_thread(_persist, db, job, outcome)
        except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次输出
            logger.warning("落库失败：%s", exc)
            outcome.degrade.add(f"运行记录落库失败：{type(exc).__name__}", degraded=False)

    # ---- 15. 发布 ----
    shadow = settings.acra_shadow_mode if opts.shadow is None else opts.shadow
    if publisher is not None and not opts.dry_run and not shadow and outcome.degrade.publish_allowed:
        try:
            outcome.publish_result = await _publish(publisher, job, outcome)
        except AcraError as exc:
            outcome.degrade.add(f"评论发布失败（不影响分析结果）：{type(exc).__name__}")
    elif shadow and not opts.dry_run:
        outcome.degrade.add("影子模式：只落库不发布评论", degraded=False)

    return outcome


# ---------------------------------------------------------------------------- 步骤实现


def _open_repo(job: ReviewJob, settings, *, token: str | None = None):
    if job.remote_url:
        # 私有仓库必须带 installation token；公开仓库带了也无害（文档 §4.4）
        return gateway.ensure_bare_clone(
            job.remote_url,
            settings.ensure_workdir(),
            token=token,
            depth=1,
        )
    return gateway.discover_local(job.repo_path)


def _prepare_diff(handle, job: ReviewJob, settings, db, opts: ReviewOptions):
    """返回 (DiffSet, mode, 降级说明, 信息性说明)。"""
    notes: list[str] = []
    info: list[str] = []
    head = job.head_sha or handle.resolve_commit(job.head_ref or "HEAD")
    base_ref = job.base_ref or handle.default_branch()
    base = job.base_sha or handle.resolve_commit(base_ref)
    merge_base = handle.merge_base(base, head)

    start_sha = merge_base
    mode = "full"

    if not opts.force_full and job.pr_number and db is not None:
        from acra.store.repository import last_merge_base, last_reviewed_sha

        try:
            with db.session() as session:
                repo = _resolve_repository(session, job)
                prev_head = last_reviewed_sha(session, repo, job.pr_number)
                prev_mb = last_merge_base(session, repo, job.pr_number)
        except Exception:  # noqa: BLE001
            prev_head = prev_mb = None

        if prev_head and prev_head != head:
            if prev_mb and prev_mb != merge_base:
                info.append("检测到 rebase（merge-base 变化），本次按全量重审")
            elif handle.try_resolve_commit(prev_head):
                start_sha = prev_head
                mode = "incremental"

    raw = handle.diff(start_sha, head)
    diff_set = build_diff_set(
        raw, base_sha=base, head_sha=head, merge_base_sha=merge_base
    )

    guard = risk_rules.apply_budget_guard(diff_set, settings)
    notes.extend(guard.notes)
    info.extend(guard.info)
    if guard.truncated:
        notes.append("本次为降级审查，分析范围不完整")

    return guard.diff_set, mode, notes, info


def _build_chunks(
    handle,
    diff_set: DiffSet,
    settings,
    static_findings: list[StaticFinding],
    opts: ReviewOptions,
) -> tuple[list[Chunk], list[str]]:
    notes: list[str] = []
    symbol_index = SymbolIndex()

    # 符号索引需要一份仓库文件清单来把 import 解析成定义文件路径（文档 §7.5）。
    # 清单本身只取一次；超大仓库直接放弃索引，避免为了 L2 多花一次全量 ls-tree。
    repo_files: list[str] = []
    if settings.acra_l2_type_signatures:
        try:
            repo_files = handle.list_files(diff_set.head_sha)
        except Exception as exc:  # noqa: BLE001 - 索引是增强项，失败不影响 L1/L2 主路径
            notes.append(f"仓库文件清单获取失败，已跳过类型签名：{type(exc).__name__}")
        if repo_files and len(repo_files) > settings.acra_l2_max_repo_files:
            notes.append(
                f"仓库文件数 {len(repo_files)} 超过 {settings.acra_l2_max_repo_files}，已跳过类型签名索引"
            )
            repo_files = []

    builder = ContextBuilder(
        handle,
        settings,
        head_sha=diff_set.head_sha,
        symbol_index=symbol_index,
        repo_files=repo_files,
    )
    chunks: list[Chunk] = []

    files = sorted(
        diff_set.files,
        key=lambda f: (not _is_high_risk(f), -f.added_count, f.path),
    )
    attempted = 0
    failed = 0
    for file_diff in files:
        attempted += 1
        try:
            ctx = builder.build(
                file_diff,
                level=opts.level,
                static_findings=[
                    sf for sf in static_findings if sf.path == file_diff.path
                ],
                allow_l3=bool(risk_rules.l3_triggers(file_diff)),
            )
        except Exception as exc:  # noqa: BLE001 - 单文件上下文失败不应终止整次审查
            failed += 1
            detail = " ".join(str(exc).split())[:120]
            notes.append(f"文件 {file_diff.path} 上下文构建失败：{type(exc).__name__}: {detail}")
            logger.warning("上下文构建失败 %s: %s", file_diff.path, exc)
            continue
        if not ctx.diff_text:
            notes.append(f"文件 {file_diff.path} 无可用上下文，已跳过")
            continue
        for note in ctx.degraded:
            notes.append(f"{file_diff.path}: {note}")
        chunks.extend(chunk_file(file_diff, ctx, settings))

    # 全量失败必须显式报出来。否则一次系统性的上下文构建故障会表现成"这次没什么问题"，
    # 那是最危险的一种静默失败（文档 P7：失败要可降级，不要静默消失）。
    if attempted and failed == attempted:
        notes.append(f"全部 {attempted} 个文件的上下文构建均失败，本次无有效分析范围")

    # 去重降级说明，保持可读
    deduped: list[str] = []
    for note in notes:
        if note not in deduped:
            deduped.append(note)
    return chunks, deduped[:20]


def _is_high_risk(file_diff) -> bool:
    return risk_rules.looks_high_risk(file_diff)


def _verify_contexts(chunks: list[Chunk]) -> dict[str, list[tuple[set[int], str]]]:
    """按文件收集用于 Verify 自证的仓库内容块。

    一个文件可能被切成多块，都要留着 —— Verify 需要"这条结论所在的那一块"，
    给它别的块等于让它对着不相干的代码做判断。
    """
    contexts: dict[str, list[tuple[set[int], str]]] = {}
    for chunk in chunks:
        if chunk.context is None:
            continue
        block = repo_content_block(chunk.context)
        if not block:
            continue
        contexts.setdefault(chunk.path, []).append((set(chunk.added_line_numbers), block))
    return contexts


def _to_finding(raw: RawFinding) -> Finding:
    finding = Finding.from_raw(raw, score=raw.confidence)
    finding.evidence_rule_ids = list(raw.raw.get("_evidence_rule_ids") or [])
    finding.merged_sources = int(raw.raw.get("_merged_sources", 1))
    finding.verify_verdict = raw.raw.get("_verify_verdict")
    finding.verify_reason = raw.raw.get("_verify_reason")
    finding.score_penalty = float(raw.raw.get("_score_penalty") or 1.0)
    finding.source = "llm"
    return finding


def _to_findings_from_candidates(
    candidates: list[RawFinding],
    *,
    diff_set: DiffSet,
    static_findings: list[StaticFinding],
    outcome: ReviewOutcome,
    repo_config,
) -> list[Finding]:
    """走完 §9.1 的第 1~6 步与第 8 步，产出可评分的 Finding。

    顺序不能颠倒：**先校验（Schema + 锚定 + 类别 + 存量）再合并**。
    合并会把同簇候选的代表换成置信度最高的那条，若在校验前合并，
    一条越界的幻觉结论（例如 confidence=1.8）会顶掉合法结论，然后连累整条被丢弃。
    文档 §3.3 的时序也是校验(11) → 合并(12)。

    第 8 步（门槛）放在最后：它排在交叉验证(5)与去重(6)之后（文档 §9.1），
    且评估层需要"过门槛之前的完整候选池"来离线扫阈值，所以池子先记在 `outcome` 上。
    """
    validation = validate_candidates(
        candidates, diff_set, static_findings=static_findings
    )
    kept = merge_candidates(validation.kept)
    kept = cross_validate_findings(kept, static_findings)

    # 过门槛之前的完整候选池 —— 评估层用它重放不同阈值（零额外模型成本）
    threshold = repo_config.confidence_threshold if repo_config else None
    outcome.candidate_pool = _pool_records(kept)

    filtered, threshold_dropped = apply_threshold(kept, threshold)
    outcome.dropped = [*validation.dropped, *threshold_dropped]
    return [_to_finding(raw) for raw in filtered]


def _pool_records(candidates: list[RawFinding]) -> list[dict[str, Any]]:
    """记录候选池，并**用真实评分函数**补上 score。

    评估层离线扫阈值时要重新排序与截断，必须拿到与实际运行同源的 score；
    没有 score 就只能按置信度近似排序，扫描结论会与真实重跑对不上。
    """
    if not candidates:
        return []
    scored = assign_scores([_to_finding(raw) for raw in candidates])
    pool: list[dict[str, Any]] = []
    for raw, finding in zip(candidates, scored, strict=True):
        record = _pool_record(raw)
        record["score"] = round(finding.score, 4)
        pool.append(record)
    return pool


def _pool_record(raw: RawFinding) -> dict[str, Any]:
    """候选池记录：足够在离线重放时重新做门槛过滤与排序截断。"""
    return {
        "path": raw.path,
        "line": raw.line,
        "category": raw.category,
        "severity": raw.severity,
        "confidence": round(raw.confidence, 4),
        "verify_verdict": raw.raw.get("_verify_verdict"),
        "needs_human_judgment": bool(raw.needs_human_judgment),
        "evidence_rule_ids": list(raw.raw.get("_evidence_rule_ids") or []),
        "source": "llm",
    }


async def _publish(publisher, job: ReviewJob, outcome: ReviewOutcome) -> dict[str, Any]:
    from acra.orchestrator.degrade import check_run_conclusion

    owner, _, repo = job.repo_full_name.partition("/")
    if not owner or not repo or job.pr_number is None:
        return {"skipped": True, "reason": "missing_platform_target"}

    head_sha = outcome.diff_set.head_sha if outcome.diff_set else (job.head_sha or "")
    comments = review_comments_payload(outcome.findings)
    result = await publisher.create_review(
        owner,
        repo,
        job.pr_number,
        head_sha=head_sha,
        body=outcome.summary,
        comments=comments,
    )

    has_high = any(f.severity in ("blocker", "high") for f in outcome.reported)
    conclusion = check_run_conclusion(outcome.degrade, has_high_severity=has_high)
    try:
        check = await publisher.create_check_run(
            owner,
            repo,
            head_sha=head_sha,
            conclusion=conclusion,
            title=f"acra 发现 {len(outcome.reported)} 个问题",
            summary="\n".join(f"- {f.path}:{f.line} {f.title}" for f in outcome.reported)
            or "未发现问题",
        )
        result["check_run_id"] = check.get("id")
    except AcraError as exc:
        result["check_run_error"] = f"{type(exc).__name__}"
    return result


def _resolve_repository(session, job: ReviewJob) -> int:
    from acra.store.repository import get_or_create_repository

    repo = get_or_create_repository(
        session,
        platform="local" if not job.remote_url else job.platform,
        full_name=job.repo_full_name,
        installation_id=job.installation_id,
    )
    return repo.id


def _persist(db, job: ReviewJob, outcome: ReviewOutcome) -> int | None:
    from acra.store.repository import finish_run, save_findings, start_run

    diff_set = outcome.diff_set
    if diff_set is None:
        return None

    with db.session() as session:
        repo_id = _resolve_repository(session, job)
        run = start_run(
            session,
            job_id=str(uuid.uuid4()),
            repository_id=repo_id,
            pr_number=job.pr_number or 0,
            base_sha=diff_set.base_sha,
            head_sha=diff_set.head_sha,
            merge_base_sha=diff_set.merge_base_sha,
            trigger_source=job.trigger_source,
            mode=outcome.mode,
        )
        rows = save_findings(session, run.id, outcome.rank.all_ranked)
        for row, finding in zip(rows, outcome.rank.all_ranked, strict=False):
            finding.published = row.published
        status = (
            "failed"
            if outcome.error
            else ("degraded" if outcome.degrade.degraded else "succeeded")
        )
        finish_run(
            session,
            run,
            status=status,
            context_level_max=outcome.context_level_max,
            files_analyzed=outcome.files_analyzed,
            lines_changed=outcome.lines_changed,
            findings_raw=outcome.raw_count,
            findings_kept=len(outcome.reported),
            input_tokens=outcome.usage.prompt_tokens,
            output_tokens=outcome.usage.completion_tokens,
            cost_micros=outcome.usage.cost_micros,
            duration_ms=outcome.duration_ms,
            error_message=outcome.error,
            degrade_notes=outcome.degrade.notes,
        )
        return run.id


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
