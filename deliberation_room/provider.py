"""Provider discovery, model catalog, and completion adapters for Deliberation Room."""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence
from urllib import error, request

from .domain import CompletionResult, CompletionStatus, JSONDict


MessageParam = Mapping[str, Any]


class KeySource(StrEnum):
    ENVIRONMENT = "environment"
    MANUAL = "manual"


@dataclass(slots=True)
class ProviderModel:
    provider: str
    model_id: str
    display_name: str | None
    key_source: KeySource
    raw: JSONDict = field(default_factory=dict)

    @property
    def catalog_id(self) -> str:
        return f"{self.provider}:{self.model_id}"


@dataclass(slots=True)
class ProviderRegistration:
    provider: str
    api_key: str
    key_source: KeySource
    source_name: str
    models: list[ProviderModel] = field(default_factory=list)


@dataclass(slots=True)
class ProviderDiscoveryResult:
    provider: str
    key_source: KeySource
    source_name: str
    models: list[ProviderModel]
    error_message: str | None = None


@dataclass(slots=True)
class ProviderStatus:
    provider: str
    registered: bool
    key_source: KeySource | None = None
    source_name: str | None = None
    model_count: int = 0
    last_error: str | None = None


class ProviderAPIError(RuntimeError):
    """Represents a structured provider API failure."""

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProviderAdapter(ABC):
    """Provider-specific adapter contract."""

    provider: str
    api_key_env_vars: tuple[str, ...]

    @abstractmethod
    def list_models(self, api_key: str) -> list[ProviderModel]:
        """Return all models available to the supplied key."""

    @abstractmethod
    def send(
        self,
        api_key: str,
        model_id: str,
        messages: Sequence[MessageParam],
        config: Mapping[str, Any] | None = None,
    ) -> JSONDict:
        """Send the provider request and return the raw provider payload."""

    @abstractmethod
    def parse_response(self, raw: Mapping[str, Any], *, latency_ms: int) -> CompletionResult:
        """Normalize a raw provider response."""

    def _json_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
    ) -> JSONDict:
        body = None
        request_headers = dict(headers)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        req = request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with request.urlopen(req) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            raise self._coerce_error(raw, status_code=exc.code) from exc
        except error.URLError as exc:
            raise ProviderAPIError(str(exc.reason), code="network_error") from exc

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ProviderAPIError("provider returned a non-object JSON response")
        return data

    def _coerce_error(self, raw: str, *, status_code: int) -> ProviderAPIError:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ProviderAPIError(raw or f"HTTP {status_code}", code="http_error", status_code=status_code)

        if not isinstance(payload, dict):
            return ProviderAPIError(raw or f"HTTP {status_code}", code="http_error", status_code=status_code)

        provider_error = payload.get("error")
        if isinstance(provider_error, dict):
            message = str(provider_error.get("message", raw or f"HTTP {status_code}"))
            code = provider_error.get("code") or provider_error.get("type")
            return ProviderAPIError(message, code=str(code) if code is not None else None, status_code=status_code)

        message = str(payload.get("message", raw or f"HTTP {status_code}"))
        code = payload.get("type") or payload.get("code")
        return ProviderAPIError(message, code=str(code) if code is not None else None, status_code=status_code)


def _coerce_text_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part)
    return str(value)


class OpenAICompatibleAdapter(ProviderAdapter):
    """Shared OpenAI-style chat completions behavior."""

    models_url: str
    completions_url: str

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    def list_models(self, api_key: str) -> list[ProviderModel]:
        payload = self._json_request("GET", self.models_url, headers=self._headers(api_key))
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ProviderAPIError("provider returned an invalid model list")
        models: list[ProviderModel] = []
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            models.append(
                ProviderModel(
                    provider=self.provider,
                    model_id=str(item["id"]),
                    display_name=str(item.get("name")) if item.get("name") is not None else None,
                    key_source=KeySource.ENVIRONMENT,
                    raw=dict(item),
                )
            )
        return models

    def send(
        self,
        api_key: str,
        model_id: str,
        messages: Sequence[MessageParam],
        config: Mapping[str, Any] | None = None,
    ) -> JSONDict:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [dict(message) for message in messages],
        }
        if config:
            payload.update(dict(config))
        return self._json_request(
            "POST",
            self.completions_url,
            headers=self._headers(api_key),
            payload=payload,
        )

    def parse_response(self, raw: Mapping[str, Any], *, latency_ms: int) -> CompletionResult:
        choices = raw.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise ProviderAPIError("provider returned no completion choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ProviderAPIError("provider returned an invalid completion choice")
        message = first_choice.get("message", {})
        if not isinstance(message, dict):
            raise ProviderAPIError("provider returned an invalid completion message")

        usage = raw.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}

        return CompletionResult(
            content=_coerce_text_content(message.get("content", "")),
            token_usage={
                "input": int(usage.get("prompt_tokens", 0)),
                "output": int(usage.get("completion_tokens", 0)),
            },
            latency_ms=latency_ms,
            status=CompletionStatus.SUCCESS,
            provider_metadata={
                "id": raw.get("id"),
                "model": raw.get("model"),
                "finish_reason": first_choice.get("finish_reason"),
            },
        )


class OpenAIAdapter(OpenAICompatibleAdapter):
    provider = "openai"
    api_key_env_vars = ("OPENAI_API_KEY",)
    models_url = "https://api.openai.com/v1/models"
    completions_url = "https://api.openai.com/v1/chat/completions"


class OpenRouterAdapter(OpenAICompatibleAdapter):
    provider = "openrouter"
    api_key_env_vars = ("OPENROUTER_API_KEY",)
    models_url = "https://openrouter.ai/api/v1/models"
    completions_url = "https://openrouter.ai/api/v1/chat/completions"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }


class AnthropicAdapter(ProviderAdapter):
    provider = "anthropic"
    api_key_env_vars = ("ANTHROPIC_API_KEY",)
    models_url = "https://api.anthropic.com/v1/models"
    messages_url = "https://api.anthropic.com/v1/messages"
    anthropic_version = "2023-06-01"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": self.anthropic_version,
            "Content-Type": "application/json",
        }

    def list_models(self, api_key: str) -> list[ProviderModel]:
        payload = self._json_request("GET", self.models_url, headers=self._headers(api_key))
        data = payload.get("data", [])
        if not isinstance(data, list):
            raise ProviderAPIError("provider returned an invalid model list")
        return [
            ProviderModel(
                provider=self.provider,
                model_id=str(item["id"]),
                display_name=str(item.get("display_name")) if item.get("display_name") is not None else None,
                key_source=KeySource.ENVIRONMENT,
                raw=dict(item),
            )
            for item in data
            if isinstance(item, dict) and "id" in item
        ]

    def send(
        self,
        api_key: str,
        model_id: str,
        messages: Sequence[MessageParam],
        config: Mapping[str, Any] | None = None,
    ) -> JSONDict:
        config_data = dict(config or {})
        anthropic_messages: list[dict[str, Any]] = []
        system_parts: list[str] = []

        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content", "")
            text_content = _coerce_text_content(content)
            if role == "system":
                if text_content:
                    system_parts.append(text_content)
                continue
            normalized_role = "assistant" if role == "assistant" else "user"
            anthropic_messages.append({"role": normalized_role, "content": text_content})

        payload: dict[str, Any] = {
            "model": model_id,
            "max_tokens": int(config_data.pop("max_tokens", 1024)),
            "messages": anthropic_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        payload.update(config_data)
        return self._json_request(
            "POST",
            self.messages_url,
            headers=self._headers(api_key),
            payload=payload,
        )

    def parse_response(self, raw: Mapping[str, Any], *, latency_ms: int) -> CompletionResult:
        content_blocks = raw.get("content", [])
        usage = raw.get("usage", {})
        if not isinstance(content_blocks, list):
            content_blocks = []
        if not isinstance(usage, dict):
            usage = {}
        content_parts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                content_parts.append(str(block.get("text", "")))

        return CompletionResult(
            content="\n".join(part for part in content_parts if part),
            token_usage={
                "input": int(usage.get("input_tokens", 0)),
                "output": int(usage.get("output_tokens", 0)),
            },
            latency_ms=latency_ms,
            status=CompletionStatus.SUCCESS,
            provider_metadata={
                "id": raw.get("id"),
                "model": raw.get("model"),
                "stop_reason": raw.get("stop_reason"),
            },
        )


DEFAULT_ADAPTERS: tuple[ProviderAdapter, ...] = (
    AnthropicAdapter(),
    OpenAIAdapter(),
    OpenRouterAdapter(),
)


class ProviderLayer:
    """Registry and runtime interface for model providers."""

    def __init__(
        self,
        adapters: Sequence[ProviderAdapter] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        self._adapters = {adapter.provider: adapter for adapter in (adapters or DEFAULT_ADAPTERS)}
        self._environ = environ if environ is not None else os.environ
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._registrations: dict[str, ProviderRegistration] = {}
        self._status: dict[str, ProviderStatus] = {
            provider: ProviderStatus(provider=provider, registered=False)
            for provider in self._adapters
        }

    def discover_keys(self) -> list[ProviderDiscoveryResult]:
        discovered: list[ProviderDiscoveryResult] = []
        for provider, adapter in self._adapters.items():
            for env_var in adapter.api_key_env_vars:
                api_key = self._environ.get(env_var)
                if not api_key:
                    continue
                discovered.append(self.register_key(provider, api_key, source=KeySource.ENVIRONMENT, source_name=env_var))
                break
        return discovered

    def register_key(
        self,
        provider: str,
        api_key: str,
        *,
        source: KeySource = KeySource.MANUAL,
        source_name: str | None = None,
    ) -> ProviderDiscoveryResult:
        adapter = self._get_adapter(provider)
        registration = ProviderRegistration(
            provider=provider,
            api_key=api_key,
            key_source=source,
            source_name=source_name or source.value,
        )
        self._registrations[provider] = registration
        status = self._status[provider]
        status.registered = True
        status.key_source = source
        status.source_name = registration.source_name
        status.last_error = None

        try:
            models = adapter.list_models(api_key)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            registration.models = []
            status.model_count = 0
            status.last_error = message
            return ProviderDiscoveryResult(
                provider=provider,
                key_source=source,
                source_name=registration.source_name,
                models=[],
                error_message=message,
            )

        for model in models:
            model.key_source = source
        registration.models = models
        status.model_count = len(models)
        return ProviderDiscoveryResult(
            provider=provider,
            key_source=source,
            source_name=registration.source_name,
            models=models,
        )

    def list_available_models(self) -> list[ProviderModel]:
        models: list[ProviderModel] = []
        for registration in self._registrations.values():
            if not registration.models:
                try:
                    registration.models = self._get_adapter(registration.provider).list_models(registration.api_key)
                    for model in registration.models:
                        model.key_source = registration.key_source
                    self._status[registration.provider].model_count = len(registration.models)
                    self._status[registration.provider].last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._status[registration.provider].last_error = str(exc)
                    continue
            models.extend(registration.models)
        return sorted(models, key=lambda model: (model.provider, model.model_id))

    def get_provider_status(self, provider: str) -> ProviderStatus:
        self._get_adapter(provider)
        status = self._status[provider]
        return ProviderStatus(
            provider=status.provider,
            registered=status.registered,
            key_source=status.key_source,
            source_name=status.source_name,
            model_count=status.model_count,
            last_error=status.last_error,
        )

    def complete(
        self,
        model_id: str,
        messages: Sequence[MessageParam],
        config: Mapping[str, Any] | None = None,
    ) -> CompletionResult:
        try:
            provider, resolved_model_id, registration = self._resolve_model(model_id)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(message=str(exc), code="model_resolution_error")

        adapter = self._get_adapter(provider)
        max_attempts = max(1, self._retry_attempts + 1)
        last_error: ProviderAPIError | None = None

        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                raw = adapter.send(registration.api_key, resolved_model_id, messages, config=config)
                latency_ms = int((time.perf_counter() - started) * 1000)
                self._status[provider].last_error = None
                return adapter.parse_response(raw, latency_ms=latency_ms)
            except ProviderAPIError as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001
                last_error = ProviderAPIError(str(exc), code="provider_error")

            if attempt < max_attempts and self._retry_backoff_seconds > 0:
                time.sleep(self._retry_backoff_seconds)

        assert last_error is not None
        self._status[provider].last_error = str(last_error)
        return self._error_result(
            message=str(last_error),
            code=last_error.code or "provider_error",
        )

    def _resolve_model(self, model_id: str) -> tuple[str, str, ProviderRegistration]:
        if ":" in model_id:
            provider, raw_model_id = model_id.split(":", 1)
            registration = self._registrations.get(provider)
            if registration is None:
                raise ValueError(f"no key registered for provider '{provider}'")
            return provider, raw_model_id, registration

        matching_models = [model for model in self.list_available_models() if model.model_id == model_id]
        if not matching_models:
            raise ValueError(f"unknown model '{model_id}'")
        if len(matching_models) > 1:
            providers = ", ".join(sorted({model.provider for model in matching_models}))
            raise ValueError(f"model '{model_id}' is ambiguous across providers: {providers}")

        model = matching_models[0]
        registration = self._registrations[model.provider]
        return model.provider, model.model_id, registration

    def _get_adapter(self, provider: str) -> ProviderAdapter:
        try:
            return self._adapters[provider]
        except KeyError as exc:
            raise ValueError(f"unknown provider '{provider}'") from exc

    def _error_result(self, *, message: str, code: str | None) -> CompletionResult:
        return CompletionResult(
            content="",
            token_usage={"input": 0, "output": 0},
            latency_ms=0,
            status=CompletionStatus.ERROR,
            error_code=code,
            error_message=message,
            provider_metadata=None,
        )
