"""OpenAI 兼容协议的 LLM 客户端。

对应开发文档 §4.6（模型分级）、§8.4（结构约束、温度）、§4.2（失败与降级）。

设计要点：

- **不绑定 SDK**：直接用 httpx 打 `/chat/completions`。一是减少依赖与版本漂移，
  二是测试时可以用 respx 统一拦截 LLM 与平台 API 两类 HTTP 调用，离线可跑（文档 §14.2）。
- **模型档位来自配置**：调用方只给 phase（scan/verify/summary），代码里不出现模型名。
- **结构化输出优先、可降级**：先带 `response_format={"type":"json_object"}`，
  供应商不支持（400）时自动去掉重试，并"记住"该供应商不支持，后续调用不再尝试；
  解析层仍保留容错（剥 ```json 围栏、截取首个 `[` 到末个 `]`）。
- **思考 token 计入上限**：MiMo 等带 reasoning 的模型，reasoning token 会被计入
  `max_completion_tokens`。预算给小时会出现"只有思考、content 为空"且
  `finish_reason=length`，这里显式识别并给出可操作的错误信息。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from acra.errors import LLMResponseError, LLMUnavailable
from acra.models import estimate_tokens

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


# ---------------------------------------------------------------------------- 统计


@dataclass(slots=True)
class Usage:
    """token 与成本统计（文档 §13.3 指标里的 token / cost 维度）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0
    cost_micros: int = 0
    per_phase: dict[str, int] = field(default_factory=dict)

    def add(self, *, prompt: int, completion: int, cached: int, cost: int, phase: str) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += cached
        self.calls += 1
        self.cost_micros += cost
        self.per_phase[phase] = self.per_phase.get(phase, 0) + prompt + completion

    def merge(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls
        self.cost_micros += other.cost_micros
        for k, v in other.per_phase.items():
            self.per_phase[k] = self.per_phase.get(k, 0) + v

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_micros": self.cost_micros,
            "per_phase": dict(self.per_phase),
        }


@dataclass(slots=True)
class LLMResult:
    text: str
    model: str
    phase: str
    finish_reason: str = ""
    latency_ms: int = 0
    usage: Usage = field(default_factory=Usage)


def compute_cost(
    settings,
    *,
    prompt_tokens: int,
    completion_tokens: int,
) -> int:
    """成本（微美元）。单价未配置时记 0，不臆造价格。"""
    return int(
        prompt_tokens / 1_000_000 * settings.llm_input_price_micros_per_mtok
        + completion_tokens / 1_000_000 * settings.llm_output_price_micros_per_mtok
    )


# ---------------------------------------------------------------------------- 解析


def extract_json(text: str) -> Any:
    """从模型输出中尽力抽取 JSON。

    容错顺序：直接解析 → 剥 ```json 围栏 → 截取首个 `[`/`{` 到末个 `]`/`}`。
    """
    if text is None:
        raise LLMResponseError("模型返回为空")
    raw = text.strip()
    if not raw:
        raise LLMResponseError("模型返回为空字符串")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            raw = fence.group(1).strip()

    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise LLMResponseError(f"无法从模型输出中解析 JSON：{raw[:300]}")


# ---------------------------------------------------------------------------- 客户端


class LLMClient:
    def __init__(self, settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_api_base.rstrip("/"),
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=15.0),
            transport=transport,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )
        self.usage = Usage()
        #: 供应商不支持 response_format 时置位，后续调用不再尝试
        self._supports_json_mode = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ 核心调用

    async def chat(
        self,
        phase: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        model: str | None = None,
        max_completion_tokens: int | None = None,
    ) -> LLMResult:
        """一次 chat 调用，含指数退避重试（文档 §4.2：重试 2 次）。"""
        model = model or self.settings.model_for(phase)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_completion_tokens
            or self.settings.llm_max_completion_tokens,
        }
        if json_mode and self._supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        attempts = max(1, self.settings.llm_max_retries + 1)

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                last_error = LLMUnavailable(f"LLM 网络错误：{type(exc).__name__}: {exc}")
                await self._backoff(attempt)
                continue

            if resp.status_code == 400 and "response_format" in payload:
                # 供应商不支持结构化输出：去掉后立刻重试一次，并永久降级
                self._supports_json_mode = False
                payload.pop("response_format", None)
                last_error = LLMResponseError("供应商不支持 response_format，已降级为纯 JSON 提示")
                continue

            if resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500:
                last_error = LLMUnavailable(
                    f"LLM 返回 {resp.status_code}：{_safe_body(resp)}"
                )
                await self._backoff(attempt)
                continue

            if resp.status_code >= 400:
                raise LLMUnavailable(f"LLM 返回 {resp.status_code}：{_safe_body(resp)}")

            latency_ms = int((time.monotonic() - started) * 1000)
            return self._parse_response(phase, model, resp.json(), latency_ms)

        raise last_error or LLMUnavailable("LLM 调用失败")

    def _parse_response(self, phase: str, model: str, body: dict[str, Any], latency_ms: int) -> LLMResult:
        choices = body.get("choices") or []
        if not choices:
            raise LLMResponseError(f"响应缺少 choices：{str(body)[:300]}")
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = str(choice.get("finish_reason") or "")

        if not content.strip():
            if finish_reason == "length":
                raise LLMResponseError(
                    "模型只输出了思考内容、正文为空（finish_reason=length）。"
                    "该模型的 reasoning token 计入 max_completion_tokens，请调大 "
                    "LLM_MAX_COMPLETION_TOKENS。"
                )
            raise LLMResponseError(f"模型返回空内容（finish_reason={finish_reason}）")

        raw_usage = body.get("usage") or {}
        prompt_tokens = int(raw_usage.get("prompt_tokens") or 0)
        completion_tokens = int(raw_usage.get("completion_tokens") or 0)
        cached = int((raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

        usage = Usage()
        usage.add(
            prompt=prompt_tokens,
            completion=completion_tokens,
            cached=cached,
            cost=compute_cost(
                self.settings,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
            phase=phase,
        )
        self.usage.merge(usage)
        return LLMResult(
            text=content,
            model=body.get("model") or model,
            phase=phase,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            usage=usage,
        )

    async def complete_json(
        self,
        phase: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> tuple[Any, LLMResult]:
        """要求模型输出 JSON 并解析。"""
        result = await self.chat(phase, messages, temperature=temperature, json_mode=True, model=model)
        return extract_json(result.text), result

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(8.0, 1.0 * (2**attempt)))


def _safe_body(resp: httpx.Response) -> str:
    try:
        text = resp.text
    except Exception:  # pragma: no cover
        return "<unreadable>"
    return " ".join(text.split())[:400]


def count_prompt_tokens(messages: list[dict[str, str]]) -> int:
    """预估提示词 token（用于预算守卫，不依赖供应商返回）。"""
    return sum(estimate_tokens(m.get("content", "")) for m in messages)
