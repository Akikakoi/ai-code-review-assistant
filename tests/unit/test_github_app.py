"""GitHub App 认证与 publisher 凭据解析。

这组测试覆盖的是"发布链路能不能拿到凭据"——它此前完全没有实现，
因此 `GithubPublisher` 虽然单测齐全，却没有任何入口能构造出它。

重点断言的是安全与刷新语义，而不只是"请求发出去了"：
- App JWT 的 `iat` 必须回拨（否则 GitHub 判 "iat in the future"）、`exp` 不得超上限；
- 换 token 的请求头里必须是 App JWT，**不是** installation token；
- token 在到期前一段时间就要换新，不能等它失效。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from acra.github_app import (
    JWT_CLOCK_SKEW_SECONDS,
    JWT_TTL_SECONDS,
    GithubAppAuth,
    GithubAppError,
)
from acra.models import ReviewJob
from acra.publish.factory import (
    SOURCE_APP,
    SOURCE_NONE,
    SOURCE_STATIC,
    GithubAccess,
    describe_credentials,
    open_publisher,
    reset_app_auth_cache,
    resolve_access,
)

API = "https://api.github.test"

_PRIVATE_KEY_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    .decode()
)

_NOW = 1_800_000_000.0  # 固定时钟，用于断言 iat/exp


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "app.pem"
    path.write_text(_PRIVATE_KEY_PEM, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_app_auth_cache()
    yield
    reset_app_auth_cache()


def _job(installation_id=None) -> ReviewJob:
    return ReviewJob(
        job_id="j1",
        platform="github",
        repo_full_name="o/r",
        repo_path="https://x/y.git",
        remote_url="https://x/y.git",
        pr_number=7,
        installation_id=installation_id,
    )


# ---------------------------------------------------------------------------- JWT


def test_app_jwt_has_skew_backdated_iat_and_bounded_exp(key_file) -> None:
    auth = GithubAppAuth("12345", str(key_file), api_base=API)
    token = auth.app_jwt(now=_NOW)
    claims = jwt.decode(token, options={"verify_signature": False})

    assert claims["iss"] == "12345"
    # 回拨 60s：客户端时钟快于 GitHub 时，不回拨会被判 iat 在未来
    assert claims["iat"] == int(_NOW) - JWT_CLOCK_SKEW_SECONDS
    # GitHub 硬限制 10 分钟，不能超
    assert claims["exp"] - claims["iat"] <= 600
    assert claims["exp"] == int(_NOW) + JWT_TTL_SECONDS


def test_missing_app_id_or_key_path_raises(key_file) -> None:
    with pytest.raises(GithubAppError, match="GITHUB_APP_ID"):
        GithubAppAuth("", str(key_file))
    with pytest.raises(GithubAppError, match="GITHUB_APP_PRIVATE_KEY_PATH"):
        GithubAppAuth("1", "")


def test_missing_key_file_reports_path_only(tmp_path) -> None:
    missing = tmp_path / "nope.pem"
    auth = GithubAppAuth("1", str(missing), api_base=API)
    with pytest.raises(GithubAppError) as exc:
        auth.app_jwt(now=_NOW)
    assert "nope.pem" in str(exc.value)


# ---------------------------------------------------------------------------- token


@respx.mock
async def test_installation_token_uses_app_jwt_and_caches(key_file) -> None:
    route = respx.post(f"{API}/app/installations/999/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_abc", "expires_at": "2100-01-01T00:00:00Z"},
        )
    )
    auth = GithubAppAuth("1", str(key_file), api_base=API)

    first = await auth.installation_token(999, now=_NOW)
    second = await auth.installation_token(999, now=_NOW)

    assert first == second == "ghs_abc"
    # 第二次必须命中缓存：每来一个任务都换一次 token 会撞限流
    assert route.call_count == 1

    sent = route.calls[0].request.headers["Authorization"]
    assert sent.startswith("Bearer ")
    claims = jwt.decode(sent.removeprefix("Bearer "), options={"verify_signature": False})
    assert claims["iss"] == "1"  # 换 token 用的是 App JWT，不是 installation token


@respx.mock
async def test_installation_token_refreshes_before_expiry(key_file) -> None:
    """token 必须在**到期前**换新，而不是等它失效 —— 否则长任务会在中途断掉。"""
    valid_until = _NOW + 3600
    expires_iso = (
        datetime.fromtimestamp(valid_until, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    route = respx.post(f"{API}/app/installations/999/access_tokens").mock(
        side_effect=[
            httpx.Response(201, json={"token": "first", "expires_at": expires_iso}),
            httpx.Response(201, json={"token": "second", "expires_at": expires_iso}),
        ]
    )
    auth = GithubAppAuth("1", str(key_file), api_base=API)

    assert await auth.installation_token(999, now=_NOW) == "first"
    # 还早得很：必须命中缓存
    assert await auth.installation_token(999, now=_NOW + 60) == "first"
    assert route.call_count == 1

    # 进入刷新余量区间（剩余 100s < REFRESH_MARGIN_SECONDS）→ 必须换新
    assert await auth.installation_token(999, now=valid_until - 100) == "second"
    assert route.call_count == 2


@respx.mock
async def test_installation_id_falls_back_to_configured_default(key_file) -> None:
    respx.post(f"{API}/app/installations/55/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": "2100-01-01T00:00:00Z"})
    )
    auth = GithubAppAuth("1", str(key_file), api_base=API, installation_id="55")
    assert await auth.installation_token(now=_NOW) == "t"


async def test_missing_installation_id_raises(key_file) -> None:
    auth = GithubAppAuth("1", str(key_file), api_base=API)
    with pytest.raises(GithubAppError, match="installation id"):
        await auth.installation_token(now=_NOW)


@respx.mock
async def test_token_exchange_failure_is_reported(key_file) -> None:
    respx.post(f"{API}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(404, text='{"message":"Not Found"}')
    )
    auth = GithubAppAuth("1", str(key_file), api_base=API)
    with pytest.raises(GithubAppError, match="404"):
        await auth.installation_token(1, now=_NOW)


# ---------------------------------------------------------------------------- 凭据解析


def _settings(**kw) -> SimpleNamespace:
    base = {
        "acra_github_token": "",
        "github_api_base": API,
        "github_app_id": "",
        "github_app_private_key_path": "",
        "github_app_installation_id": "",
    }
    base.update(kw)
    return SimpleNamespace(**base)


async def test_static_token_takes_precedence() -> None:
    access = await resolve_access(_settings(acra_github_token="ghp_x"), _job())
    assert access.available
    assert access.source == SOURCE_STATIC
    assert access.token == "ghp_x"


async def test_no_credentials_yields_unavailable_not_exception() -> None:
    access = await resolve_access(_settings(), _job())
    assert not access.available
    assert access.source == SOURCE_NONE
    assert access.error is None


@respx.mock
async def test_app_credentials_are_resolved_from_job_installation(key_file) -> None:
    respx.post(f"{API}/app/installations/777/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_job", "expires_at": "2100-01-01T00:00:00Z"})
    )
    settings = _settings(github_app_id="1", github_app_private_key_path=str(key_file))
    access = await resolve_access(settings, _job(installation_id=777))
    assert access.available
    assert access.source == SOURCE_APP
    assert access.installation_id == 777


async def test_app_failure_is_reported_rather_than_raised(key_file) -> None:
    # 私钥文件不存在 → 不该把整次审查炸掉，而应降级成"不发布"并留下原因
    settings = _settings(github_app_id="1", github_app_private_key_path=str(key_file) + ".oops")
    access = await resolve_access(settings, _job(installation_id=1))
    assert not access.available
    assert access.error and "私钥文件不存在" in access.error


async def test_open_publisher_skips_when_no_token() -> None:
    access = await resolve_access(_settings(), _job())
    async with open_publisher(access) as publisher:
        assert publisher is None


@respx.mock
async def test_open_publisher_closes_client(key_file) -> None:
    respx.post(f"{API}/app/installations/1/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "t", "expires_at": "2100-01-01T00:00:00Z"})
    )
    settings = _settings(github_app_id="1", github_app_private_key_path=str(key_file))
    access = await resolve_access(settings, _job(installation_id=1))
    async with open_publisher(access) as publisher:
        assert publisher is not None
        client = publisher._client
    assert client.is_closed


def test_access_is_json_friendly_for_logging() -> None:
    # 日志与总结里会引用 source / installation_id，字段改名必须被这个测试挡住
    payload = asdict(GithubAccess())
    assert set(payload) == {"token", "source", "installation_id", "api_base", "error"}
    json.dumps(payload)


# ---------------------------------------------------------------------------- doctor 口径


def test_describe_credentials_reports_each_state(key_file, tmp_path) -> None:
    """doctor 的这一行要能回答"到底会不会有评论发出去"，四种状态都必须可区分。"""
    source, detail = describe_credentials(_settings())
    assert source == SOURCE_NONE
    assert "不会发布评论" in detail

    source, detail = describe_credentials(_settings(acra_github_token="ghp_x"))
    assert source == SOURCE_STATIC

    base = {"github_app_id": "1", "github_app_private_key_path": str(tmp_path / "missing.pem")}
    source, detail = describe_credentials(_settings(**base))
    assert source == SOURCE_APP
    assert "私钥文件不存在" in detail

    source, detail = describe_credentials(
        _settings(github_app_id="1", github_app_private_key_path=str(key_file))
    )
    assert source == SOURCE_APP
    assert "INSTALLATION_ID" in detail

    source, detail = describe_credentials(
        _settings(
            github_app_id="1",
            github_app_private_key_path=str(key_file),
            github_app_installation_id="42",
        )
    )
    assert source == SOURCE_APP
    assert "42" in detail and "不会发布评论" not in detail
