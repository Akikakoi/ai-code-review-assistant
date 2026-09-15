"""默认值一致性守卫。

有些默认值在代码里存在**三份**（全局设置 / 仓库配置视图 / DB 列默认值），
任何一份不一致都会造成"改了全局默认却看起来没生效"：

流水线优先用仓库配置，而仓库配置来自 DB 行或 `RepoConfigView` 的默认值。
所以只改 `Settings.acra_confidence_threshold` 是不够的 —— DB 那条会盖过去。

这类"静默失效"很难在代码审查里发现，因此用测试把它钉住：
**默认值必须一致，不一致就在这里失败。**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_core import PydanticUndefined

from acra.cli import overridden_acra_settings
from acra.settings import Settings
from acra.store.models import RepoConfig
from acra.store.repository import RepoConfigView


def declared_default(field: str):
    """字段的**声明默认值**。

    不要用 `Settings(_env_file=None)` 取"代码默认值" —— 它只关掉 `.env` 文件，
    环境变量照样会被读取，于是环境变量造成的覆盖会被误判成"没有覆盖"。
    """
    value = Settings.model_fields[field].default
    assert value is not PydanticUndefined, f"{field} 没有声明默认值"
    return value


def _db_column_default(column_name: str):
    column = RepoConfig.__table__.columns[column_name]
    return getattr(column.default, "arg", None)


@pytest.mark.parametrize(
    ("field", "column"),
    [
        ("confidence_threshold", "confidence_threshold"),
        ("max_comments", "max_comments"),
    ],
)
def test_repo_config_defaults_match_global_settings(field: str, column: str) -> None:
    view_value = getattr(RepoConfigView(), field)
    settings_value = declared_default(f"acra_{field}")
    db_value = _db_column_default(column)

    assert view_value == pytest.approx(settings_value), (
        f"RepoConfigView.{field}={view_value} 与 Settings.acra_{field}={settings_value} 不一致；"
        "仓库配置会盖过全局设置，改了全局默认会看起来没生效"
    )
    assert db_value == pytest.approx(settings_value), (
        f"RepoConfig 列默认值={db_value} 与 Settings.acra_{field}={settings_value} 不一致"
    )


def test_confidence_threshold_is_the_swept_value() -> None:
    """阈值是评估集扫出来的，不是拍的。

    20 用例真实模型跑分：0.65 → Precision 1.000/Recall 0.500；
    0.50 → Precision 1.000/Recall 0.750（精度零代价，召回 +25pp）。
    改这个值必须重新跑 `acra eval sweep-threshold` 并更新这里的断言。
    """
    assert declared_default("acra_confidence_threshold") == pytest.approx(0.50)


def test_verify_floor_stays_below_confidence_threshold() -> None:
    """进 Verify 的置信度下限必须低于丢弃门槛，否则"低置信但正确"的结论救不回来。"""
    assert declared_default("acra_verify_min_confidence") < declared_default(
        "acra_confidence_threshold"
    )


def test_overrides_detected_from_environment(monkeypatch) -> None:
    """回归：环境变量造成的覆盖必须能被检测出来。

    早期实现拿 `Settings(_env_file=None)` 当"代码默认值"参照，
    而它仍然读环境变量，于是两边一样、检测不出来 —— 覆盖机制形同虚设。
    """
    monkeypatch.setenv("ACRA_MAX_COMMENTS", "9")
    settings = Settings()
    overrides = dict(overridden_acra_settings(settings))
    assert overrides.get("acra_max_comments") == "9"


def test_no_overrides_reported_when_clean(monkeypatch) -> None:
    """干净环境下不报任何项。

    特别是 `acra_workdir`：它的校验器会把相对默认值 `.acra-work` 绝对化，
    直接比较会把它报成"被覆盖"。诊断输出里混进这种噪音，看的人很快就学会忽略整行。

    必须同时关掉 `.env`：只 `delenv` 只清环境变量，`.env` 文件照读 ——
    实测踩过：`.env` 里加了一行 `ACRA_GITHUB_TOKEN`，这条测试就红了。
    """
    for field in Settings.model_fields:
        if field.startswith("acra_"):
            monkeypatch.delenv(field.upper(), raising=False)
    assert overridden_acra_settings(Settings(_env_file=None)) == []


def test_credential_values_are_masked_in_doctor_output(monkeypatch) -> None:
    """doctor 要报"谁盖了默认值"，但**不能把凭据的值打出来**。

    实测踩过：把 `ACRA_GITHUB_TOKEN` 写进 `.env` 后，`acra doctor` 直接打印了整串
    token，连一次失败测试的 diff 里都带出了它 —— 终端、日志、CI 输出都是泄露面。
    """
    from acra.cli import MASKED

    secret = "github_pat_11AAAAA_zzzzzzzzzzzzzzzzzzzzzzzzzzzz"
    monkeypatch.setenv("ACRA_GITHUB_TOKEN", secret)
    overrides = dict(overridden_acra_settings(Settings(_env_file=None)))

    value = overrides.get("acra_github_token")
    assert value is not None, "被覆盖了却没报出来，等于把这一项藏了"
    assert secret not in value
    assert MASKED in value
    assert str(len(secret)) in value, "长度要留着，否则无法判断配的是哪一个"


def test_path_normalization_is_not_reported_as_override(monkeypatch) -> None:
    """相对默认值被绝对化 ≠ 被覆盖。"""
    monkeypatch.delenv("ACRA_WORKDIR", raising=False)
    settings = Settings()
    # 生效值是绝对路径，声明默认值是 .acra-work —— 两者等价
    assert settings.acra_workdir.is_absolute()
    assert declared_default("acra_workdir") == Path(".acra-work")
    assert "acra_workdir" not in dict(overridden_acra_settings(settings))


def test_real_workdir_override_is_reported(monkeypatch) -> None:
    """换成别的目录时**必须**报出来 —— 否则就只是把假阳性换成了漏报。"""
    monkeypatch.setenv("ACRA_WORKDIR", str(Path("/tmp/acra-other-workdir")))
    overrides = dict(overridden_acra_settings(Settings()))
    assert "acra_workdir" in overrides
    assert "acra-other-workdir" in overrides["acra_workdir"]
