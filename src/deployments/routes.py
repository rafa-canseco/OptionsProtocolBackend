"""Deployment registry API."""

from fastapi import APIRouter

from src.deployments.registry import DeploymentRegistry, get_deployment_registry

router = APIRouter(prefix="/api/deployments", tags=["Deployments"])


@router.get("/registry", response_model=DeploymentRegistry)
async def deployment_registry():
    """Return public deployment addresses and CCTP domains for current env."""
    return get_deployment_registry()
