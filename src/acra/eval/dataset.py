"""评估数据集：用例定义、缺陷注入、JSONL 读写。

对应开发文档 §11.1 的两类语料：

| 来源 | 作用 | 本模块的对应 |
| --- | --- | --- |
| A 正例（真实 PR 里的真实缺陷） | 测漏报 | `load_jsonl` 导入（需平台 API 回溯，见 §11.1） |
| B 反例（真实 PR 里的非问题） | 测误报 | 同上 |
| C 缺陷注入（人工造缺陷、带 ground truth） | 精确测漏报率 | `BUILTIN_TEMPLATES` + `materialize` |

**为什么先做 C**：注入用例的 ground truth 行号是确定的，能给出可复现的 Recall 数字；
而 A/B 需要平台侧的历史 PR 与人工标注，是持续积累的过程（文档 §11.4 的 P0）。

JSONL 用例格式（可直接用 `acra eval run --dataset` 消费）：

```json
{"case_id": "java-null-check-inverted", "kind": "positive", "language": "java",
 "path": "src/main/java/com/example/Order.java", "base": "...", "head": "...",
 "expectations": [{"marker": "userId == null", "category": "bug", "severity_min": "high"}],
 "source": "builtin", "note": "空值校验被取反"}
```

`marker` 是 ground truth 的定位锚：在 `head` 里找到它的行号就是期望行号。
用"内容标记"而不是硬编码行号，模板被编辑后不会静默失准。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------- 数据结构


@dataclass(slots=True)
class Expectation:
    """一条 ground truth：期望在 head 的某一行上被发现。"""

    marker: str
    category: str = ""
    severity_min: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "marker": self.marker,
            "category": self.category,
            "severity_min": self.severity_min,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Expectation:
        return cls(
            marker=str(data.get("marker") or ""),
            category=str(data.get("category") or ""),
            severity_min=str(data.get("severity_min") or ""),
            note=str(data.get("note") or ""),
        )


@dataclass(slots=True)
class EvalCase:
    """一个可独立执行的评估用例：自带仓库内容与期望结论。"""

    case_id: str
    path: str
    base: str
    head: str
    expectations: list[Expectation] = field(default_factory=list)
    language: str = ""
    kind: str = "positive"
    source: str = "builtin"
    note: str = ""
    extra_files: dict[str, str] = field(default_factory=dict)

    @property
    def is_positive(self) -> bool:
        return self.kind == "positive"

    def expected_lines(self) -> list[int]:
        """按 marker 在 head 中定位期望行号（1-based）。"""
        lines = self.head.split("\n")
        out: list[int] = []
        for exp in self.expectations:
            for index, line in enumerate(lines, start=1):
                if exp.marker and exp.marker in line:
                    out.append(index)
                    break
        return out

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "language": self.language,
            "path": self.path,
            "base": self.base,
            "head": self.head,
            "expectations": [e.to_dict() for e in self.expectations],
            "source": self.source,
            "note": self.note,
            "extra_files": self.extra_files,
        }

    @classmethod
    def from_dict(cls, data: dict) -> EvalCase:
        return cls(
            case_id=str(data.get("case_id") or ""),
            path=str(data.get("path") or ""),
            base=str(data.get("base") or ""),
            head=str(data.get("head") or ""),
            expectations=[Expectation.from_dict(e) for e in data.get("expectations") or []],
            language=str(data.get("language") or ""),
            kind=str(data.get("kind") or "positive"),
            source=str(data.get("source") or "imported"),
            note=str(data.get("note") or ""),
            extra_files={str(k): str(v) for k, v in (data.get("extra_files") or {}).items()},
        )


def load_jsonl(path: Path | str) -> list[EvalCase]:
    """读取 JSONL 数据集。坏行跳过并计数，不因为一行脏数据丢掉整个数据集。"""
    cases: list[EvalCase] = []
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("case_id"):
            continue
        cases.append(EvalCase.from_dict(payload))
    return cases


def save_jsonl(cases: list[EvalCase], path: Path | str) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case.to_dict(), ensure_ascii=False) + "\n")
    return len(cases)


# ---------------------------------------------------------------------------- 注入模板

JAVA_PKG = "src/main/java/com/example/eval"
TS_DIR = "web/src"


def _java(name: str) -> str:
    return f"{JAVA_PKG}/{name}.java"


def _ts(name: str) -> str:
    return f"{TS_DIR}/{name}.ts"


#: 常见缺陷注入模板。每个模板都是"能编译、看起来正常、但确实有问题"的最小改动 ——
#: 刻意避免一眼可见的愚蠢错误，否则测出来的召回率没有参考价值。
BUILTIN_TEMPLATES: list[EvalCase] = [
    EvalCase(
        case_id="java-null-check-inverted",
        language="java",
        path=_java("PaymentService"),
        note="空值校验把 || 改成 &&，null 入参直接 NPE",
        base="""package com.example.eval;

public class PaymentService {

    public String pay(String userId) {
        if (userId == null || userId.isEmpty()) {
            throw new IllegalArgumentException("empty");
        }
        return "ok:" + userId;
    }
}
""",
        head="""package com.example.eval;

public class PaymentService {

    public String pay(String userId) {
        if (userId == null && userId.isEmpty()) {
            throw new IllegalArgumentException("empty");
        }
        return "ok:" + userId;
    }
}
""",
        expectations=[Expectation(marker="userId == null &&", category="bug", severity_min="high")],
    ),
    EvalCase(
        case_id="java-sql-injection",
        language="java",
        path=_java("OrderRepository"),
        note="字符串拼接构造 SQL（原实现用占位符）",
        base="""package com.example.eval;

import java.util.List;

public class OrderRepository {

    private final JdbcTemplate jdbc;

    public List<Order> findByStatus(String status) {
        return jdbc.query("select * from orders where status = ?", status);
    }
}
""",
        head="""package com.example.eval;

import java.util.List;

public class OrderRepository {

    private final JdbcTemplate jdbc;

    public List<Order> findByStatus(String status) {
        String sql = "select * from orders where status = '" + status + "'";
        return jdbc.query(sql);
    }
}
""",
        expectations=[Expectation(marker='where status = \'"', category="security", severity_min="high")],
    ),
    EvalCase(
        case_id="java-resource-leak",
        language="java",
        path=_java("FileExportService"),
        note="去掉 try-with-resources，流在异常路径不关闭",
        base="""package com.example.eval;

import java.io.FileInputStream;
import java.io.IOException;

public class FileExportService {

    public String export(String path) throws IOException {
        try (FileInputStream in = new FileInputStream(path)) {
            return new String(in.readAllBytes());
        }
    }
}
""",
        head="""package com.example.eval;

import java.io.FileInputStream;
import java.io.IOException;

public class FileExportService {

    public String export(String path) throws IOException {
        FileInputStream in = new FileInputStream(path);
        byte[] data = in.readAllBytes();
        return new String(data);
    }
}
""",
        expectations=[Expectation(marker="FileInputStream in = new FileInputStream", category="bug", severity_min="medium")],
    ),
    EvalCase(
        case_id="java-off-by-one",
        language="java",
        path=_java("PaginationUtil"),
        note="分页循环边界写成 < 导致少算一条",
        base="""package com.example.eval;

import java.util.List;

public class PaginationUtil {

    public int countInRange(List<Integer> values, int from, int to) {
        int total = 0;
        for (int i = from; i < to; i++) {
            total += values.get(i);
        }
        return total;
    }
}
""",
        head="""package com.example.eval;

import java.util.List;

public class PaginationUtil {

    public int countInRange(List<Integer> values, int from, int to) {
        int total = 0;
        for (int i = from; i < to - 1; i++) {
            total += values.get(i);
        }
        return total;
    }
}
""",
        expectations=[Expectation(marker="i < to - 1", category="bug", severity_min="medium")],
    ),
    EvalCase(
        case_id="java-check-then-act",
        language="java",
        path=_java("CouponService"),
        note="并发下 check-then-act：先查再发，存在重复发放窗口",
        base="""package com.example.eval;

import java.util.HashSet;
import java.util.Set;

public class CouponService {

    private final Set<String> claimed = new HashSet<>();

    public synchronized boolean claim(String userId) {
        if (claimed.contains(userId)) {
            return false;
        }
        claimed.add(userId);
        return true;
    }
}
""",
        head="""package com.example.eval;

import java.util.HashSet;
import java.util.Set;

public class CouponService {

    private final Set<String> claimed = new HashSet<>();

    public boolean claim(String userId) {
        if (claimed.contains(userId)) {
            return false;
        }
        claimed.add(userId);
        return true;
    }
}
""",
        expectations=[Expectation(marker="public boolean claim", category="concurrency", severity_min="medium")],
    ),
    EvalCase(
        case_id="java-swallowed-exception",
        language="java",
        path=_java("SyncService"),
        note="空 catch 吞掉异常，上游无法感知同步失败",
        base="""package com.example.eval;

public class SyncService {

    public void sync(String payload) {
        try {
            doSync(payload);
        } catch (RuntimeException e) {
            throw new IllegalStateException("sync failed", e);
        }
    }

    private void doSync(String payload) {
        // ...
    }
}
""",
        head="""package com.example.eval;

public class SyncService {

    public void sync(String payload) {
        try {
            doSync(payload);
        } catch (RuntimeException e) {
            // 忽略
        }
    }

    private void doSync(String payload) {
        // ...
    }
}
""",
        expectations=[Expectation(marker="// 忽略", category="bug", severity_min="medium")],
    ),
    EvalCase(
        case_id="java-hardcoded-secret",
        language="java",
        path=_java("MailClient"),
        note="密钥硬编码进源码",
        base="""package com.example.eval;

public class MailClient {

    private final String endpoint;

    public MailClient(String endpoint) {
        this.endpoint = endpoint;
    }

    public String endpoint() {
        return endpoint;
    }
}
""",
        head="""package com.example.eval;

public class MailClient {

    private static final String API_KEY = "sk-live-2f8a1c9d4b7e6035";

    private final String endpoint;

    public MailClient(String endpoint) {
        this.endpoint = endpoint;
    }

    public String endpoint() {
        return endpoint;
    }
}
""",
        expectations=[Expectation(marker="API_KEY = ", category="security", severity_min="high")],
    ),
    EvalCase(
        case_id="java-null-check-missing",
        language="java",
        path=_java("ProfileService"),
        note="新增的字段访问缺空值保护（上游可能返回 null）",
        base="""package com.example.eval;

public class ProfileService {

    public String displayName(Profile profile) {
        return "";
    }
}
""",
        head="""package com.example.eval;

public class ProfileService {

    public String displayName(Profile profile) {
        return profile.getNickname().trim();
    }
}
""",
        expectations=[Expectation(marker="profile.getNickname().trim()", category="bug", severity_min="medium")],
    ),
    # ---------------------------------------------------------------- 反例
    EvalCase(
        case_id="negative-plain-getter",
        kind="negative",
        language="java",
        path=_java("UserProfile"),
        note="反例：新增普通 getter，不该报任何问题",
        base="""package com.example.eval;

public class UserProfile {

    private String nickname;
}
""",
        head="""package com.example.eval;

public class UserProfile {

    private String nickname;

    public String getNickname() {
        return nickname;
    }
}
""",
        expectations=[],
    ),
    EvalCase(
        case_id="negative-local-rename",
        kind="negative",
        language="java",
        path=_java("MathUtil"),
        note="反例：仅重命名局部变量，行为不变",
        base="""package com.example.eval;

public class MathUtil {

    public int sum(int[] values) {
        int total = 0;
        for (int value : values) {
            total += value;
        }
        return total;
    }
}
""",
        head="""package com.example.eval;

public class MathUtil {

    public int sum(int[] values) {
        int acc = 0;
        for (int value : values) {
            acc += value;
        }
        return acc;
    }
}
""",
        expectations=[],
    ),
    EvalCase(
        case_id="negative-added-log",
        kind="negative",
        language="java",
        path=_java("AuditService"),
        note="反例：补充一行日志，不该报问题",
        base="""package com.example.eval;

public class AuditService {

    private final Logger log = LoggerFactory.getLogger(AuditService.class);

    public void audit(String action) {
        record(action);
    }

    private void record(String action) {
        // ...
    }
}
""",
        head="""package com.example.eval;

public class AuditService {

    private final Logger log = LoggerFactory.getLogger(AuditService.class);

    public void audit(String action) {
        log.info("audit {}", action);
        record(action);
    }

    private void record(String action) {
        // ...
    }
}
""",
        expectations=[],
    ),
    # ---------------------------------------------------------------- TypeScript
    EvalCase(
        case_id="ts-loose-equality",
        language="typescript",
        path=_ts("discount.ts"),
        note="用 == 比较，null 与 undefined 会被判等",
        base="""export function discount(rate: number | null): number {
  if (rate === null) {
    return 0;
  }
  return rate * 100;
}
""",
        head="""export function discount(rate: number | null): number {
  if (rate == null) {
    return 0;
  }
  return rate * 100;
}
""",
        expectations=[Expectation(marker="rate == null", category="bug", severity_min="low")],
    ),
    EvalCase(
        case_id="ts-missing-await",
        language="typescript",
        path=_ts("order.ts"),
        note="漏了 await，异常不会被捕获",
        base="""export async function submit(id: string): Promise<void> {
  try {
    await send(id);
  } catch (err) {
    report(err);
  }
}

async function send(id: string): Promise<void> {
  throw new Error("fail");
}

function report(err: unknown): void {
  void err;
}
""",
        head="""export async function submit(id: string): Promise<void> {
  try {
    send(id);
  } catch (err) {
    report(err);
  }
}

async function send(id: string): Promise<void> {
  throw new Error("fail");
}

function report(err: unknown): void {
  void err;
}
""",
        expectations=[Expectation(marker="    send(id);", category="bug", severity_min="medium")],
    ),
    EvalCase(
        case_id="ts-array-mutation",
        language="typescript",
        path=_ts("cart.ts"),
        note="直接改传入数组，副作用外泄",
        base="""export function withTax(items: number[], rate: number): number[] {
  return items.map((item) => item * (1 + rate));
}
""",
        head="""export function withTax(items: number[], rate: number): number[] {
  for (let i = 0; i < items.length; i++) {
    items[i] = items[i] * (1 + rate);
  }
  return items;
}
""",
        expectations=[Expectation(marker="items[i] = items[i]", category="bug", severity_min="low")],
    ),
    EvalCase(
        case_id="negative-ts-optional-chaining",
        kind="negative",
        language="typescript",
        path=_ts("format.ts"),
        note="反例：改用可选链，是健壮性提升而非缺陷",
        base="""export function city(user: { address?: { city?: string } }): string {
  if (user.address && user.address.city) {
    return user.address.city;
  }
  return "";
}
""",
        head="""export function city(user: { address?: { city?: string } }): string {
  return user.address?.city ?? "";
}
""",
        expectations=[],
    ),
]


def builtin_cases() -> list[EvalCase]:
    """早期手写的 15 个用例（保留作为最小回归集）。"""
    return [EvalCase.from_dict(c.to_dict()) for c in BUILTIN_TEMPLATES]


def injection_cases(*, variants: int = 3, negatives: bool = True) -> list[EvalCase]:
    """文档 §11.1 B 的缺陷注入语料：20 类 × 变体 + 反例。

    惰性导入 `defects`：它依赖本模块的数据结构，模块级互相导入会成环。
    """
    from acra.eval.defects import expand

    return expand(variants=variants, negatives=negatives)


def full_suite(
    *,
    variants: int = 3,
    negatives: bool = True,
    decoys: bool = True,
    static_probes: bool = True,
) -> list[EvalCase]:
    """默认评估语料 = 缺陷注入 + 防注入诱饵 + 静态可检出用例。

    - 诱饵用例**混在同一份数据集里跑**，而不是单独跑一轮：单跑容易被当成特殊场景，
      混跑才能反映"线上真的遇到注入时"的表现（见 `acra.eval.decoys`）；
    - 静态可检出用例同样混跑，但来源标成 `static_probe`，便于单独看静态层有没有真的工作
      （见 `acra.eval.static_cases` 对这组用例局限的说明）。
    """
    cases = injection_cases(variants=variants, negatives=negatives)
    if decoys:
        from acra.eval.decoys import decoy_cases

        cases = cases + decoy_cases()
    if static_probes:
        from acra.eval.static_cases import static_cases

        cases = cases + static_cases()
    return cases


def sample_cases(
    cases: list[EvalCase],
    size: int | None,
    *,
    seed: int = 20260914,
) -> list[EvalCase]:
    """分层抽样：按 **(来源, 正反例)** 分层，保证每一层都有人。

    两个坑都踩过一遍：

    1. 直接 `cases[:size]` 会把顺序靠前的模板全抽走，得到一个只有几类缺陷的样本；
    2. 只按正/反例分层**不够** —— 诱饵用例与静态探针各只有 20 / 1 条，
       小样本完全可能一条都抽不到，于是"防注入防护率"静默变成"没测"，
       而报告上看不出区别。

    因此按来源分层，并对每层至少取 1 条（层内不足则全取）。
    """
    import random

    if size is None or size >= len(cases):
        return list(cases)
    rng = random.Random(seed)

    strata: dict[tuple[str, bool], list[EvalCase]] = {}
    for case in cases:
        strata.setdefault((case.source, case.is_positive), []).append(case)

    ratio = size / len(cases)
    # 先给每层算名额（至少 1 条，保证每层都有人），再按"名额余数"从多到少补足到 size
    quotas: dict[tuple[str, bool], int] = {
        key: max(1, round(len(group) * ratio)) for key, group in strata.items()
    }
    while sum(quotas.values()) > size:
        # 超额时从最大的一层里减 —— 优先保住小层（诱饵、静态探针）
        biggest = max(quotas, key=lambda k: (quotas[k], len(strata[k])))
        if quotas[biggest] <= 1:
            break
        quotas[biggest] -= 1
    while sum(quotas.values()) < size:
        largest_group = max(strata, key=lambda k: len(strata[k]) - quotas[k])
        if len(strata[largest_group]) <= quotas[largest_group]:
            break
        quotas[largest_group] += 1

    chosen: list[EvalCase] = []
    for key, group in sorted(strata.items()):
        chosen.extend(rng.sample(group, min(quotas[key], len(group))))

    # 按 case_id 排序，保证同一 seed 下顺序稳定（便于跑批对比）
    return sorted(chosen, key=lambda c: c.case_id)


# ---------------------------------------------------------------------------- 物化

GIT_ID = (
    "-c",
    "user.email=acra-eval@test.local",
    "-c",
    "user.name=acra eval",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)


def _git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *GIT_ID, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _tracked_files(repo: Path) -> set[str]:
    proc = _git_run(repo, "ls-files")
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _case_files(case: EvalCase) -> set[str]:
    return {case.path, *case.extra_files}


def _reusable(repo: Path, case: EvalCase) -> bool:
    """这个目录能否直接复用（而不是另开一个新目录）。

    **case_id 决定了路径与内容**，所以同一个 case_id 的仓库可以反复复用：
    只要它跟踪的文件集合与用例声明的一致，就直接在上面追加两个新修订。
    文件集合不一致（换了数据集、某个 case 改了路径）就不复用 ——
    否则会残留上一次的文件、污染 diff。
    """
    if not (repo / ".git").exists():
        return False
    return _tracked_files(repo) == _case_files(case)


def _build_repo(repo: Path, case: EvalCase, *, initialize: bool = True) -> tuple[str, str]:
    """在 `repo` 里真实地建出 base→head 两个修订，返回 (base_sha, head_sha)。

    **刻意不用分支，只用 commit SHA。** 原因是踩过一个很隐蔽的环境问题：
    在部分环境里 `git checkout -b` 之后引用会"消失"，随后的 `commit` 变成一个
    root commit（说明 git 认为 HEAD 又回到了未出生状态），甚至返回码为 0 却什么都没建。
    由此产生的失败极难定位 —— 仓库看着建好了，只是引用解析不了。

    评估需要的其实只是"两个修订 + 它们之间的 diff"，分支是多余的中间物。
    SHA 指向 commit 对象，只要对象在就一定能解析，不依赖 ref 是否落盘。

    `initialize=False` 表示复用既有仓库：此时**不删除任何文件**，
    只在其历史上追加两个新修订。历史会累积，但每层只是一个小 delta，
    而"每跑一次评估就多一个目录"的无界增长被彻底消掉了 ——
    目录数从此收敛到"用例数"，与跑了多少轮无关。
    """
    def git(*args: str) -> str:
        proc = _git_run(repo, *args)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
        return proc.stdout.strip()

    def write(path: str, content: str) -> None:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def head_sha() -> str:
        proc = _git_run(repo, "rev-parse", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 else ""

    if initialize:
        git("init", "-q", "-b", "main")

    write(case.path, case.base)
    for rel, content in case.extra_files.items():
        write(rel, content)
    git("add", "-A")
    # `--allow-empty`：复用时首层内容可能与上一轮的 head 完全相同，此时没有可提交的差异。
    # 空提交照样给出一个合法且唯一的修订点，不会让整批用例失败。
    git("commit", "-q", "--allow-empty", "-m", "base")
    base_sha = head_sha()

    write(case.path, case.head)
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", f"feat: {case.note or case.case_id}")
    head = head_sha()

    if not base_sha or not head or base_sha == head:
        raise RuntimeError(f"两个修订未正确建立（base={base_sha[:8]!r} head={head[:8]!r}）")
    return base_sha, head


def materialize(case: EvalCase, root: Path, *, max_attempts: int = 100, retries: int = 2) -> tuple[Path, str, str]:
    """把用例写成一个真实的临时 git 仓库，返回 (repo, base_sha, head_sha)。

    刻意用真实 git 仓库而不是内存里的假 DiffSet：评估要覆盖 diff 解析、行号映射、
    上下文构建的整条链路，绕过它们测出来的指标不能代表线上。

    **优先复用 `root/<case_id>`，而不是每轮另开新目录。**
    起初的做法是"目录冲突就换新目录、从不删旧目录"（为了绕开删目录需要的确认），
    短期省事，长期是**无界增长**：跑 6 轮评估就堆出 500 多个目录、47MB。
    现在改成在既有仓库上追加修订（见 `_build_repo(initialize=False)`）——
    目录数收敛到"用例数"，与跑了多少轮无关，且全程不需要删任何东西。

    只有在文件集合对不上（换了数据集或某个用例改了路径）时才另开新目录，
    这种情况下旧目录会被原地保留，需要时手工清。`retries` 兜住偶发失败。
    """
    def locate(attempt_index: int) -> Path:
        base = case.case_id if attempt_index == 0 else f"{case.case_id}.retry{attempt_index}"
        candidate = root / base
        if _reusable(candidate, case):
            return candidate
        if not candidate.exists():
            return candidate
        # 文件集合对不上 → 找一个没被占用的新名字，不复用也不删除
        suffix = 0
        while candidate.exists() and suffix < max_attempts:
            suffix += 1
            candidate = root / f"{base}.{suffix}"
        return candidate

    last_error: Exception | None = None
    for attempt_index in range(max(1, retries)):
        repo = locate(attempt_index)
        reuse = _reusable(repo, case)
        repo.mkdir(parents=True, exist_ok=True)
        try:
            base_sha, head_sha = _build_repo(repo, case, initialize=not reuse)
        except Exception as exc:  # noqa: BLE001 - 换目录重试，仍失败才向上抛
            last_error = exc
            continue
        return repo, base_sha, head_sha
    raise RuntimeError(f"materialize 失败（已重试 {retries} 次）：{last_error}")
