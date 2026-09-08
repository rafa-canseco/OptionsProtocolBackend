"""Operational entry point for verified option-fund observations."""

from collections.abc import Callable
from typing import Any

from src.config import get_tokenized_fund_rpc_url
from src.contracts.web3_client import create_validated_backend_w3
from src.fund_nav.observations import ObservationIngestor, OptionObservation
from src.fund_nav.runtime import (
    IdempotentObservationStore,
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
        rpc_url = get_tokenized_fund_rpc_url()
        if not rpc_url:
            raise RuntimeError("TOKENIZED_FUND_RPC_URL_OR_RPC_URL_REQUIRED")
        w3 = create_validated_backend_w3(
            rpc_url,
            fund.chain_id,
            "TOKENIZED_FUND_RPC_URL",
        )

        def build_gateway(selected: TrustedFund) -> Web3ReporterGateway:
            return Web3ReporterGateway(w3, selected, repository)

        gateway_factory = build_gateway

    return ObservationIngestor(
        gateway_factory(fund),
        IdempotentObservationStore(
            repository,
            fund.registry.get("strategy_kind", "csp"),
        ),
    ).ingest(observation)
