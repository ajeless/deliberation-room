from __future__ import annotations

import os
import unittest

from deliberation_room.provider import AnthropicAdapter, OpenAIAdapter, OpenRouterAdapter


RUN_LIVE_PROVIDER_TESTS = os.getenv("RUN_LIVE_PROVIDER_TESTS") == "1"


class LiveProviderIntegrationTests(unittest.TestCase):
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
