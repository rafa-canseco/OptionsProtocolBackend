"""Shared pytest fixtures for the backend test suite."""

import pytest


@pytest.fixture(autouse=True)
def configure_resend_api_key(monkeypatch):
    """Provide a non-empty resend_api_key so email send functions run in tests.

    Tests that want the noop path must patch settings.resend_api_key = "" themselves.
    """
    import src.notifications.email as email_mod

    monkeypatch.setattr(email_mod.settings, "resend_api_key", "re_test_key")
