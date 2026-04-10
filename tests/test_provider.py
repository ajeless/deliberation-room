from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence

from deliberation_room.domain import CompletionResult, CompletionStatus
from deliberation_room.provider import (
    KeySource,
    ProviderAPIError,
    ProviderAdapter,
    ProviderLayer,
    ProviderModel,
)


class FakeAdapter(ProviderAdapter):
    def __init__(
        self,
        provider: str,
        env_var: str,
        *,
        model_ids: Sequence[str],
        send_plan: Sequence[Any] | None = None,
    ) -> None:
        self.provider = provider
        self.api_key_env_vars = (env_var,)
        self._model_ids = list(model_ids)
        self._send_plan = list(send_plan or [])
        self.list_models_calls = 0
        self.send_calls = 0

    def list_models(self, api_key: str) -> list[ProviderModel]:
        self.list_models_calls += 1
        return [
            ProviderModel(
                provider=self.provider,
                model_id=model_id,
                display_name=model_id.upper(),
                key_source=KeySource.ENVIRONMENT,
                raw={"id": model_id},
            )
            for model_id in self._model_ids
        ]

    def send(
        self,
        api_key: str,
        model_id: str,
        messages: Sequence[Mapping[str, Any]],
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.send_calls += 1
        if self._send_plan:
            next_result = self._send_plan.pop(0)
            if isinstance(next_result, Exception):
                raise next_result
            return dict(next_result)
        return {
            "choices": [{"message": {"content": f"{self.provider}:{model_id}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            "model": model_id,
            "id": f"{self.provider}_response",
        }

    def parse_response(self, raw: Mapping[str, Any], *, latency_ms: int) -> CompletionResult:
        return CompletionResult(
            content=str(raw["choices"][0]["message"]["content"]),
            token_usage={
                "input": int(raw.get("usage", {}).get("prompt_tokens", 0)),
                "output": int(raw.get("usage", {}).get("completion_tokens", 0)),
            },
            latency_ms=latency_ms,
            status=CompletionStatus.SUCCESS,
            provider_metadata={"model": raw.get("model")},
        )


class ProviderLayerTests(unittest.TestCase):
    def test_discover_keys_scans_environment_and_loads_models(self) -> None:
        openai = FakeAdapter("openai", "OPENAI_API_KEY", model_ids=("gpt-a",))
        anthropic = FakeAdapter("anthropic", "ANTHROPIC_API_KEY", model_ids=("claude-a",))
        layer = ProviderLayer(
            adapters=(anthropic, openai),
            environ={"OPENAI_API_KEY": "sk-openai", "ANTHROPIC_API_KEY": "sk-anthropic"},
            retry_backoff_seconds=0,
        )

        discovered = layer.discover_keys()

        self.assertEqual([item.provider for item in discovered], ["anthropic", "openai"])
        self.assertEqual(openai.list_models_calls, 1)
        self.assertEqual(anthropic.list_models_calls, 1)
        self.assertEqual(
            [(model.provider, model.model_id) for model in layer.list_available_models()],
            [("anthropic", "claude-a"), ("openai", "gpt-a")],
        )

    def test_register_key_supports_manual_entry(self) -> None:
        openrouter = FakeAdapter("openrouter", "OPENROUTER_API_KEY", model_ids=("openai/gpt-test",))
        layer = ProviderLayer(adapters=(openrouter,), environ={}, retry_backoff_seconds=0)

        discovery = layer.register_key("openrouter", "sk-openrouter")

        self.assertEqual(discovery.key_source, KeySource.MANUAL)
        self.assertEqual(discovery.models[0].catalog_id, "openrouter:openai/gpt-test")
        status = layer.get_provider_status("openrouter")
        self.assertTrue(status.registered)
        self.assertEqual(status.model_count, 1)

    def test_complete_retries_then_succeeds(self) -> None:
        openai = FakeAdapter(
            "openai",
            "OPENAI_API_KEY",
            model_ids=("gpt-a",),
            send_plan=[
                ProviderAPIError("rate limited", code="rate_limit"),
                {
                    "choices": [{"message": {"content": "success after retry"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 12},
                    "model": "gpt-a",
                    "id": "cmpl_1",
                },
            ],
        )
        layer = ProviderLayer(adapters=(openai,), environ={}, retry_attempts=1, retry_backoff_seconds=0)
        layer.register_key("openai", "sk-openai")

        result = layer.complete("gpt-a", [{"role": "user", "content": "Hello"}])

        self.assertEqual(result.status, CompletionStatus.SUCCESS)
        self.assertEqual(result.content, "success after retry")
        self.assertEqual(openai.send_calls, 2)
        self.assertIsNone(layer.get_provider_status("openai").last_error)

    def test_complete_returns_error_after_retry_exhaustion(self) -> None:
        anthropic = FakeAdapter(
            "anthropic",
            "ANTHROPIC_API_KEY",
            model_ids=("claude-a",),
            send_plan=[
                ProviderAPIError("temporary failure", code="server_error"),
                ProviderAPIError("still failing", code="server_error"),
            ],
        )
        layer = ProviderLayer(adapters=(anthropic,), environ={}, retry_attempts=1, retry_backoff_seconds=0)
        layer.register_key("anthropic", "sk-anthropic")

        result = layer.complete("claude-a", [{"role": "user", "content": "Hello"}])

        self.assertEqual(result.status, CompletionStatus.ERROR)
        self.assertEqual(result.error_code, "server_error")
        self.assertIn("still failing", result.error_message)
        self.assertEqual(anthropic.send_calls, 2)
        self.assertIn("still failing", layer.get_provider_status("anthropic").last_error or "")

    def test_complete_detects_ambiguous_model_ids(self) -> None:
        openai = FakeAdapter("openai", "OPENAI_API_KEY", model_ids=("shared-model",))
        openrouter = FakeAdapter("openrouter", "OPENROUTER_API_KEY", model_ids=("shared-model",))
        layer = ProviderLayer(
            adapters=(openai, openrouter),
            environ={},
            retry_backoff_seconds=0,
        )
        layer.register_key("openai", "sk-openai")
        layer.register_key("openrouter", "sk-openrouter")

        ambiguous = layer.complete("shared-model", [{"role": "user", "content": "Hello"}])
        resolved = layer.complete("openai:shared-model", [{"role": "user", "content": "Hello"}])

        self.assertEqual(ambiguous.status, CompletionStatus.ERROR)
        self.assertEqual(ambiguous.error_code, "model_resolution_error")
        self.assertIn("ambiguous", ambiguous.error_message)
        self.assertEqual(resolved.status, CompletionStatus.SUCCESS)
        self.assertEqual(resolved.content, "openai:shared-model")


if __name__ == "__main__":
    unittest.main()
