# B1N-352/B1N-353: contrato de integración frontend

Este documento es el handoff vigente para el frontend de la instancia
`base-sepolia:csp`. El backend es de lectura de producto: prepara estado,
capacidades y razones de bloqueo; las transacciones de usuario se firman en la
wallet y se envían directamente al `FundVault`.

## Deployment que consume staging

- Red: Base Sepolia, chain ID `84532`.
- Manifest: `blockchain/deployments/base-sepolia/b1n-352/v2/manifest.json`.
- Estado del registro: `DEPLOYED`.
- FundVault proxy: `0x53e38Baf2fC55259729085b7542BFF066F6a509e`.
- Share token: `0x07Db1F574ecCFD15c4A8bd4582e5d25baA84De7d`.
- Accounting (USDC): `0x21d3acc5a2c64666dA93ABC8c77AB483b96836a3`.
- FundFlowManager: `0x0206C0A5050b09B7A2AD4E8CbF83a06ae2193080`.
- NavReportVerifier: `0x3093a0f0634aF2991ACD265d1f1F325480113d40`.
- ClaimEscrow: `0xf0E540dfb0D3d8c3eDfCB5B5E457CafE116C5439`.

Staging tiene el indexer habilitado, el reporter continuo deshabilitado y
usa Base Sepolia RPC/WSS públicos. La consulta read-only a
`/v2/vaults/base-sepolia:csp/config` devuelve `deploymentStatus=DEPLOYED`, las
direcciones anteriores y `writesEnabled=false`; en este momento la razón es
`STALE_NAV_WINDOW`. Es el comportamiento esperado: no se habilita ningún write
mientras el estado indexado/NAV no sea fresco.

## API de producto

Todos los valores de tokens son **enteros en unidades mínimas**, serializados
como strings decimales. Para este deployment el accounting asset es USDC (6
decimales) y las shares son `b1CSP` (18 decimales). Las direcciones son EVM.

| Endpoint | Uso |
| --- | --- |
| `GET /v2/vaults` | Lista de fondos habilitados y metadatos de tokens. |
| `GET /v2/vaults/{fundKey}` | NAV, supply, precio de share, composición, estado, ventana NAV y disponibilidad de acciones. |
| `GET /v2/vaults/{fundKey}/positions/{address}` | Shares del usuario, valor en accounting asset, redención y acciones disponibles. |
| `GET /v2/vaults/{fundKey}/redemptions/{address}` | Alias de la posición; facilita una pantalla dedicada de redención. |
| `GET /v2/vaults/{fundKey}/config` | Bindings confiables, capabilities, `writesEnabled` y `blockedReasonCode`. |
| `GET /v2/vaults/{fundKey}/activity?cursor=...&limit=20` | Actividad paginada; `limit` de 1 a 100. |

El backend no expone endpoints de escritura para depósito o redención. El
frontend debe consultar `actions.*` y sólo presentar/enviar una transacción si
la acción está disponible. El estado mostrado puede sobrevivir a un bloqueo,
pero toda acción de escritura se deshabilita fail-closed.

## Modelo de redención

La redención es asíncrona y usa un único `requestId = 0` (la constante
ERC-7540 del fondo). El campo `redemption.status` puede ser:

- `none`: no hay solicitud activa.
- `pending`: shares en la cola del batch abierto.
- `claimable`: shares procesadas y assets reservados para reclamar.
- `cancelled`: la solicitud pendiente fue cancelada.
- `claimed`: se consumió el claim; puede volver a `none` cuando no quedan saldos.

`pendingShares` y `claimableShares` están en unidades de share; `claimableAssets`
está en unidades mínimas de USDC. `nextAction` es `none`, `cancel_or_wait` o
`claim`.

El procesamiento normal es:

1. `requestRedeem` bloquea shares y devuelve `requestId=0`.
2. Un batch procesa la solicitud y emite `ClaimReserved`; el usuario pasa a
   `claimable`.
3. El usuario consume el claim con `redeem` (por shares) o `withdraw` (por
   assets), y recibe USDC.
4. Si el batch aún no está sellado/procesándose, el usuario puede cancelar la
   parte pendiente.

No existe una función Solidity llamada `claim`; “claim” en la API significa
`redeem`/`withdraw` sobre una solicitud ya claimable. Assigned WETH es inventario
del fondo y no un derecho individual del holder.

## ABI de writes (FundVault v2)

Funciones de usuario relevantes:

```solidity
deposit(uint256 assets, address receiver)
    returns (uint256 shares)

depositWithMinShares(uint256 assets, address receiver, uint256 minSharesOut)
    returns (uint256 shares)

requestRedeem(uint256 shares, address controller, address owner)
    returns (uint256 requestId) // siempre 0 en este fondo

requestRedeemWithMinAssets(
    uint256 shares, address controller, address owner, uint256 minAssetsOut
) returns (uint256 requestId)

cancelPending(uint256 shares)
cancelRedeemRequest(address controller, uint256 shares)

redeem(uint256 shares, address receiver, address controller)
    returns (uint256 assets)

withdraw(uint256 assets, address receiver, address controller)
    returns (uint256 shares)
```

Lecturas que deben usarse para preflight/estado:

```solidity
asset() returns (address)
maxDeposit(address receiver) returns (uint256)
previewDeposit(uint256 assets) returns (uint256 shares)
pendingRedeemRequest(uint256 requestId, address controller) returns (uint256 shares)
claimableRedeemRequest(uint256 requestId, address controller) returns (uint256 shares)
maxRedeem(address controller) returns (uint256 shares)
maxWithdraw(address controller) returns (uint256 assets)
redemptionsPaused() returns (bool)
depositsPaused() returns (bool)
```

`deposit` es el ERC-4626 estándar y no tiene protección de mínimo; cuando se
requiera slippage usar `depositWithMinShares`. La cancelación es una extensión
del fondo, no una operación definida por ERC-7540. `redeem`/`withdraw` exigen
que `msg.sender` sea el controller o un operator aprobado para él.

## Fail-closed

Las cuatro acciones (`deposit`, `requestRedemption`, `cancelRedemption`,
`claimRedemption`) se deshabilitan si falta cualquiera de estas condiciones:

- registry `DEPLOYED` y bindings/implementaciones confiables del manifest;
- snapshot reconciliado y head confirmado fresco;
- ventana NAV activa y no expirada;
- ausencia de lock o procesamiento incompatible;
- pausa o saldo insuficiente según la operación.

Las razones son códigos estables, por ejemplo `STALE_NAV_WINDOW`,
`STALE_CONFIRMED_HEAD`, `UNRECONCILED`, `DEPOSITS_PAUSED`,
`REDEMPTIONS_PAUSED`, `NO_PENDING_REDEMPTION` y
`NO_CLAIMABLE_REDEMPTION`. El frontend no debe inferir disponibilidad sólo por
el balance ni reintentar writes bloqueados.
