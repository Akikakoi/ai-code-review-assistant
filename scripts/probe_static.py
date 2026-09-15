"""隔离复现：run_static_analysis 为什么报 ruff 未安装。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acra.analysis.static_runner import (  # noqa: E402
    TOOLS,
    _select_tools,
    resolve_command,
    run_static_analysis,
)
from acra.models import ChangeType, FileDiff, Hunk  # noqa: E402
from acra.repo import gateway  # noqa: E402
from acra.repo.diff_parser import build_diff_set  # noqa: E402
from acra.settings import get_settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "examples" / "static-demo"


def main() -> int:
    print("resolve_command('ruff') =", resolve_command("ruff"))

    settings = get_settings()
    settings.acra_static_analysis_enabled = True
    print("static enabled =", settings.acra_static_analysis_enabled)

    handle = gateway.discover_local(str(REPO))
    mb = handle.merge_base("main", "feature/tweak")
    head = handle.resolve_commit("feature/tweak")
    diff_set = build_diff_set(handle.diff(mb, head), base_sha="main", head_sha=head, merge_base_sha=mb)
    print("files:", [(f.path, sorted(f.added_line_numbers)) for f in diff_set.files])

    selected, skipped = _select_tools(diff_set.files, settings, None)
    print("selected:", selected, "skipped:", skipped)
    for tool in selected:
        print(f"  {tool}: command={TOOLS[tool].command!r} -> {resolve_command(TOOLS[tool].command)}")

    report = asyncio.run(
        run_static_analysis(
            diff_set.files,
            settings,
            whitelist=diff_set.whitelist(),
            read_file=lambda p: handle.show_file(head, p),
            linters=None,
        )
    )
    print("executed:", report.executed)
    print("skipped:", report.skipped)
    print("timed_out:", report.timed_out)
    print("raw_count:", report.raw_count)
    for f in report.findings:
        print("  finding:", f.tool, f.rule_id, f.path, f.line, f.severity, f.message[:60])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
