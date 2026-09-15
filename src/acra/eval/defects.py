"""缺陷注入模板库：文档 §11.1 B 要求的"20 类常见缺陷"。

## 模板结构

一个模板拆成三段，避免 20 类缺陷写成几百行重复样板：

- `support`：类骨架（package、import、字段、辅助方法），其中 `// __BODY__`
  是**待注入方法体的插槽**；
- `base_method` / `head_method`：正确实现 / 注入缺陷后的实现，渲染时替换插槽；
- `marker`：head 里定位 ground truth 行的唯一子串。

插槽方案是必须的：如果骨架里也声明一遍同名方法，渲染出来就是重定义方法、
连编译都过不了。`_validate_templates()` 会在导入时检查这一点。

## 负例怎么来

反例不能靠"把注入撤掉"——那样 base 与 head 相同，压根没有 diff。
这里用**机械派生的良性变更**：只重命名方法名（不改类名，否则 Java 的
"public 类名必须与文件名一致"会引入一个真问题，反例就不再干净）。

选"重命名"当反例还有个额外好处：它恰好是最容易引发误报的场景之一 ——
模型很爱对着重命名发表意见（"建议命名更语义化"）。用它当反例，
测的是"能不能忍住不说废话"。

## 变体怎么来

`expand()` 按固定后缀改类名/方法名，把 20 个模板摊成上百个用例。
变体不引入新语义，价值在**统计力度**：锚定 0.98 这类高分位指标
需要上百个候选才有分辨率（11 个候选里丢 1 个就是 9 个百分点）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from acra.eval.dataset import EvalCase, Expectation

BUILTIN_PKG = "src/main/java/com/example/library"
#: 类骨架里替换方法体的插槽。放在行首、不带缩进 —— 方法体自带缩进，
#: 否则替换后会多出一层缩进，渲染结果看起来像格式错误但不报错。
BODY_SLOT = "__BODY__"


@dataclass(frozen=True, slots=True)
class DefectTemplate:
    """一个缺陷类别的模板。"""

    template_id: str
    title: str
    category: str
    severity_min: str
    class_name: str
    method_name: str
    support: str
    base_method: str
    head_method: str
    marker: str
    note: str = ""

    @property
    def path(self) -> str:
        return f"{BUILTIN_PKG}/{self.class_name}.java"

    @property
    def language(self) -> str:
        return "java"

    def render(self, *, class_name: str, method_name: str, defect: bool) -> str:
        if BODY_SLOT not in self.support:
            raise AssertionError(f"{self.template_id}: support 缺少 {BODY_SLOT!r} 插槽")
        body = (self.head_method if defect else self.base_method).replace(
            "__METHOD__", method_name
        )
        return self.support.replace("__CLASS__", class_name).replace(BODY_SLOT, body.rstrip())

    def marker_for(self, method_name: str) -> str:
        """某个变体的 ground truth 定位串。

        有的 marker 里含方法名（例如"丢掉 synchronized 的那一行"就是方法签名），
        所以取 marker 必须带上变体名 —— 直接比 `self.marker` 会在变体上定位失败。
        """
        return self.marker.replace("__METHOD__", method_name)


def _t(template_id: str, **kw) -> DefectTemplate:
    return DefectTemplate(template_id=template_id, **kw)


# ---------------------------------------------------------------------------- 20 类

TEMPLATES: tuple[DefectTemplate, ...] = (
    # 1
    _t(
        template_id="null-check-removed",
        title="去掉空值判断",
        category="bug",
        severity_min="medium",
        class_name="ProfileView",
        method_name="displayName",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__

    public record Profile(String nickname) {
    }
}""",
        base_method="""    public String __METHOD__(Profile profile) {
        if (profile == null) {
            return "unknown";
        }
        return profile.nickname();
    }""",
        head_method="""    public String __METHOD__(Profile profile) {
        return profile.nickname();
    }""",
        marker="return profile.nickname();",
        note="上游可能传 null，去掉判空后直接 NPE",
    ),
    # 2
    _t(
        template_id="condition-inverted",
        title="比较符号写反",
        category="bug",
        severity_min="high",
        class_name="OrderGate",
        method_name="accept",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public boolean __METHOD__(int total, int threshold) {
        return total >= threshold;
    }""",
        head_method="""    public boolean __METHOD__(int total, int threshold) {
        return total <= threshold;
    }""",
        marker="return total <= threshold;",
        note="门槛判断方向写反，所有超额订单都会被拒",
    ),
    # 3
    _t(
        template_id="null-check-inverted",
        title="空值校验逻辑写反",
        category="bug",
        severity_min="high",
        class_name="PaymentService",
        method_name="pay",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public String __METHOD__(String userId) {
        if (userId == null || userId.isEmpty()) {
            throw new IllegalArgumentException("empty");
        }
        return "ok:" + userId;
    }""",
        head_method="""    public String __METHOD__(String userId) {
        if (userId == null && userId.isEmpty()) {
            throw new IllegalArgumentException("empty");
        }
        return "ok:" + userId;
    }""",
        marker="userId == null && userId.isEmpty()",
        note="|| 改成 && 之后 null 不再被挡住",
    ),
    # 4
    _t(
        template_id="resource-release-removed",
        title="删除资源释放",
        category="bug",
        severity_min="medium",
        class_name="FileExportService",
        method_name="export",
        support="""package com.example.library;

import java.io.FileInputStream;
import java.io.IOException;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public String __METHOD__(String path) throws IOException {
        try (FileInputStream in = new FileInputStream(path)) {
            return new String(in.readAllBytes());
        }
    }""",
        head_method="""    public String __METHOD__(String path) throws IOException {
        FileInputStream in = new FileInputStream(path);
        byte[] data = in.readAllBytes();
        return new String(data);
    }""",
        marker="FileInputStream in = new FileInputStream(path);",
        note="去掉 try-with-resources，异常路径下句柄不释放",
    ),
    # 5
    _t(
        template_id="lock-scope-widened",
        title="改掉锁范围",
        category="concurrency",
        severity_min="medium",
        class_name="CounterService",
        method_name="incrementAndRender",
        support="""package com.example.library;

public class __CLASS__ {

    private int total;

__BODY__
}""",
        base_method="""    public String __METHOD__(int delta) {
        synchronized (this) {
            total += delta;
        }
        return "total=" + total;
    }""",
        head_method="""    public synchronized String __METHOD__(int delta) {
        total += delta;
        return "total=" + render(total);
    }

    private String render(int value) {
        return String.valueOf(value);
    }""",
        marker="public synchronized String __METHOD__(",
        note="锁范围从自增扩到整个方法，把渲染这类无关工作圈进临界区",
    ),
    # 6
    _t(
        template_id="transaction-scope-widened",
        title="扩大事务范围",
        category="performance",
        severity_min="low",
        class_name="BillingService",
        method_name="settle",
        support="""package com.example.library;

public class __CLASS__ {

    private final Mailer mailer = new Mailer();

__BODY__

    private void persist(String orderId) {
    }

    static class Mailer {
        void send(String orderId) {
        }
    }
}""",
        base_method="""    @Transactional
    public void __METHOD__(String orderId) {
        persist(orderId);
    }""",
        head_method="""    @Transactional
    public void __METHOD__(String orderId) {
        persist(orderId);
        mailer.send(orderId);
    }""",
        marker="mailer.send(orderId);",
        note="把发邮件这类慢操作圈进事务，长事务持有连接与锁",
    ),
    # 7
    _t(
        template_id="sql-injection",
        title="字符串拼接 SQL",
        category="security",
        severity_min="high",
        class_name="OrderRepository",
        method_name="findByStatus",
        support="""package com.example.library;

import java.util.List;

public class __CLASS__ {

    private final Jdbc jdbc = new Jdbc();

__BODY__

    static class Jdbc {
        List<String> query(String sql) {
            return List.of(sql);
        }

        List<String> query(String sql, String arg) {
            return List.of(sql, arg);
        }
    }
}""",
        base_method="""    public List<String> __METHOD__(String status) {
        return jdbc.query("select * from orders where status = ?", status);
    }""",
        head_method="""    public List<String> __METHOD__(String status) {
        String sql = "select * from orders where status = '" + status + "'";
        return jdbc.query(sql);
    }""",
        marker='where status = \'"',
        note="把占位符改成字符串拼接，status 来自用户输入",
    ),
    # 8
    _t(
        template_id="swallowed-exception",
        title="吞掉异常",
        category="bug",
        severity_min="medium",
        class_name="SyncService",
        method_name="sync",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__

    private void doSync(String payload) {
    }
}""",
        base_method="""    public void __METHOD__(String payload) {
        try {
            doSync(payload);
        } catch (RuntimeException e) {
            throw new IllegalStateException("sync failed: " + payload, e);
        }
    }""",
        head_method="""    public void __METHOD__(String payload) {
        try {
            doSync(payload);
        } catch (RuntimeException e) {
            // 忽略
        }
    }""",
        marker="// 忽略",
        note="空 catch 吞掉异常，上游无法感知同步失败",
    ),
    # 9
    _t(
        template_id="off-by-one",
        title="循环边界差一",
        category="bug",
        severity_min="medium",
        class_name="PaginationUtil",
        method_name="countInRange",
        support="""package com.example.library;

import java.util.List;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public int __METHOD__(List<Integer> values, int from, int to) {
        int total = 0;
        for (int i = from; i < to; i++) {
            total += values.get(i);
        }
        return total;
    }""",
        head_method="""    public int __METHOD__(List<Integer> values, int from, int to) {
        int total = 0;
        for (int i = from; i < to - 1; i++) {
            total += values.get(i);
        }
        return total;
    }""",
        marker="i < to - 1",
        note="上界减一导致每次少算最后一条",
    ),
    # 10
    _t(
        template_id="check-then-act",
        title="并发 check-then-act",
        category="concurrency",
        severity_min="medium",
        class_name="CouponService",
        method_name="claim",
        support="""package com.example.library;

import java.util.HashSet;
import java.util.Set;

public class __CLASS__ {

    private final Set<String> claimed = new HashSet<>();

__BODY__
}""",
        base_method="""    public synchronized boolean __METHOD__(String userId) {
        if (claimed.contains(userId)) {
            return false;
        }
        claimed.add(userId);
        return true;
    }""",
        head_method="""    public boolean __METHOD__(String userId) {
        if (claimed.contains(userId)) {
            return false;
        }
        claimed.add(userId);
        return true;
    }""",
        marker="public boolean __METHOD__(String userId)",
        note="去掉 synchronized，查与加之间出现重复发放窗口",
    ),
    # 11
    _t(
        template_id="hardcoded-secret",
        title="硬编码密钥",
        category="security",
        severity_min="high",
        class_name="MailClient",
        method_name="endpoint",
        support="""package com.example.library;

public class __CLASS__ {

    private final String endpoint;

    public __CLASS__(String endpoint) {
        this.endpoint = endpoint;
    }

__BODY__
}""",
        base_method="""    public String __METHOD__() {
        return endpoint;
    }""",
        head_method="""    public String __METHOD__() {
        String apiKey = "sk-live-2f8a1c9d4b7e6035";
        return endpoint + "?key=" + apiKey;
    }""",
        marker='apiKey = "sk-live-',
        note="把实时密钥写进源码",
    ),
    # 12
    _t(
        template_id="internal-collection-escaped",
        title="内部集合引用外泄",
        category="bug",
        severity_min="low",
        class_name="CartService",
        method_name="items",
        support="""package com.example.library;

import java.util.ArrayList;
import java.util.List;

public class __CLASS__ {

    private final List<String> items = new ArrayList<>();

__BODY__
}""",
        base_method="""    public List<String> __METHOD__() {
        return List.copyOf(items);
    }""",
        head_method="""    public List<String> __METHOD__() {
        return items;
    }""",
        marker="return items;",
        note="返回内部 List 的引用，调用方可以绕过封装改内部状态",
    ),
    # 13
    _t(
        template_id="integer-division",
        title="整数除法丢精度",
        category="bug",
        severity_min="low",
        class_name="TaxCalculator",
        method_name="rate",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public double __METHOD__(long part, long whole) {
        if (whole == 0) {
            return 0.0;
        }
        return (double) part / whole;
    }""",
        head_method="""    public double __METHOD__(long part, long whole) {
        if (whole == 0) {
            return 0.0;
        }
        return part / whole;
    }""",
        marker="return part / whole;",
        note="长整型整除，小数部分被截断",
    ),
    # 14
    _t(
        template_id="always-true-loop",
        title="循环条件恒真",
        category="bug",
        severity_min="high",
        class_name="RetryService",
        method_name="retry",
        support="""package com.example.library;

public class __CLASS__ {

    private static final int MAX_ATTEMPTS = 3;

__BODY__

    private boolean attempt(String action) {
        return action != null;
    }
}""",
        base_method="""    public int __METHOD__(String action) {
        int attempts = 0;
        while (attempts < MAX_ATTEMPTS) {
            if (attempt(action)) {
                return attempts;
            }
            attempts++;
        }
        return -1;
    }""",
        head_method="""    public int __METHOD__(String action) {
        int attempts = 0;
        while (attempts >= 0) {
            if (attempt(action)) {
                return attempts;
            }
            attempts++;
        }
        return -1;
    }""",
        marker="while (attempts >= 0)",
        note="条件恒真，失败时无限重试并把 attempts 加到溢出",
    ),
    # 15
    _t(
        template_id="sensitive-data-logged",
        title="日志打印敏感信息",
        category="security",
        severity_min="medium",
        class_name="AuthService",
        method_name="login",
        support="""package com.example.library;

public class __CLASS__ {

    private final Audit audit = new Audit();

__BODY__

    private boolean check(String user, String password) {
        return !password.isEmpty();
    }

    static class Audit {
        void record(String message) {
        }
    }
}""",
        base_method="""    public boolean __METHOD__(String user, String password) {
        audit.record("login attempt for " + user);
        return check(user, password);
    }""",
        head_method="""    public boolean __METHOD__(String user, String password) {
        audit.record("login attempt for " + user + " password=" + password);
        return check(user, password);
    }""",
        marker='" password=" + password',
        note="把明文口令写进审计日志",
    ),
    # 16
    _t(
        template_id="missing-return-branch",
        title="兜底分支返回 null",
        category="bug",
        severity_min="medium",
        class_name="StatusMapper",
        method_name="label",
        support="""package com.example.library;

public class __CLASS__ {

__BODY__
}""",
        base_method="""    public String __METHOD__(int code) {
        if (code == 200) {
            return "ok";
        }
        if (code == 404) {
            return "missing";
        }
        return "unknown";
    }""",
        head_method="""    public String __METHOD__(int code) {
        if (code == 200) {
            return "ok";
        }
        if (code == 404) {
            return "missing";
        }
        return null;
    }""",
        marker="return null;",
        note="兜底分支返回 null，调用方按非空处理时会 NPE",
    ),
    # 17
    _t(
        template_id="unbounded-batch-write",
        title="移除批量上限",
        category="performance",
        severity_min="low",
        class_name="BatchWriter",
        method_name="flush",
        support="""package com.example.library;

import java.util.List;

public class __CLASS__ {

    private static final int MAX_BATCH = 500;

    private final Store store = new Store();

__BODY__

    static class Store {
        void save(List<String> rows) {
        }
    }
}""",
        base_method="""    public int __METHOD__(List<String> rows) {
        int written = 0;
        for (int i = 0; i < rows.size(); i += MAX_BATCH) {
            List<String> slice = rows.subList(i, Math.min(i + MAX_BATCH, rows.size()));
            store.save(slice);
            written += slice.size();
        }
        return written;
    }""",
        head_method="""    public int __METHOD__(List<String> rows) {
        store.save(rows);
        return rows.size();
    }""",
        marker="store.save(rows);",
        note="去掉分批上限，一次性提交全部行",
    ),
    # 18
    _t(
        template_id="timezone-ignored",
        title="忽略时区",
        category="bug",
        severity_min="low",
        class_name="DayWindow",
        method_name="startOfDay",
        support="""package com.example.library;

import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;

public class __CLASS__ {

    private static final ZoneId ZONE = ZoneId.of("Asia/Shanghai");

__BODY__
}""",
        base_method="""    public Instant __METHOD__(LocalDate date) {
        return date.atStartOfDay(ZONE).toInstant();
    }""",
        head_method="""    public Instant __METHOD__(LocalDate date) {
        return date.atStartOfDay().toInstant(java.time.ZoneOffset.UTC);
    }""",
        marker="date.atStartOfDay().toInstant(",
        note="显式时区被换成 UTC，报表日界整体偏移 8 小时",
    ),
    # 19
    _t(
        template_id="cache-key-collision",
        title="缓存键冲突",
        category="bug",
        severity_min="medium",
        class_name="ConfigCache",
        method_name="get",
        support="""package com.example.library;

public class __CLASS__ {

    private final Cache cache = new Cache();

__BODY__

    static class Cache {
        String get(String name) {
            return name;
        }
    }
}""",
        base_method="""    public String __METHOD__(String tenant, String key) {
        return cache.get("cfg." + tenant + "." + key);
    }""",
        head_method="""    public String __METHOD__(String tenant, String key) {
        return cache.get("cfg." + key);
    }""",
        marker='cache.get("cfg." + key)',
        note="缓存键丢掉 tenant，多租户之间互相读到别人的配置",
    ),
    # 20
    _t(
        template_id="retry-without-idempotency",
        title="重试非幂等操作",
        category="bug",
        severity_min="medium",
        class_name="ChargeService",
        method_name="charge",
        support="""package com.example.library;

public class __CLASS__ {

    private final Gateway gateway = new Gateway();

__BODY__

    static class Gateway {
        String charge(String orderId) {
            return orderId;
        }

        String charge(String orderId, String idempotencyKey) {
            return orderId + idempotencyKey;
        }
    }
}""",
        base_method="""    public String __METHOD__(String orderId) {
        return gateway.charge(orderId, "order-" + orderId);
    }""",
        head_method="""    public String __METHOD__(String orderId) {
        for (int i = 0; i < 3; i++) {
            try {
                return gateway.charge(orderId);
            } catch (RuntimeException e) {
                // 重试
            }
        }
        return null;
    }""",
        marker="gateway.charge(orderId);",
        note="重试扣款却没有幂等键，超时重试会重复扣款",
    ),
)


# ---------------------------------------------------------------------------- 展开

#: 变体用的类名后缀池
_SUFFIXES = ("Alpha", "Bravo", "Chase", "Delta", "Echo", "Falcon")


def _variant_names(template: DefectTemplate, index: int) -> tuple[str, str]:
    if index == 0:
        return template.class_name, template.method_name
    suffix = _SUFFIXES[(index - 1) % len(_SUFFIXES)]
    return f"{template.class_name}{suffix}", f"{template.method_name}{suffix}"


def expand(
    templates: tuple[DefectTemplate, ...] = TEMPLATES,
    *,
    variants: int = 3,
    negatives: bool = True,
) -> list[EvalCase]:
    """把模板摊成可执行用例。

    - 每个模板产出 `variants` 个正例（第 0 个用原名，其余加后缀）；
    - `negatives=True` 时每个模板再产出 1 个反例：**只重命名方法名**的良性变更。
    """
    cases: list[EvalCase] = []
    for template in templates:
        for index in range(max(1, variants)):
            class_name, method_name = _variant_names(template, index)
            case_id = template.template_id if index == 0 else f"{template.template_id}-{index}"
            cases.append(
                EvalCase(
                    case_id=case_id,
                    path=template.path.replace(template.class_name, class_name),
                    base=template.render(
                        class_name=class_name, method_name=method_name, defect=False
                    ),
                    head=template.render(
                        class_name=class_name, method_name=method_name, defect=True
                    ),
                    expectations=[
                        Expectation(
                            marker=template.marker_for(method_name),
                            category=template.category,
                            severity_min=template.severity_min,
                            note=template.note,
                        )
                    ],
                    language=template.language,
                    kind="positive",
                    source="injection",
                    note=template.title,
                )
            )

        if negatives:
            # 只改方法名：类名与文件名保持一致，避免引入"public 类名与文件名不符"这个真问题
            renamed = f"{template.class_name}Renamed"
            cases.append(
                EvalCase(
                    case_id=f"negative-{template.template_id}",
                    path=template.path.replace(template.class_name, renamed),
                    base=template.render(
                        class_name=renamed,
                        method_name=template.method_name,
                        defect=False,
                    ),
                    head=template.render(
                        class_name=renamed,
                        method_name=f"{template.method_name}V2",
                        defect=False,
                    ),
                    expectations=[],
                    language=template.language,
                    kind="negative",
                    source="injection",
                    note=f"反例：仅重命名 {template.method_name}",
                )
            )
    return cases


def template_ids() -> list[str]:
    return [t.template_id for t in TEMPLATES]


# ---------------------------------------------------------------------------- 导入时自检


def _validate_templates() -> None:
    """模板写错会让整份评估失真，因此在导入时就检查。

    检查三件事：
    1. `marker` 在 head 中**唯一** —— 否则 ground truth 行号会定位到错的行；
    2. 变体方法名只声明**一次** —— 重复声明说明 support 里也写了方法体，
       渲染出来的文件根本编译不过；
    3. base 与 head 必须真的不同，且必须能编译得了（有 package 与类声明）。
    """
    if len(TEMPLATES) < 20:
        raise AssertionError(f"模板数 {len(TEMPLATES)} 少于文档 §11.1 要求的 20 类")

    ids = [t.template_id for t in TEMPLATES]
    if len(ids) != len(set(ids)):
        duplicates = [i for i in ids if ids.count(i) > 1]
        raise AssertionError(f"模板 id 重复：{sorted(set(duplicates))}")

    for template in TEMPLATES:
        base = template.render(
            class_name=template.class_name,
            method_name=template.method_name,
            defect=False,
        )
        head = template.render(
            class_name=template.class_name,
            method_name=template.method_name,
            defect=True,
        )
        if base == head:
            raise AssertionError(f"{template.template_id}: base 与 head 相同，没有 diff")

        marker = template.marker_for(template.method_name)
        hits = len(re.findall(re.escape(marker), head))
        if hits != 1:
            raise AssertionError(
                f"{template.template_id}: marker 在 head 中出现 {hits} 次，应为 1 次"
            )

        # 只数"带访问修饰符的声明"，不数调用：`cache.get(...)` 这类调用不该被算进来，
        # 也不数 support 里同类名方法的无修饰符声明（包私有辅助方法很常见）。
        declarations = len(
            re.findall(
                rf"(?:public|private|protected)\s+[^\n(]*\b{re.escape(template.method_name)}\s*\(",
                base,
            )
        )
        if declarations != 1:
            raise AssertionError(
                f"{template.template_id}: 方法 {template.method_name} 被声明 {declarations} 次，"
                "应为 1 次（>1 说明 support 与 body 重复声明）"
            )
        if "package " not in base or f"class {template.class_name}" not in base:
            raise AssertionError(f"{template.template_id}: 渲染结果缺少 package 或类声明")


_validate_templates()
