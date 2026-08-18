from unittest.mock import MagicMock

import pytest
from eth_account import Account

from src.config import Settings, settings
from src.main import (
    validate_lazy_otoken_chain_config,
    validate_lazy_otoken_config,
)


TEST_PRIVATE_KEY = "0x" + f"{1:064x}"
ADDRESSES = {
    "batch": "0x1111111111111111111111111111111111111111",
    "factory": "0x2222222222222222222222222222222222222222",
    "whitelist": "0x3333333333333333333333333333333333333333",
    "usdc": "0x4444444444444444444444444444444444444444",
    "weth": "0x5555555555555555555555555555555555555555",
    "wbtc": "0x6666666666666666666666666666666666666666",
    "address_book": "0x7777777777777777777777777777777777777777",
}


def _configure_lazy_requirements(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rpc_url", "https://rpc.example")
    monkeypatch.setattr(settings, "chain_id", 84532)
    monkeypatch.setattr(settings, "batch_settler_address", ADDRESSES["batch"])
    monkeypatch.setattr(settings, "otoken_factory_address", ADDRESSES["factory"])
    monkeypatch.setattr(settings, "whitelist_address", ADDRESSES["whitelist"])
    monkeypatch.setattr(settings, "operator_private_key", TEST_PRIVATE_KEY)
    monkeypatch.setattr(settings, "usdc_address", ADDRESSES["usdc"])
    monkeypatch.setattr(settings, "weth_address", ADDRESSES["weth"])
    monkeypatch.setattr(settings, "wbtc_address", ADDRESSES["wbtc"])
    monkeypatch.setattr(settings, "otoken_lazy_assets", "eth")
    monkeypatch.setattr(settings, "otoken_intent_hmac_secret", "secret")
    monkeypatch.setattr(settings, "privy_app_id", "app")
    monkeypatch.setattr(settings, "privy_app_secret", "secret")
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "key")
    monkeypatch.setattr(settings, "privy_jwks_url", "")
    monkeypatch.setattr(settings, "otoken_ensure_deadline_buffer_seconds", 30)
    monkeypatch.setattr(
        settings,
        "otoken_materialization_deadline_buffer_seconds",
        150,
    )


def _contract() -> MagicMock:
    return MagicMock()


def _configured_w3(
    *,
    chain_id: int = 84532,
    missing_code_for: str | None = None,
    batch_address_book: str | None = None,
    operator: str | None = None,
    address_book_whitelist: str | None = None,
) -> MagicMock:
    factory = _contract()
    factory.functions.addressBook.return_value.call.return_value = ADDRESSES[
        "address_book"
    ]
    factory.functions.operator.return_value.call.return_value = (
        operator or Account.from_key(TEST_PRIVATE_KEY).address
    )

    batch = _contract()
    batch.functions.addressBook.return_value.call.return_value = (
        batch_address_book or ADDRESSES["address_book"]
    )

    address_book = _contract()
    address_book.functions.oTokenFactory.return_value.call.return_value = ADDRESSES[
        "factory"
    ]
    address_book.functions.whitelist.return_value.call.return_value = (
        address_book_whitelist or ADDRESSES["whitelist"]
    )
    address_book.functions.batchSettler.return_value.call.return_value = ADDRESSES[
        "batch"
    ]

    w3 = MagicMock()
    w3.is_connected.return_value = True
    w3.eth.chain_id = chain_id

    def get_code(address):
        if missing_code_for and address.lower() == ADDRESSES[missing_code_for].lower():
            return b""
        return b"\x60\x00"

    def get_contract(*, address, abi):
        del abi
        if address.lower() == ADDRESSES["factory"].lower():
            return factory
        if address.lower() == ADDRESSES["batch"].lower():
            return batch
        if address.lower() == ADDRESSES["address_book"].lower():
            return address_book
        raise AssertionError(f"Unexpected contract lookup: {address}")

    w3.eth.get_code.side_effect = get_code
    w3.eth.contract.side_effect = get_contract
    return w3


def test_eager_is_the_rollback_safe_default() -> None:
    assert Settings.model_fields["otoken_series_mode"].default == "eager"
    validate_lazy_otoken_config("eager")

    w3 = MagicMock()
    validate_lazy_otoken_chain_config("eager", w3)
    w3.is_connected.assert_not_called()


def test_lazy_startup_requires_whitelist(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "whitelist_address", "")

    with pytest.raises(RuntimeError, match="WHITELIST_ADDRESS"):
        validate_lazy_otoken_config("lazy")


@pytest.mark.parametrize("materialization_buffer", [31, 120, 149])
def test_lazy_startup_rejects_short_materialization_buffer(
    monkeypatch,
    materialization_buffer,
) -> None:
    _configure_lazy_requirements(monkeypatch)
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

    validate_lazy_otoken_config("lazy")


def test_lazy_startup_accepts_https_jwks_without_pem(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(
        settings,
        "privy_jwks_url",
        "https://auth.example/.well-known/jwks.json",
    )

    validate_lazy_otoken_config("lazy")


def test_lazy_startup_requires_pem_or_jwks(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(settings, "privy_jwks_url", "")

    with pytest.raises(
        RuntimeError,
        match="PRIVY_JWT_VERIFICATION_KEY or PRIVY_JWKS_URL",
    ):
        validate_lazy_otoken_config("lazy")


def test_lazy_startup_rejects_non_https_jwks(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "privy_jwt_verification_key", "")
    monkeypatch.setattr(settings, "privy_jwks_url", "http://auth.example/jwks.json")

    with pytest.raises(RuntimeError, match="valid HTTPS URL"):
        validate_lazy_otoken_config("lazy")


def test_lazy_startup_rejects_non_base_configured_chain(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "chain_id", 1)

    with pytest.raises(RuntimeError, match="only supports Base"):
        validate_lazy_otoken_chain_config("lazy", _configured_w3(chain_id=1))


def test_lazy_startup_rejects_rpc_for_another_chain(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)

    with pytest.raises(RuntimeError, match="RPC chain mismatch"):
        validate_lazy_otoken_chain_config("lazy", _configured_w3(chain_id=8453))


def test_lazy_startup_rejects_required_contract_without_code(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)

    with pytest.raises(
        RuntimeError, match="WHITELIST_ADDRESS has no deployed bytecode"
    ):
        validate_lazy_otoken_chain_config(
            "lazy",
            _configured_w3(missing_code_for="whitelist"),
        )


def test_lazy_startup_rejects_reused_contract_address(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    monkeypatch.setattr(settings, "whitelist_address", ADDRESSES["factory"])

    with pytest.raises(RuntimeError, match="reuses one address"):
        validate_lazy_otoken_chain_config("lazy", _configured_w3())


def test_lazy_startup_rejects_crossed_deployment_wiring(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    other_address_book = "0x8888888888888888888888888888888888888888"

    with pytest.raises(RuntimeError, match="crossed configuration"):
        validate_lazy_otoken_chain_config(
            "lazy",
            _configured_w3(batch_address_book=other_address_book),
        )


def test_lazy_startup_rejects_wrong_factory_operator(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)
    wrong_operator = Account.from_key("0x" + f"{2:064x}").address

    with pytest.raises(RuntimeError, match="does not match factory operator"):
        validate_lazy_otoken_chain_config(
            "lazy",
            _configured_w3(operator=wrong_operator),
        )


def test_lazy_startup_rejects_crossed_address_book_entries(monkeypatch) -> None:
    _configure_lazy_requirements(monkeypatch)

    with pytest.raises(RuntimeError, match="WHITELIST_ADDRESS"):
        validate_lazy_otoken_chain_config(
            "lazy",
            _configured_w3(address_book_whitelist=ADDRESSES["weth"]),
        )


def test_lazy_startup_accepts_consistent_base_sepolia_deployment(
    monkeypatch,
) -> None:
    _configure_lazy_requirements(monkeypatch)
    w3 = _configured_w3()

    validate_lazy_otoken_chain_config("lazy", w3)

    assert w3.eth.get_code.call_count == 6
