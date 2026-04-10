from __future__ import annotations

import os
import unittest

from deliberation_room.provider import (
    AnthropicAdapter,
    KeySource,
    OpenAIAdapter,
    OpenRouterAdapter,
    ProviderLayer,
)


RUN_LIVE_PROVIDER_TESTS = os.getenv("RUN_LIVE_PROVIDER_TESTS") == "1"
LIVE_DISCOVERY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
HAS_LIVE_DISCOVERY_KEYS = any(os.getenv(env_var) for env_var in LIVE_DISCOVERY_ENV_VARS.values())


def expected_live_discovery_providers() -> dict[str, str]:
    return {
        provider: env_var
        for provider, env_var in LIVE_DISCOVERY_ENV_VARS.items()
        if os.getenv(env_var)
    }


class LiveProviderIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        RUN_LIVE_PROVIDER_TESTS and HAS_LIVE_DISCOVERY_KEYS,
        "requires RUN_LIVE_PROVIDER_TESTS=1 and at least one supported provider key",
    )
    def test_provider_layer_discovers_live_environment_keys(self) -> None:
        expected = expected_live_discovery_providers()
        layer = ProviderLayer()

        discovered = layer.discover_keys()
        discovered_by_provider = {item.provider: item for item in discovered}

        self.assertEqual(set(discovered_by_provider), set(expected))
        aggregated_models = layer.list_available_models()
        models_by_provider = {
            provider: [model for model in aggregated_models if model.provider == provider]
            for provider in expected
        }

        for provider, env_var in expected.items():
            with self.subTest(provider=provider):
                result = discovered_by_provider[provider]
                self.assertEqual(result.key_source, KeySource.ENVIRONMENT)
                self.assertEqual(result.source_name, env_var)
                self.assertIsNone(result.error_message)
                self.assertGreater(len(result.models), 0)
                self.assertGreater(len(models_by_provider[provider]), 0)

                status = layer.get_provider_status(provider)
                self.assertTrue(status.registered)
                self.assertEqual(status.key_source, KeySource.ENVIRONMENT)
                self.assertEqual(status.source_name, env_var)
                self.assertIsNone(status.last_error)
                self.assertGreater(status.model_count, 0)

    @unittest.skipUnless(
        RUN_LIVE_PROVIDER_TESTS and os.getenv("OPENAI_API_KEY"),
        "requires RUN_LIVE_PROVIDER_TESTS=1 and OPENAI_API_KEY",
    )
    def test_openai_list_models(self) -> None:
        models = OpenAIAdapter().list_models(os.environ["OPENAI_API_KEY"])
        self.assertGreater(len(models), 0)

    @unittest.skipUnless(
        RUN_LIVE_PROVIDER_TESTS and os.getenv("ANTHROPIC_API_KEY"),
        "requires RUN_LIVE_PROVIDER_TESTS=1 and ANTHROPIC_API_KEY",
    )
    def test_anthropic_list_models(self) -> None:
        models = AnthropicAdapter().list_models(os.environ["ANTHROPIC_API_KEY"])
        self.assertGreater(len(models), 0)

    @unittest.skipUnless(
        RUN_LIVE_PROVIDER_TESTS and os.getenv("OPENROUTER_API_KEY"),
        "requires RUN_LIVE_PROVIDER_TESTS=1 and OPENROUTER_API_KEY",
    )
    def test_openrouter_list_models(self) -> None:
        models = OpenRouterAdapter().list_models(os.environ["OPENROUTER_API_KEY"])
        self.assertGreater(len(models), 0)


if __name__ == "__main__":
    unittest.main()
