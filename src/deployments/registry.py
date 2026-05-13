"""Public deployment registry for Agora testnet routes."""

from pydantic import BaseModel

from src.config import settings


class ArcDeployment(BaseModel):
    chain: str = "arc"
    chain_id: int
    cctp_domain: int
    usdc: str
    metavault_address: str
    rpc_configured: bool


class BaseSepoliaDeployment(BaseModel):
    chain: str = "base_sepolia"
    chain_id: int = 84532
    cctp_domain: int
    usdc: str
    vault_adapter: str


class SolanaDevnetDeployment(BaseModel):
    chain: str = "solana_devnet"
    cluster: str
    cctp_domain: int
    usdc: str


class DeploymentRegistry(BaseModel):
    environment: str
    arc: ArcDeployment
    base_sepolia: BaseSepoliaDeployment
    solana_devnet: SolanaDevnetDeployment


def get_deployment_registry() -> DeploymentRegistry:
    """Return non-secret addresses/domains for frontend, agents, and backend flows."""
    return DeploymentRegistry(
        environment=settings.app_env,
        arc=ArcDeployment(
            chain_id=settings.arc_chain_id,
            cctp_domain=settings.cctp_domain_arc,
            usdc=settings.arc_usdc,
            metavault_address=settings.arc_metavault_address,
            rpc_configured=bool(settings.arc_testnet_rpc),
        ),
        base_sepolia=BaseSepoliaDeployment(
            cctp_domain=settings.cctp_domain_base,
            usdc=settings.base_sepolia_usdc or settings.usdc_address,
            vault_adapter=settings.base_sepolia_vault_adapter,
        ),
        solana_devnet=SolanaDevnetDeployment(
            cluster=settings.solana_cluster,
            cctp_domain=settings.cctp_domain_solana,
            usdc=settings.solana_devnet_usdc or settings.solana_usdc_mint,
        ),
    )
