import pytest

from scripts.verify_lazy_create2_fork import validate_local_fork_url


@pytest.mark.parametrize(
    "rpc_url",
    [
        "https://mainnet.base.org",
        "https://127.0.0.1.attacker.example",
        "http://10.0.0.5:8545",
        "file:///tmp/anvil.ipc",
    ],
)
def test_fork_harness_refuses_non_loopback_rpc(rpc_url: str) -> None:
    with pytest.raises(ValueError, match="loopback Anvil"):
        validate_local_fork_url(rpc_url)


@pytest.mark.parametrize(
    "rpc_url",
    [
        "http://127.0.0.1:8545",
        "http://localhost:8545",
        "https://[::1]:8545",
    ],
)
def test_fork_harness_allows_only_loopback_rpc(rpc_url: str) -> None:
    validate_local_fork_url(rpc_url)
