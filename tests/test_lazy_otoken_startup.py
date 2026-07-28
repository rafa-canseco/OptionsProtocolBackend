import pytest

from src.config import Settings, settings
from src.main import validate_lazy_otoken_config


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
