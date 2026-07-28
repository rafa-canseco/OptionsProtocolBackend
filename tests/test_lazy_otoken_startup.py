import pytest

from src.config import Settings, settings
from src.main import validate_lazy_otoken_config


def _configure_lazy_requirements(monkeypatch) -> None:
    monkeypatch.setattr(settings, "whitelist_address", "0xwhitelist")
    monkeypatch.setattr(settings, "otoken_intent_hmac_secret", "secret")
    monkeypatch.setattr(settings, "privy_app_id", "app")
    monkeypatch.setattr(settings, "privy_app_secret", "secret")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "key")


def test_eager_is_the_rollback_safe_default() -> None:
    assert Settings.model_fields["otoken_series_mode"].default == "eager"
    validate_lazy_otoken_config("eager")


def test_lazy_startup_requires_whitelist(monkeypatch) -> None:
    monkeypatch.setattr(settings, "whitelist_address", "")
    monkeypatch.setattr(settings, "otoken_intent_hmac_secret", "secret")
    monkeypatch.setattr(settings, "privy_app_id", "app")
    monkeypatch.setattr(settings, "privy_app_secret", "secret")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "key")

    with pytest.raises(RuntimeError, match="WHITELIST_ADDRESS"):
        validate_lazy_otoken_config("lazy")


@pytest.mark.parametrize("materialization_buffer", [31, 120, 149])
def test_lazy_startup_rejects_short_materialization_buffer(
    monkeypatch,
    materialization_buffer,
) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "otoken_ensure_deadline_buffer_seconds", 30)
    monkeypatch.setattr(
        settings,
        "otoken_materialization_deadline_buffer_seconds",
        materialization_buffer,
    )

    with pytest.raises(
        RuntimeError,
        match="OTOKEN_MATERIALIZATION_DEADLINE_BUFFER_SECONDS",
    ):
        validate_lazy_otoken_config("lazy")


def test_lazy_startup_accepts_default_materialization_buffer(
    monkeypatch,
) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "otoken_ensure_deadline_buffer_seconds", 30)
    monkeypatch.setattr(
        settings,
        "otoken_materialization_deadline_buffer_seconds",
        150,
    )

    validate_lazy_otoken_config("lazy")
