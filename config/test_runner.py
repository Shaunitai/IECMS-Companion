"""Test runner that guarantees the test suite cannot reach the network.

Three hard guarantees:

1. Any real LLM credentials in the environment are cleared, so the suite never
   depends on the developer's ``.env`` and always runs in a known, offline
   state. A test that wants credentials sets ``LLM_API_KEY`` itself.
2. The provider SDK loader is replaced with a function that raises, so a test
   that forgets to mock ``llm_service.generate_explanation`` fails loudly
   instead of making a real, billable call to a provider.
3. ``urllib.request.urlopen`` is blocked, so no test can accidentally fetch a
   remote document.

This exists because an earlier version of the suite did make one real call to
the Gemini API when a test overrode ``LLM_API_KEY`` without mocking the
provider. See docs/SECURITY.md.
"""

from __future__ import annotations

import urllib.request
from unittest import mock

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

from chatbot.services.llm_service import LLMServiceError


def _blocked_sdk_load():
    raise LLMServiceError(
        "The provider SDK is disabled during tests. Mock "
        "chatbot.services.llm_service.generate_explanation instead of letting "
        "a test reach the network.",
        code="sdk_disabled_in_tests",
    )


def _blocked_urlopen(*args, **kwargs):
    raise RuntimeError(
        "Network access is disabled during tests. Mock the fetch function "
        "instead of performing a real request."
    )


class NoNetworkTestRunner(DiscoverRunner):
    """Django test runner that disables outbound network access."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # A developer .env with a real API key must not change how the tests
        # behave. Individual tests can still override LLM_API_KEY explicitly.
        self._settings_override = override_settings(
            LLM_API_KEY="",
            LLM_PROVIDER="gemini",
            ENABLE_WEB_FALLBACK=False,
        )
        self._settings_override.enable()
        self._patchers = [
            mock.patch(
                "chatbot.services.llm_service._load_sdk",
                side_effect=_blocked_sdk_load,
            ),
            mock.patch.object(urllib.request, "urlopen", _blocked_urlopen),
        ]
        for patcher in self._patchers:
            patcher.start()

    def teardown_test_environment(self, **kwargs):
        for patcher in getattr(self, "_patchers", []):
            patcher.stop()
        settings_override = getattr(self, "_settings_override", None)
        if settings_override is not None:
            settings_override.disable()
        super().teardown_test_environment(**kwargs)
