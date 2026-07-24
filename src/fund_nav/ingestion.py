"""Operational entry point for verified CSP option observations."""

from collections.abc import Callable
from typing import Any

from web3 import Web3

from src.config import settings
from src.fund_nav.observations import ObservationIngestor, OptionObservation
from src.fund_nav.runtime import (
    SupabaseNavRepository,
    TrustedFund,
    TrustedRegistryLoader,
    Web3ReporterGateway,
)


def ingest_observation_document(
    document: dict[str, Any],
    *,
    repository: SupabaseNavRepository | None = None,
    funds: list[TrustedFund] | None = None,
    gateway_factory: Callable[[TrustedFund], Any] | None = None,
) -> str:
    """Validate and persist one signed observation through the trusted runtime."""
    observation = OptionObservation(**document)
    repository = repository or SupabaseNavRepository()
    funds = funds if funds is not None else TrustedRegistryLoader(repository).load()
    fund = next(
        (
            item
            for item in funds
            if item.chain_id == observation.chain_id
            and item.address == observation.fund_address.lower()
        ),
        None,
    )
    if fund is None:
        raise ValueError("UNKNOWN_TRUSTED_FUND")
    if fund.trust_reason:
        raise RuntimeError(fund.trust_reason)

    if gateway_factory is None:
        if not settings.rpc_url:
            raise RuntimeError("RPC_URL_REQUIRED")
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url))

        def build_gateway(selected: TrustedFund) -> Web3ReporterGateway:
            return Web3ReporterGateway(w3, selected, repository)

        gateway_factory = build_gateway

    return ObservationIngestor(gateway_factory(fund), repository).ingest(observation)
