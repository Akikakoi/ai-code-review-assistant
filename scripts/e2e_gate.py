"""CI 门禁退出码的端到端自检（离线可跑，不需要模型与网络）。

为什么要单独有它：`--fail-on` 的退出码契约（`0/1/2/3`）此前只在单测里被间接覆盖 ——
`severity_exit_hit()` 有 parametrize，但没有**任何一个真实进程**验证过
"退出码真的能当 CI 门禁用"。单测证明的是映射函数对，不是 CLI 真的按它退出。

本脚本用真实子进程（`python -m acra.cli review ...`）跑场景并断言退出码，
且额外断言"退出码符合预期是因为真的发生了预期的事"——
否则一次空跑（什么都没报）也会让退出码恰好等于期望值，那种"通过"毫无意义。

用法：
    ./.venv/Scripts/python.exe scripts/e2e_gate.py

结果写入 `.acra-work/e2e_gate.txt`（该目录已 gitignore）。通过返回 0，任一断言失败返回 1。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".acra-work"

EXIT_OK = 0
EXIT_GATE_HIT = 1
EXIT_BAD_ARGS = 2
EXIT_FAILED = 3


@dataclass(slots=True)
class Scenario:
    name: str
    args: list[str]
    expected_rc: int
    #: 退出码之外还必须成立的断言，用来挡住"空跑恰好返回期望码"的假通过
    must_contain: str | None = None
    #: 额外注入的环境变量（用来构造确定性的失败路径）
    env: dict[str, str] = field(default_factory=dict)


def run_cli(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    """跑一次真实 CLI 进程。`args` 是 `review` 之后的参数（子命令由这里补齐）。"""
    merged = {**os.environ, **(env or {})}
    proc = subprocess.run(
        [sys.executable, "-m", "acra.cli", "review", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def build_demo_repo(target: Path) -> Path:
    """用仓库自带的构造器生成一个必然命中的演示仓库（不依赖 examples/ 已被提交）。"""
    sys.path.insert(0, str(ROOT / "examples"))
    from build_static_demo import build  # type: ignore[import-not-found]

    return build(target)


def scenarios(repo: Path, tmp: Path) -> list[Scenario]:
    """`--publish` 的两个失败面用环境变量构造，因此不依赖本机有没有配凭据。"""
    no_credentials = {
        # 环境变量优先于 .env：这样即使本机配了 static token，这条路径也是确定的
        "ACRA_GITHUB_TOKEN": "",
        "GITHUB_APP_ID": "1",
        "GITHUB_APP_PRIVATE_KEY_PATH": str(tmp / "definitely-missing.pem"),
        "GITHUB_APP_INSTALLATION_ID": "1",
    }
    return [
        Scenario(
            name="未设门槛：有高危结论也返回 0（门禁是使用方主动选的，不是隐式阻断）",
            args=["--repo", str(repo), "--base", "main", "--head", "feature/tweak", "--no-llm"],
            expected_rc=EXIT_OK,
            must_contain="[高]",
        ),
        Scenario(
            name="--fail-on high：命中高危结论返回 1",
            args=[
                "--repo",
                str(repo),
                "--base",
                "main",
                "--head",
                "feature/tweak",
                "--no-llm",
                "--fail-on",
                "high",
            ],
            expected_rc=EXIT_GATE_HIT,
        ),
        Scenario(
            name="参数非法返回 2",
            args=["--format", "bogus"],
            expected_rc=EXIT_BAD_ARGS,
        ),
        Scenario(
            name="--publish 缺少 --pr 返回 2（发布目标必须明确）",
            args=[
                "--repo",
                str(repo),
                "--base",
                "main",
                "--head",
                "feature/tweak",
                "--no-llm",
                "--publish",
            ],
            expected_rc=EXIT_BAD_ARGS,
        ),
        Scenario(
            name="--pr 与 --base/--head 互斥仍然成立（非发布场景）",
            args=["--repo", str(repo), "--base", "main", "--head", "feature/tweak", "--pr", "1"],
            expected_rc=EXIT_BAD_ARGS,
        ),
        Scenario(
            name="--publish 拿不到凭据时返回 3 并说明原因（不是静默不发布）",
            args=[
                "--repo",
                str(repo),
                "--base",
                "main",
                "--head",
                "feature/tweak",
                "--no-llm",
                "--no-store",
                "--publish",
                "--repo-full-name",
                "owner/name",
                "--pr",
                "1",
            ],
            expected_rc=EXIT_FAILED,
            must_contain="私钥文件不存在",
            env=no_credentials,
        ),
        Scenario(
            name="分析失败（仓库不存在）返回 3",
            args=["--repo", str(tmp / "no-such-repo"), "--base", "main", "--head", "HEAD"],
            expected_rc=EXIT_FAILED,
        ),
    ]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="acra-e2e-gate-") as tmp:
        tmp_path = Path(tmp)
        repo = build_demo_repo(tmp_path / "static-demo")

        results: list[dict[str, object]] = []
        for sc in scenarios(repo, tmp_path):
            rc, out = run_cli(sc.args, sc.env)
            ok = rc == sc.expected_rc
            detail = f"rc={rc}（期望 {sc.expected_rc}）"
            if ok and sc.must_contain and sc.must_contain not in out:
                ok = False
                detail += f"；输出里没有 {sc.must_contain!r}，疑似空跑"
            results.append({"scenario": sc.name, "ok": ok, "detail": detail})
            print(f"[{'PASS' if ok else 'FAIL'}] {sc.name} —— {detail}")

    failed = [r for r in results if not r["ok"]]
    payload = {"passed": len(results) - len(failed), "total": len(results), "results": results}
    (OUT_DIR / "e2e_gate.txt").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
