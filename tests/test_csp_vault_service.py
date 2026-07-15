from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import pytest

from src.vaults.csp_reader import CspRpcError, RpcRead
from src.vaults.csp_service import CspVaultService


VAULT = "0xcf2c5b2e065bB7ADD2a29ed4d3A61910e6a59645"
USDC = "0xAB51a471493832C1D70cef8ff937A850cf37c860"
WETH = "0x8A6Aa2304797898d46eC1d342Fedc817D3a973B6"
USER = "0x1234567890abcdef1234567890abcdef12345678"
OTOKEN_1 = "0x5f0095EdE2B3539C0a6fDa6b50cCF850Fc5E3CF4"
OTOKEN_2 = "0x09b0844b757410aA97ea433b5e4DEBEdEa0CB126"
OTOKEN_3 = "0x61faa2bA6f7135b0cFE528e3844727391D98E798"


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _global(**updates):
    values = {
        "totalManagedAssets": 0,
        "totalShares": 0,
        "availableIdleAssets": 0,
        "activeCollateral": 0,
        "activeBatches": 0,
        "currentEpoch": 1,
        "preparedSettlementBatchId": 0,
        "totalPendingDepositAssets": 0,
        "totalPendingWithdrawalShares": 0,
        "reservedWithdrawalAssets": 0,
        "accountedUnderlyingAssets": 0,
        "availableUnderlyingAssets": 0,
        "reservedUnderlyingAssets": 0,
        "allocatedUnderlyingAssets": 0,
        "cumulativeUnderlyingPerShare": 0,
        "currentShareGeneration": 1,
        "batchCount": 0,
        "settlementDefaultDelay": 3600,
        "totalPendingWithdrawalClaims": 0,
    }
    values.update(updates)
    return values


def _epoch(**updates):
    values = {
        "epochId": 1,
        "startedAt": 1_700_000_000,
        "endedAt": 0,
        "deposits": 0,
        "withdrawals": 0,
        "committedCollateral": 0,
        "returnedCollateral": 0,
        "premiumEarned": 0,
        "assignmentShortfall": 0,
        "performanceFee": 0,
        "withdrawalAssetsPerShare": 0,
        "withdrawalAssetsRemaining": 0,
        "remainingWithdrawalClaims": 0,
        "closed": False,
        "withdrawalUnderlyingPerShare": 0,
        "withdrawalUnderlyingRemaining": 0,
    }
    values.update(updates)
    return values


def _user(**updates):
    values = {
        "sharesOf": 0,
        "pendingDepositAssets": 0,
        "pendingWithdrawalShares": 0,
        "pendingWithdrawalEpoch": 0,
        "claimableAssignedUnderlying": 0,
        "shareGeneration": 1,
        "underlyingPerSharePaid": 0,
    }
    values.update(updates)
    return values


def _batch(batch_id: int, otoken: str, **updates):
    values = {
        "batchId": batch_id,
        "epochId": 1,
        "oToken": otoken,
        "protocolVaultId": batch_id,
        "amount": 1_000_000,
        "collateral": 20_000_000,
        "premiumEarned": 9_600,
        "collateralReturned": 0,
        "settled": False,
        "underlyingReceived": 0,
    }
    values.update(updates)
    return values


class FakeReader:
    def __init__(self) -> None:
        self.block = 100
        self.calls = Counter()
        self.global_values = _global()
        self.epochs = {1: _epoch()}
        self.users = {USER.lower(): _user()}
        self.batches = {}
        self.options = {}
        self.cutoffs = {}
        self.fail_global = False

    def _read(self, values):
        return RpcRead(self.block, "0x" + "11" * 32, values)

    def read_global(self):
        self.calls["global"] += 1
        if self.fail_global:
            raise CspRpcError("offline")
        return self._read(dict(self.global_values))

    def read_epoch(self, epoch_id, *, block_identifier):
        assert block_identifier == self.block
        self.calls[f"epoch:{epoch_id}"] += 1
        return self._read(dict(self.epochs[epoch_id]))

    def read_batches(self, batch_ids, *, block_identifier):
        assert block_identifier == self.block
        ids = list(batch_ids)
        self.calls["batches"] += 1
        return self._read({"batches": {i: dict(self.batches[i]) for i in ids}})

    def read_option_series(self, addresses, *, block_identifier):
        assert block_identifier == self.block
        normalized = [address.lower() for address in addresses]
        self.calls["options"] += 1
        return self._read(
            {"series": {address: dict(self.options[address]) for address in normalized}}
        )

    def read_user(self, address, *, block_identifier):
        assert block_identifier == self.block
        self.calls[f"user:{address.lower()}"] += 1
        return self._read(dict(self.users[address.lower()]))

    def read_generation_cutoff(self, generation, *, block_identifier):
        assert block_identifier == self.block
        self.calls[f"generation:{generation}"] += 1
        return self._read({"cutoff": self.cutoffs[generation]})


def make_service(reader: FakeReader, clock: Clock | None = None):
    return CspVaultService(
        reader,
        vault_key="base-sepolia:eth-usdc-csp",
        chain_id=84532,
        vault_address=VAULT,
        usdc_address=USDC,
        weth_address=WETH,
        ttl_seconds=15,
        stale_seconds=60,
        recent_batch_limit=20,
        max_batch_scan=100,
        user_cache_size=2,
        clock=clock or Clock(),
        utcnow=lambda: datetime(2026, 7, 15, tzinfo=timezone.utc),
    )


def add_option(reader: FakeReader, address: str, strike: int = 2000 * 10**8):
    reader.options[address.lower()] = {
        "oToken": address,
        "underlying": WETH,
        "strikeAsset": USDC,
        "collateralAsset": USDC,
        "strikePrice": strike,
        "expiry": 1_784_188_800,
        "isPut": True,
    }


def test_empty_snapshot_is_cached_without_repeated_reads():
    reader = FakeReader()
    service = make_service(reader)

    first = service.get_vault("base-sepolia:eth-usdc-csp")
    second = service.get_vault("base-sepolia:eth-usdc-csp")

    assert first.status == "idle"
    assert first.summary.total_managed_assets == "0"
    assert first.summary.share_price_assets == "1000000"
    assert second == first
    assert reader.calls == Counter({"global": 1, "epoch:1": 1})


def test_deposited_user_actions_and_user_cache():
    reader = FakeReader()
    reader.global_values.update(
        totalManagedAssets=1_000_000_000,
        totalShares=1_000_000_000,
        availableIdleAssets=1_000_000_000,
    )
    reader.users[USER.lower()] = _user(sharesOf=100_000_000)
    service = make_service(reader)

    first = service.get_user_position("base-sepolia:eth-usdc-csp", USER)
    second = service.get_user_position("base-sepolia:eth-usdc-csp", USER)

    assert first.position.active_shares == "100000000"
    assert first.position.active_assets == "100000000"
    assert first.actions.deposit.available is True
    assert first.actions.deposit.mode == "immediate"
    assert first.actions.withdraw_idle.available is True
    assert first.actions.request_withdraw.available is True
    assert second == first
    assert reader.calls[f"user:{USER.lower()}"] == 1
    assert reader.calls["global"] == 1


def test_active_batch_hydration_and_actions_are_cached():
    reader = FakeReader()
    reader.global_values = _global(
        totalManagedAssets=1_000_025_920,
        totalShares=1_000_000_000,
        availableIdleAssets=980_025_920,
        activeCollateral=20_000_000,
        activeBatches=1,
        batchCount=1,
    )
    reader.epochs[1] = _epoch(
        committedCollateral=20_000_000, premiumEarned=9_600, performanceFee=960
    )
    reader.batches[1] = _batch(1, OTOKEN_1)
    add_option(reader, OTOKEN_1)
    reader.users[USER.lower()] = _user(sharesOf=1_000_000_000)
    service = make_service(reader)

    vault = service.get_vault("base-sepolia:eth-usdc-csp")
    position = service.get_user_position("base-sepolia:eth-usdc-csp", USER)
    service.get_vault("base-sepolia:eth-usdc-csp")

    assert vault.status == "active"
    assert vault.current_cycle.batches[0].status == "open"
    assert vault.current_cycle.batches[0].strike_price == "200000000000"
    assert position.actions.deposit.mode == "queued"
    assert position.actions.withdraw_idle.reason == "ACTIVE_BATCHES"
    assert position.actions.request_withdraw.available is True
    assert reader.calls["batches"] == 1
    assert reader.calls["options"] == 1


def test_settlement_classifications_remain_explicitly_provisional():
    reader = FakeReader()
    reader.global_values = _global(batchCount=3)
    reader.batches = {
        1: _batch(
            1,
            OTOKEN_1,
            settled=True,
            collateralReturned=20_000_000,
        ),
        2: _batch(
            2,
            OTOKEN_2,
            settled=True,
            collateral=22_000_000,
            collateralReturned=0,
            underlyingReceived=10**16,
        ),
        3: _batch(
            3,
            OTOKEN_3,
            settled=True,
            collateral=23_000_000,
            collateralReturned=21_000_000,
        ),
    }
    for address in (OTOKEN_1, OTOKEN_2, OTOKEN_3):
        add_option(reader, address)
    service = make_service(reader)

    response = service.get_vault("base-sepolia:eth-usdc-csp")
    statuses = {
        batch.batch_id: batch.status for batch in response.current_cycle.batches
    }

    assert statuses == {
        1: "otm_settled",
        2: "physical_delivered",
        3: "default_cash_settled",
    }
    assert all(
        batch.settlement_classification == "provisional"
        for batch in response.current_cycle.batches
    )


def test_closed_withdrawal_previews_usdc_and_weth_claim():
    reader = FakeReader()
    reader.global_values = _global(
        totalManagedAssets=800_000_000,
        totalShares=800_000_000,
    )
    reader.epochs[1] = _epoch(
        closed=True,
        endedAt=1_700_086_400,
        withdrawalAssetsPerShare=2 * 10**18,
        withdrawalAssetsRemaining=400_000_000,
        remainingWithdrawalClaims=2,
        withdrawalUnderlyingPerShare=3 * 10**16,
        withdrawalUnderlyingRemaining=6 * 10**15,
    )
    reader.users[USER.lower()] = _user(
        sharesOf=80_000_000,
        pendingWithdrawalShares=100_000_000,
        pendingWithdrawalEpoch=1,
    )
    service = make_service(reader)

    response = service.get_user_position("base-sepolia:eth-usdc-csp", USER)

    assert response.position.withdrawal.claimable is True
    assert response.position.withdrawal.usdc_assets == "200000000"
    assert response.position.withdrawal.weth_assets == "3000000"
    assert response.actions.claim_withdraw.available is True
    assert response.actions.request_withdraw.reason == "PENDING_WITHDRAWAL"


def test_assigned_weth_lazy_accrual_matches_share_generation_logic():
    reader = FakeReader()
    reader.global_values = _global(
        currentShareGeneration=2,
        cumulativeUnderlyingPerShare=4 * 10**18,
        accountedUnderlyingAssets=10**18,
    )
    reader.users[USER.lower()] = _user(
        sharesOf=100,
        shareGeneration=1,
        underlyingPerSharePaid=10**18,
        claimableAssignedUnderlying=5,
    )
    reader.cutoffs[1] = 3 * 10**18
    service = make_service(reader)

    response = service.get_user_position("base-sepolia:eth-usdc-csp", USER)

    assert response.position.active_shares == "0"
    assert response.position.claimable_assigned_weth == "205"
    assert response.actions.claim_assigned_weth.available is True
    assert response.actions.withdraw_idle.reason == "NO_ACTIVE_SHARES"
    assert reader.calls["generation:1"] == 1


def test_stale_snapshot_is_visible_but_disables_every_action():
    clock = Clock()
    reader = FakeReader()
    reader.global_values = _global(
        totalManagedAssets=1_000_000,
        totalShares=1_000_000,
        availableIdleAssets=1_000_000,
    )
    reader.users[USER.lower()] = _user(sharesOf=1_000_000)
    service = make_service(reader, clock)
    service.get_user_position("base-sepolia:eth-usdc-csp", USER)

    clock.value = 16
    reader.fail_global = True
    response = service.get_user_position("base-sepolia:eth-usdc-csp", USER)

    assert response.stale is True
    for action in response.actions.model_dump().values():
        assert action["available"] is False
        assert action["reason"] == "STALE_SNAPSHOT"


def test_snapshot_older_than_stale_window_is_not_served():
    clock = Clock()
    reader = FakeReader()
    service = make_service(reader, clock)
    service.get_vault("base-sepolia:eth-usdc-csp")

    clock.value = 61
    reader.fail_global = True
    with pytest.raises(CspRpcError):
        service.get_vault("base-sepolia:eth-usdc-csp")
