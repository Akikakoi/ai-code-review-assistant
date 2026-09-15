"""把测试与 lint 结果写成文件，绕开工具输出偶发丢失的问题。

用法：
    ./.venv/Scripts/python.exe scripts/run_checks.py
结果写入 `.acra-work/checks_result.txt`（该目录已 gitignore，不会污染仓库根）。

为什么要这个脚本：在某些终端/沙箱环境下，pytest 的 stdout 末尾会被截断，
看不到 "N passed" 汇总行。这里改为读 JUnit XML，结论不受 stdout 影响。
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / ".acra-work"


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    python = sys.executable
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    junit = OUT_DIR / "junit.xml"
    results: dict[str, object] = {}

    rc_ruff, out_ruff = run([python, "-m", "ruff", "check", "src", "tests"])
    results["ruff"] = {"returncode": rc_ruff, "tail": out_ruff.strip().splitlines()[-3:]}

    run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"])
    suite = ET.parse(junit).getroot()
    if suite.tag != "testsuite":
        suite = suite.find("testsuite")
    results["pytest"] = {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "time_seconds": round(float(suite.get("time", 0)), 2),
    }

    (OUT_DIR / "checks_result.txt").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False))

    pytest_result = results["pytest"]
    ok = rc_ruff == 0 and pytest_result["failures"] == 0 and pytest_result["errors"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
