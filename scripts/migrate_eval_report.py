"""把既有评估报告迁移到新的指标口径。

背景：`EvalMetrics` 后来补了 `anchor_candidates` / `anchor_dropped` / `sample_adequate`
等字段（为了暴露"样本量不足以判定 0.98 这种精度"这件事）。旧报告文件里没有这些字段，
重新渲染时会显示成 0/0，看起来像数据丢了。

这里用**同一份逐用例数据**和**同一个 compute()**重算指标，因此结果与原始运行一致，
只是补齐了新字段。不做任何猜测性填充。

用法：
    python scripts/migrate_eval_report.py eval/report-v1.json [更多文件...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acra.eval.metrics import CaseOutcome, compute  # noqa: E402


def migrate(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") or []
    if not cases:
        return f"{path.name}: 没有逐用例数据，跳过"

    outcomes = [
        CaseOutcome(
            case_id=str(c.get("case_id") or ""),
            expected=bool(c.get("expected", True)),
            expected_lines=list(c.get("expected_lines") or []),
            reported=[
                (str(r.get("path") or ""), int(r.get("line") or 0), str(r.get("category") or ""))
                for r in c.get("reported") or []
            ],
            raw_candidates=int(c.get("raw_candidates") or 0),
            anchor_dropped=int(c.get("anchor_dropped") or 0),
            duration_ms=int(c.get("duration_ms") or 0),
            error=c.get("error"),
            verify_enabled=bool(c.get("verify_enabled")),
            verify_rejection_rate=float(c.get("verify_rejection_rate") or 0.0),
        )
        for c in cases
    ]
    metrics = compute(outcomes)

    # 保留原始 metrics 里的 verify 相关信息（runner 逐用例没存该项时靠它兜底）
    old = payload.get("metrics") or {}
    new = metrics.to_dict()
    if not new["verify_rejection_rate"] and old.get("verify_rejection_rate"):
        new["verify_rejection_rate"] = old["verify_rejection_rate"]

    changed = {
        key: (old.get(key), new[key])
        for key in new
        if key in ("anchor_validity", "anchor_candidates", "anchor_dropped")
    }
    payload["metrics"] = new
    payload["migrated_note"] = "本文件的 metrics 由逐用例数据重算补齐（口径见 acra/eval/metrics.py）"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    detail = "，".join(f"{k}: {a} → {b}" for k, (a, b) in changed.items())
    return f"{path.name}: 已迁移（{detail}）"


def main() -> int:
    targets = sys.argv[1:]
    if not targets:
        print("用法: python scripts/migrate_eval_report.py <报告.json> [...]")
        return 2
    for target in targets:
        print(migrate(Path(target)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
