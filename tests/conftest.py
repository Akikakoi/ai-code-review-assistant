"""共用夹具。

所有测试都**不读 `.env`**（`_env_file=None`），避免本机配置影响断言。

这里显式关掉静态分析：真实运行默认开启它，但测试环境里不该去调本机可能装着的
semgrep / eslint / ruff —— 那会让测试既慢又不确定。静态分析自己的测试用注入的
假 runner（见 `tests/unit/test_static_runner.py`）。
"""

from __future__ import annotations

import pytest

from acra.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        llm_api_key="test-key",
        llm_api_base="https://api.test.local/v1",
        llm_scan_model="scan-model",
        llm_verify_model="verify-model",
        llm_summary_model="summary-model",
        llm_timeout_seconds=10,
        llm_max_retries=0,
        llm_max_completion_tokens=2048,
        acra_workdir=tmp_path / "work",
        acra_scan_concurrency=2,
        acra_max_comments=5,
        acra_confidence_threshold=0.65,
        acra_static_analysis_enabled=False,
        acra_verify_enabled=False,
        database_url="",
        redis_url="",
    )
