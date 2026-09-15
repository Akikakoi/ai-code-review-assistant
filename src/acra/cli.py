"""CLI 入口（typer）。

契约见开发文档 §6.3：

```bash
acra review [OPTIONS]

--repo PATH             仓库路径，默认当前目录
--base REF              基线引用（分支/tag/SHA）
--head REF              目标引用，默认 HEAD
--pr NUMBER             PR 号（幂等键 / 增量基线 / 发布目标），与 --base/--head 并用
--format text|json|sarif
--out FILE              输出文件，缺省打到 stdout
--level 1|2|3           强制上下文层级（调试用）
--linters a,b,c         覆盖启用的静态工具
--dry-run               只分析不发布
--no-llm                纯静态分析模式（用于基线对比）
--fail-on high|medium   达到该严重度时返回退出码 1（供 CI 使用）
--publish               真实发布 review 到平台（需凭据 + --pr + --repo-full-name）
--repo-full-name o/r    发布目标仓库（owner/name）
```

退出码：`0` 正常，`1` 命中 `--fail-on` 门槛，`2` 参数错误，`3` 分析失败。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer

from acra import __version__
from acra.cache.content_hash import MemoryCache
from acra.errors import GitError
from acra.logging_setup import configure_logging
from acra.observability import metrics as metrics_mod
from acra.orchestrator.pipeline import ReviewOptions, ReviewOutcome, run_review
from acra.publish.factory import GithubAccess, open_publisher, resolve_access
from acra.publish.renderer import (
    render_json,
    render_sarif_json,
    render_text,
    severity_exit_hit,
)
from acra.settings import get_settings
from acra.store.db import Database
from acra.trigger.normalize import job_from_cli

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="acra · AI 代码审查助手：读 diff、给行级评论、不制造噪音",
)

EXIT_OK = 0
EXIT_GATE_HIT = 1
EXIT_BAD_ARGS = 2
EXIT_FAILED = 3

FORMATS = ("text", "json", "sarif")

#: 模块级默认值。typer 的默认值必须是模块级常量（不要写成 `typer.Option(Path(...))`：
#: 默认参数里的函数调用会被 lint 拦下，而且 Path 注解还会额外触发 B008）。
DEFAULT_EVAL_CASES = "eval/cases.jsonl"
DEFAULT_EVAL_REPORT = "eval/report.json"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"acra {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="显示版本并退出"
    ),
) -> None:
    """acra 命令行入口。"""


# ---------------------------------------------------------------------------- review


@app.command()
def review(
    repo: str = typer.Option(".", "--repo", help="仓库路径，默认当前目录"),
    base: str = typer.Option(None, "--base", help="基线引用（分支/tag/SHA）"),
    head: str = typer.Option(None, "--head", help="目标引用，默认 HEAD"),
    pr: int = typer.Option(
        None,
        "--pr",
        help="PR 号：用作幂等键与增量基线，也是 --publish 的发布目标；"
        "不会去平台拉取 ref（审查范围仍由 --base/--head 决定）",
    ),
    fmt: str = typer.Option("text", "--format", help="输出格式：text | json | sarif"),
    out: str = typer.Option(None, "--out", help="输出文件，缺省打到 stdout"),
    level: int = typer.Option(None, "--level", help="强制上下文层级 1|2|3（调试用）"),
    linters: str = typer.Option(None, "--linters", help="覆盖启用的静态工具，逗号分隔"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只分析不发布"),
    no_llm: bool = typer.Option(False, "--no-llm", help="纯静态分析模式（基线对比用）"),
    fail_on: str = typer.Option(None, "--fail-on", help="达到该严重度时返回退出码 1：high|medium"),
    shadow: bool = typer.Option(None, "--shadow/--no-shadow", help="影子模式：只落库不发布"),
    full: bool = typer.Option(False, "--full", help="强制全量重审，不做增量"),
    publish: bool = typer.Option(
        False, "--publish", help="真实发布 review 到平台（需要凭据 + --pr + --repo-full-name）"
    ),
    repo_full_name: str = typer.Option(
        None, "--repo-full-name", help="owner/name；发布时必填，缺省用仓库目录名"
    ),
    no_store: bool = typer.Option(False, "--no-store", help="不写运行记录（不碰数据库）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出调试日志"),
) -> None:
    """审查一次变更。"""
    if fmt not in FORMATS:
        raise typer.BadParameter(f"--format 只能是 {'|'.join(FORMATS)}")
    if pr is not None and pr < 1:
        raise typer.BadParameter("--pr 必须是正整数")
    if level is not None and level not in (1, 2, 3):
        raise typer.BadParameter("--level 只能是 1|2|3")
    if fail_on is not None and fail_on not in ("high", "medium"):
        raise typer.BadParameter("--fail-on 只能是 high|medium")
    if publish:
        # 发布需要知道"发到哪"：仓库全名与 PR 号缺一不可，否则只能在本地产出文本
        if pr is None:
            raise typer.BadParameter("--publish 需要 --pr 指定发布到哪个 PR")
        if not repo_full_name:
            raise typer.BadParameter("--publish 需要 --repo-full-name owner/name")
        if dry_run:
            raise typer.BadParameter("--publish 与 --dry-run 互斥")

    settings = get_settings()
    configure_logging("DEBUG" if verbose else settings.log_level)

    options = ReviewOptions(
        dry_run=dry_run,
        no_llm=no_llm,
        level=level,
        shadow=shadow,
        linters=[s.strip() for s in linters.split(",") if s.strip()] if linters else None,
        force_full=full,
    )

    job = job_from_cli(
        repo_path=repo,
        base_ref=base,
        head_ref=head,
        pr_number=pr,
        repo_full_name=repo_full_name or Path(repo).name or "local/repo",
        force_full=full,
    )

    db = None
    if not no_store:
        try:
            db = Database(settings.database_url or None, settings.ensure_workdir())
            db.create_all()
        except Exception as exc:  # noqa: BLE001 - 落库不可用不应阻止审查
            typer.secho(f"提示：运行记录不可用（{type(exc).__name__}），继续分析", err=True)
            db = None

    async def _run_review() -> ReviewOutcome:
        """凭据解析与 publisher 的生命周期都收在这里，`run_review` 本身不认识 token。"""
        access = GithubAccess()
        if publish:
            access = await resolve_access(settings, job)
            if not access.available:
                typer.secho(
                    f"无法获取 GitHub 凭据（来源={access.source}）：{access.error or '未配置'}",
                    err=True,
                )
                raise typer.Exit(EXIT_FAILED)
        async with open_publisher(access) as publisher:
            return await run_review(
                job,
                settings,
                options=options,
                db=db,
                cache=MemoryCache(),
                publisher=publisher,
                clone_token=access.token,
            )

    try:
        outcome = asyncio.run(_run_review())
    except GitError as exc:
        typer.secho(f"git 失败：{exc}", err=True)
        raise typer.Exit(EXIT_FAILED) from exc
    except KeyboardInterrupt:  # pragma: no cover
        raise typer.Exit(EXIT_FAILED) from None
    finally:
        if db is not None:
            db.dispose()

    metrics_mod.observe_outcome(outcome)

    if outcome.error:
        typer.secho(f"分析失败：{outcome.error}", err=True)
        if outcome.summary:
            typer.echo(outcome.summary)
        raise typer.Exit(EXIT_FAILED)

    payload = _render(outcome, fmt)
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        typer.echo(
            f"已写入 {target}（{len(outcome.findings)} 条行级评论，"
            f"{len(outcome.reported)} 条结论，耗时 {outcome.duration_ms}ms）"
        )
    else:
        typer.echo(payload)

    if outcome.mode == "paused":
        typer.secho("本次未分析：日预算已耗尽。", err=True)
        raise typer.Exit(EXIT_OK)

    # 发布结果必须打出来：`--publish` 的调用方要能一眼看到"到底发出去了没有"，
    # 而不是从"没有报错"倒推（幂等跳过与真的发出去，两者都没有报错）。
    if outcome.publish_result is not None:
        typer.echo(f"发布结果：{json.dumps(outcome.publish_result, ensure_ascii=False)}")

    if fail_on and severity_exit_hit(outcome.reported, fail_on):
        raise typer.Exit(EXIT_GATE_HIT)
    raise typer.Exit(EXIT_OK)


def _render(outcome, fmt: str) -> str:
    if fmt == "json":
        return render_json(
            outcome.reported,
            summary_inputs=outcome.summary_inputs(),
            dropped=[d.to_dict() for d in outcome.dropped],
            extra={"job": outcome.to_dict()},
        )
    if fmt == "sarif":
        return render_sarif_json(outcome.reported)
    return render_text(
        outcome.reported,
        summary_inputs=outcome.summary_inputs(),
        dropped_count=len(outcome.dropped),
        head_sha=outcome.diff_set.head_sha if outcome.diff_set else "",
    )


# ---------------------------------------------------------------------------- serve


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """启动 webhook 服务（web + 内联 worker）。"""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run("acra.main:app", host=host, port=port, reload=reload)


# ---------------------------------------------------------------------------- worker


@app.command()
def worker(
    iterations: int = typer.Option(None, "--iterations", help="最多处理多少个任务后退出（调试用）"),
    concurrency: int = typer.Option(2, "--concurrency"),
) -> None:
    """独立消费队列（web / worker 分离部署时使用，文档 §13.1）。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    from acra.main import _build_cache, _build_queue
    from acra.orchestrator.scheduler import run_worker

    db = Database(settings.database_url or None, settings.ensure_workdir())
    db.create_all()
    cache = _build_cache(settings)
    queue = _build_queue(settings)

    async def handler(job) -> None:
        access = await resolve_access(settings, job)
        if not access.available:
            typer.secho(
                f"[{job.job_id[:8]}] 无 GitHub 凭据（来源={access.source}），本次只分析不发布",
                err=True,
            )
        async with open_publisher(access) as publisher:
            outcome = await run_review(
                job,
                settings,
                options=ReviewOptions(),
                db=db,
                cache=cache,
                publisher=publisher,
                clone_token=access.token,
            )
        metrics_mod.observe_outcome(outcome)
        typer.echo(f"[{job.job_id[:8]}] {outcome.mode} → {len(outcome.reported)} 条结论")

    try:
        processed = asyncio.run(
            run_worker(
                queue,
                handler,
                concurrency=concurrency,
                stop_when_empty=iterations is None,
                max_iterations=iterations,
            )
        )
        typer.echo(f"已处理 {processed} 个任务")
    finally:
        db.dispose()


# ---------------------------------------------------------------------------- doctor


def _value_differs_from_default(current, default) -> bool:
    """判断生效值是否真的与声明默认值不同（消除"规范化"造成的假阳性）。

    有的字段校验器会把相对路径绝对化（`acra_workdir=".acra-work"` → 绝对路径）。
    直接比较会把它报成"被覆盖"，而它其实只是用了默认值。
    诊断输出里混进这种噪音，看的人很快就会学会忽略这一行 —— 那这个检查就白做了。
    """
    if current == default:
        return False
    cur = str(current).replace("\\", "/").rstrip("/")
    dflt = str(default).replace("\\", "/").rstrip("/")
    if not dflt:
        return True
    # 默认值是相对路径（不以 / 开头、也不含盘符）时，允许生效值是它的绝对化形式
    is_relative = not dflt.startswith("/") and ":/" not in dflt
    if is_relative:
        return not cur.endswith(dflt)
    return True


#: 名字里带这些词的 `ACRA_*` 项，值一律打码。
#:
#: doctor 的目的是"让被覆盖这件事可见"，但**凭据的值本身不该出现在终端、
#: 日志或 CI 输出里**。实测踩过：把 `ACRA_GITHUB_TOKEN` 写进 `.env` 之后，
#: doctor 直接把整串 token 打印了出来 —— 连一次失败测试的 diff 里都带出了它。
#: 需要判断"配没配、配的是哪一个"，长度就够了。
_SENSITIVE_KEY_HINTS = ("token", "secret", "password", "key", "credential")

MASKED = "***"


def _display_value(key: str, value: str) -> str:
    if any(hint in key.lower() for hint in _SENSITIVE_KEY_HINTS):
        return f"{MASKED}（已打码，长度 {len(value)}）"
    return value


def overridden_acra_settings(settings) -> list[tuple[str, str]]:
    """列出与**代码声明默认值**不同的 `ACRA_*` 项（字段名 → 生效值）。

    用途是让"被覆盖"这件事可见。配置项同时存在"代码默认值"与"外部覆盖"两层时，
    只报生效值是不够的 —— 改了代码默认值却看起来没生效时，得能一眼看出是谁盖的。

    **参照物必须是字段的声明默认值（`model_fields[...].default`），
    不能是 `Settings(_env_file=None)`。** 后者只关掉 `.env` 文件，
    环境变量照样会被读取 —— 用它当参照，环境变量造成的覆盖永远检测不出来
    （实测踩到过：`ACRA_MAX_COMMENTS=9` 明明生效了，doctor 却报"无覆盖"）。

    只比较 `acra_*` 字段：`LLM_*` / `DATABASE_URL` 这类本来就该在 .env 里配，报了是噪音。
    凭据类的值会被打码（见 `_display_value`）——**可见的是"谁盖的"，不是"值是什么"**。
    """
    from pydantic_core import PydanticUndefined

    from acra.settings import Settings

    current = settings.model_dump()
    out: list[tuple[str, str]] = []
    for key, field in sorted(Settings.model_fields.items()):
        if not key.startswith("acra_"):
            continue
        default = field.default
        if default is PydanticUndefined:
            continue
        if _value_differs_from_default(current.get(key), default):
            out.append((key, _display_value(key, str(current.get(key)))))
    return out


@app.command()
def doctor() -> None:
    """自检：git / 配置 / 数据库 / tree-sitter / 静态工具 / LLM 连通性。"""
    settings = get_settings()
    configure_logging("WARNING")

    rows: list[tuple[str, str, str]] = []

    import shutil
    import subprocess

    git = shutil.which("git")
    if git:
        version = subprocess.run(
            [git, "--version"], capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()
        rows.append(("git", "ok", version))
    else:
        rows.append(("git", "fail", "未找到 git 可执行文件"))

    rows.append(
        (
            "LLM",
            "ok" if settings.llm_configured else "warn",
            f"{settings.llm_api_base} / scan={settings.llm_scan_model}",
        )
    )

    from acra.repo.symbol_index import tree_sitter_available

    for lang in ("java", "typescript", "python"):
        rows.append(("tree-sitter", "ok" if tree_sitter_available(lang) else "warn", lang))

    from acra.analysis.static_runner import TOOLS, resolve_command

    resolved = {name: resolve_command(spec.command) for name, spec in TOOLS.items()}
    installed = [name for name, path in resolved.items() if path]
    rows.append(
        (
            "静态分析",
            "ok" if settings.acra_static_analysis_enabled else "info",
            (
                (
                    "已启用；可用工具："
                    + ("、".join(installed) if installed else "无（本次运行的检查会全部跳过）")
                )
                if settings.acra_static_analysis_enabled
                else "已关闭（ACRA_STATIC_ANALYSIS_ENABLED=false）"
            ),
        )
    )
    for name, path in resolved.items():
        rows.append(("  └ " + name, "ok" if path else "info", path or "未找到"))
    del installed

    try:
        db = Database(settings.database_url or None, settings.ensure_workdir())
        db.create_all()
        rows.append(("database", "ok", db.dialect))
        db.dispose()
    except Exception as exc:  # noqa: BLE001
        rows.append(("database", "fail", f"{type(exc).__name__}: {exc}"))

    rows.append(("redis", "ok" if settings.redis_url else "info", settings.redis_url or "未配置（用内存缓存/队列）"))

    from acra.publish.factory import SOURCE_NONE, describe_credentials

    cred_source, cred_detail = describe_credentials(settings)
    rows.append(
        (
            "发布凭据",
            "ok" if cred_source != SOURCE_NONE else "warn",
            f"[{cred_source}] {cred_detail}",
        )
    )
    rows.append(
        (
            "sandbox",
            "ok" if settings.acra_sandbox_enabled else "info",
            settings.acra_sandbox_image if settings.acra_sandbox_enabled else "已关闭（阶段一默认）",
        )
    )
    rows.append(("prometheus", "ok" if metrics_mod.available() else "warn", "prometheus_client"))
    rows.append(("workdir", "ok", str(settings.acra_workdir)))

    # 配置在代码里有默认值，.env / 环境变量可以覆盖。**被覆盖这件事必须可见**：
    # 实测踩过——把置信度门槛从 0.65 改成 0.50、代码与测试全绿，
    # 运行时读到的却仍是 0.65，因为 .env 里有一行把它钉住了。
    # 所以这里不只报生效值，而是把"哪些项与代码默认值不同"整个列出来。
    overrides = overridden_acra_settings(settings)
    if overrides:
        detail = "、".join(f"{key}={value}" for key, value in overrides)
        rows.append(("settings", "warn", f"{len(overrides)} 项被覆盖：{detail}"))
    else:
        rows.append(("settings", "ok", "全部使用代码默认值（无覆盖）"))

    width = max(len(name) for name, _, _ in rows)
    for name, status, detail in rows:
        colour = {"ok": typer.colors.GREEN, "info": typer.colors.CYAN}.get(status, typer.colors.YELLOW)
        typer.secho(f"{name.ljust(width)}  {status.upper():5}  {detail}", fg=colour)

    if "fail" in {r[1] for r in rows}:
        raise typer.Exit(EXIT_FAILED)
    raise typer.Exit(EXIT_OK)


# ---------------------------------------------------------------------------- eval

eval_app = typer.Typer(no_args_is_help=True, help="评估：跑数据集、出 Precision/Recall 报告")
app.add_typer(eval_app, name="eval")


@eval_app.command("build")
def eval_build(
    out: str = typer.Option(DEFAULT_EVAL_CASES, "--out", "-o", help="数据集输出路径"),
    variants: int = typer.Option(3, "--variants", help="每个缺陷模板展开的变体数"),
    no_negatives: bool = typer.Option(False, "--no-negatives", help="不生成反例"),
    curated: bool = typer.Option(
        False, "--curated", help="导出早期手写的 15 个用例，而不是 20 类注入语料"
    ),
) -> None:
    """导出评估数据集为 JSONL，便于人工审阅与长期积累。"""
    from acra.eval.dataset import builtin_cases, full_suite, save_jsonl

    cases = builtin_cases() if curated else full_suite(
        variants=variants, negatives=not no_negatives
    )
    count = save_jsonl(cases, Path(out))
    positives = sum(1 for c in cases if c.is_positive)
    typer.echo(
        f"已写入 {out}（{count} 个用例：正例 {positives} / 反例 {count - positives}）"
    )


@eval_app.command("run")
def eval_run(
    dataset: str = typer.Option(
        "", "--dataset", "-d", help="JSONL 数据集路径；留空使用 20 类注入语料"
    ),
    out: str = typer.Option(DEFAULT_EVAL_REPORT, "--out", "-o", help="报告输出路径"),
    limit: int = typer.Option(None, "--limit", help="只跑前 N 个用例"),
    sample: int = typer.Option(
        None, "--sample", help="分层抽样 N 个用例（保持正反例比例），用于快速跑分"
    ),
    variants: int = typer.Option(3, "--variants", help="内置语料每类缺陷展开的变体数"),
    no_llm: bool = typer.Option(False, "--no-llm", help="只跑静态检查（可作为基线）"),
    tolerance: int = typer.Option(2, "--tolerance", help="行号匹配容差"),
    level: int = typer.Option(None, "--level", help="强制上下文层级"),
    baseline: str = typer.Option("", "--compare-baseline", help="与既有报告对比（传报告 JSON 路径）"),
    workspace: str = typer.Option("", "--workspace", help="用例仓库的落地目录，默认在运行目录下"),
) -> None:
    """跑一遍评估并输出指标报告。"""
    from acra.eval.dataset import full_suite, load_jsonl, sample_cases
    from acra.eval.metrics import compare_baseline, render_report
    from acra.eval.runner import EvalRunConfig, outcomes_to_json, run_dataset

    settings = get_settings()
    configure_logging(settings.log_level)

    cases = load_jsonl(dataset) if dataset else full_suite(variants=variants)
    if sample:
        cases = sample_cases(cases, sample)
    if not cases:
        typer.secho("数据集为空", fg=typer.colors.RED)
        raise typer.Exit(EXIT_BAD_ARGS)

    root = Path(workspace) if workspace else settings.ensure_workdir() / "eval"
    root.mkdir(parents=True, exist_ok=True)

    config = EvalRunConfig(
        workspace=root, level=level, no_llm=no_llm, tolerance=tolerance, limit=limit
    )

    db = Database(settings.database_url or None, settings.ensure_workdir())
    db.create_all()

    def progress(index: int, total: int, outcome) -> None:
        flag = "OK " if not outcome.error else "ERR"
        typer.echo(
            f"[{index}/{total}] {flag} {outcome.case_id}"
            f"  期望 {len(outcome.expected_lines)} 条 / 上报 {len(outcome.reported)} 条"
            f"  {outcome.duration_ms}ms"
        )

    metrics, outcomes = asyncio.run(
        run_dataset(
            cases,
            settings,
            config,
            db=db,
            cache=MemoryCache(),
            progress=progress,
        )
    )

    notes: list[str] = []
    if baseline:
        baseline_payload = json.loads(Path(baseline).read_text(encoding="utf-8"))
        base_metrics = baseline_payload.get("metrics", baseline_payload)
        notes = compare_baseline(metrics, base_metrics)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(outcomes_to_json(metrics, outcomes, notes=notes), encoding="utf-8")

    typer.echo("")
    typer.echo(render_report(metrics, notes=notes))
    typer.echo("")
    typer.echo(f"报告已写入 {out}")

    unsatisfied = [n for n, (_t, met) in metrics.targets().items() if not met]
    raise typer.Exit(EXIT_OK if not unsatisfied else EXIT_GATE_HIT)


@eval_app.command("sweep-threshold")
def eval_sweep_threshold(
    report: str = typer.Option(..., "--report", "-r", help="既有评估报告 JSON（需含候选池）"),
    dataset: str = typer.Option(
        "", "--dataset", "-d", help="数据集路径；留空使用与 eval run 相同的默认语料"
    ),
    variants: int = typer.Option(3, "--variants", help="内置语料每类缺陷展开的变体数"),
    out: str = typer.Option("", "--out", "-o", help="扫描结果输出路径；留空只打屏"),
    maximum_comments: int = typer.Option(
        None, "--max-comments", help="模拟 ranker 的 top-K 截断；留空表示不截断"
    ),
) -> None:
    """在既有跑分结果上离线扫描置信度阈值，零额外模型成本。

    用来回答"ACRA_CONFIDENCE_THRESHOLD 该设多少"—— 当前最大的漏报来源。
    """
    from acra.eval import RECALL_TARGET
    from acra.eval.dataset import full_suite, load_jsonl
    from acra.eval.metrics import CaseOutcome, Reported
    from acra.eval.sweep import render_sweep, sweep
    from acra.settings import get_settings as _get_settings

    payload = json.loads(Path(report).read_text(encoding="utf-8"))
    # 默认与 `eval run` 的默认语料保持一致，否则报告里的 case_id 在数据集里找不到、扫描出来是空的
    cases = load_jsonl(dataset) if dataset else full_suite(variants=variants)
    case_ids = {c.case_id for c in cases}

    outcomes: list[CaseOutcome] = []
    for entry in payload.get("cases") or []:
        if entry.get("case_id") not in case_ids:
            continue
        outcomes.append(
            CaseOutcome(
                case_id=str(entry.get("case_id")),
                expected=bool(entry.get("expected", True)),
                expected_lines=list(entry.get("expected_lines") or []),
                source=str(entry.get("source") or "unknown"),
                matched=[Reported(*item) for item in entry.get("matched") or []],
                spurious=[Reported(*item) for item in entry.get("spurious") or []],
                missed=list(entry.get("missed") or []),
                raw_candidates=int(entry.get("raw_candidates") or 0),
                anchor_dropped=int(entry.get("anchor_dropped") or 0),
                error=entry.get("error"),
                candidate_pool=list(entry.get("candidate_pool") or []),
            )
        )

    if not outcomes:
        typer.secho("报告里没有可用的用例数据", fg=typer.colors.RED)
        raise typer.Exit(EXIT_BAD_ARGS)

    result = sweep(cases, outcomes, max_comments=maximum_comments)
    settings = _get_settings()
    typer.echo(render_sweep(result, current_threshold=settings.acra_confidence_threshold))

    best = result.best_by("precision", minimum_recall=RECALL_TARGET)
    if best is not None:
        typer.echo("")
        typer.echo(
            f"在 Recall ≥ {RECALL_TARGET} 的前提下，Precision 最高的阈值是 "
            f"{best.threshold:.2f}（Precision {best.metrics.precision:.3f} / "
            f"Recall {best.metrics.recall:.3f}）"
        )

    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        typer.echo(f"扫描结果已写入 {out}")
    raise typer.Exit(EXIT_OK)


@eval_app.command("variance")
def eval_variance(
    first: str = typer.Option(..., "--a", help="第一次跑分的报告 JSON"),
    second: str = typer.Option(..., "--b", help="第二次跑分的报告 JSON"),
) -> None:
    """对比同配置的两次跑分，量化跑批波动。

    没有这个数字就无法判断"改动有效"还是"模型随机"—— 上一轮实测里，
    同一配置两次运行的命中集合互相替换，聚合指标却完全相同。
    """
    from acra.eval.metrics import render_variance, variance_between

    a = json.loads(Path(first).read_text(encoding="utf-8"))
    b = json.loads(Path(second).read_text(encoding="utf-8"))
    result = variance_between(a, b)
    typer.echo(render_variance(result))
    raise typer.Exit(EXIT_OK)


# ---------------------------------------------------------------------------- versions


@app.command()
def versions() -> None:
    """列出可用的提示词模板版本。"""
    from acra.engine.prompt_loader import available_versions

    typer.echo(json.dumps({"versions": available_versions()}, ensure_ascii=False))


def main() -> None:
    try:
        app()
    except typer.Exit as exc:  # pragma: no cover - typer 自己处理
        sys.exit(exc.exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
