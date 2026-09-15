"""集中配置。

所有配置项通过环境变量 / `.env` 注入，代码中不硬编码模型名、阈值与路径。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 平台接入 ----------------
    github_app_id: str = ""
    github_app_private_key_path: str = ""
    #: GitHub API 根地址。除 GitHub.com 外还用于 GHES；也让发布链路可被指向测试端点。
    github_api_base: str = "https://api.github.com"
    #: App 安装 ID。webhook 事件里自带（`installation.id`）；CLI 本地发布时只能从这里取。
    github_app_installation_id: str = ""
    github_webhook_secret: str = ""
    #: 静态 token。**只用于本地 / CI 做一次性端到端验证**，生产走 App installation token。
    #: 存在的意义是让"发布链路能不能通"这件事可以在没有 App 的环境里被验证。
    acra_github_token: str = ""
    # 手动触发 / 管理接口的共享令牌（阶段一仅 CLI，留空则管理接口关闭）
    acra_admin_token: str = ""

    # ---------------- 模型 ----------------
    llm_api_base: str = "https://api.xiaomimimo.com/v1"
    llm_api_key: str = ""
    llm_scan_model: str = "mimo-v2.5-pro"
    llm_verify_model: str = "mimo-v2.5-pro"
    llm_summary_model: str = "mimo-v2.5-pro"
    llm_timeout_seconds: int = 60
    # MiMo 等带思考的模型会把 reasoning token 计入 max_completion_tokens，故留足预算
    llm_max_completion_tokens: int = 8192
    llm_max_retries: int = 2
    #: 单价，单位都是 **微元 / 1M tokens**（1 微元 = 1e-6 元）。
    #: 刻意用人民币：供应商按人民币计价，若这里写成美元就要引入一个隐形的汇率常数，
    #: 而 summary 里打印的是"元"—— 两边对不上时没有任何地方会报错。
    llm_input_price_micros_per_mtok: int = 0
    llm_output_price_micros_per_mtok: int = 0
    #: 缓存命中的输入价。两档价差极大（MiMo-V2.5-Pro：¥3.00 vs ¥0.025 / 1M），
    #: 不单独配就会把命中部分按全价算，成本被严重高估。
    #: 留空则按全额输入价计（保守方向，不会让预算守卫失效）。
    llm_cached_input_price_micros_per_mtok: int = 0

    # ---------------- 存储 ----------------
    database_url: str = ""
    redis_url: str = ""

    # ---------------- 行为 ----------------
    acra_max_comments: int = 5
    #: 置信度门槛（§9.1 第 8 步）。低于它的候选不透出。
    #:
    #: **0.50 是用评估集扫出来的，不是拍的。** 20 用例真实模型跑分上的阈值扫描：
    #:   0.65（旧默认）→ Precision 1.000 / Recall 0.500
    #:   0.50（现默认）→ Precision 1.000 / Recall 0.750
    #: 精度零代价、召回 +25 个百分点，约为实测跑批波动（±6.2pp）的 4 倍，属真实提升。
    #: 再降到 0.40 能换到 Recall 0.875，但出现 1 条误报（Precision 0.933）——
    #: 16 个候选的样本量不足以判定这 7 个百分点是否真实，需要更大语料再验。
    #: 复现：`acra eval sweep-threshold --report eval/baseline-a.json`
    acra_confidence_threshold: float = 0.50
    acra_context_level_max: int = 2
    acra_context_context_lines: int = 3
    acra_daily_budget_micros: int = 5_000_000
    acra_total_input_token_budget: int = 200_000
    acra_chunk_input_token_budget: int = 16_000
    acra_max_changed_lines: int = 3000
    acra_max_changed_files: int = 80
    acra_scan_concurrency: int = 4
    acra_sandbox_image: str = "acra-sandbox:latest"
    acra_sandbox_enabled: bool = False
    acra_shadow_mode: bool = False
    acra_workdir: Path = Field(default=Path(".acra-work"))
    #: web 进程内直接消费队列（阶段一单机部署）。分离部署时置 false，改用 `acra worker`
    acra_inline_worker: bool = True

    # ---------------- 阶段二 ----------------
    #: L2 是否注入"被引用类型的签名"（不含实现体）。阶段二默认开启。
    acra_l2_type_signatures: bool = True
    #: 符号索引的单文件预算：变更文件自身 + 其 import 解析出的定义文件
    acra_l2_max_index_files: int = 40
    #: 仓库文件数超过该值就不建文件索引（避免超大仓库上多花一次 ls-tree）
    acra_l2_max_repo_files: int = 20_000

    #: 静态分析总开关。默认开启；工具没装或没配置规则集时逐项跳过并写进降级说明，
    #: 不会因为环境缺工具而让整条链路失败。
    acra_static_analysis_enabled: bool = True
    acra_static_timeout_seconds: int = 120
    #: diff-aware 过滤后注入提示词的静态结论上限（省 token）
    acra_static_max_findings: int = 60
    #: Semgrep 规则集；留空则不跑 Semgrep
    acra_semgrep_config: str = "p/security-audit"

    #: 两阶段 Verify（收敛）。默认关闭：它让延迟与成本翻倍，应当先用评估集量出
    #: Precision 与驳回率的实际收益，再决定是否默认开启（文档 §10.1 的成本纪律）。
    acra_verify_enabled: bool = False
    #: Verify 的调用次数与总时长预算（文档 §10.3：Verify 内部串行，便于时间预算生效）
    acra_verify_max_candidates: int = 20
    acra_verify_budget_seconds: int = 90
    #: 低于该置信度的候选不进 Verify —— 连 Scan 自己都没把握的候选，不值得再花一次调用。
    #: 必须明显低于 acra_confidence_threshold，否则会掐掉"低置信但正确、本该被救回"的结论。
    acra_verify_min_confidence: float = 0.4

    # ---------------- 观测 ----------------
    otel_exporter_otlp_endpoint: str = ""
    log_level: str = "INFO"

    @field_validator("acra_workdir", mode="after")
    @classmethod
    def _absolutize_workdir(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_base)

    def model_for(self, phase: str) -> str:
        """阶段 → 模型档位。所有档位来自配置，代码不硬编码模型名。"""
        return {
            "scan": self.llm_scan_model,
            "verify": self.llm_verify_model,
            "summary": self.llm_summary_model,
        }.get(phase, self.llm_scan_model)

    def ensure_workdir(self) -> Path:
        self.acra_workdir.mkdir(parents=True, exist_ok=True)
        return self.acra_workdir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清理配置缓存。"""
    get_settings.cache_clear()
