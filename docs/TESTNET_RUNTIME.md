# Bybit Testnet Operational Runtime

This document applies only to the independent Crypto Scanner project.

## Runtime boundary

GitHub-hosted Actions remains CI/CD only. The 24/7 scanner and execution process must run on an operational host whose network is eligible to access Bybit Testnet.

LIVE/Mainnet remains hard locked by application code. Production Bybit endpoints are rejected.

## Required secrets

The operational host requires only the Bybit Testnet credentials at this stage:

- `BYBIT_TESTNET_API_KEY`
- `BYBIT_TESTNET_API_SECRET`

Never commit either value.

## Mandatory preflight state

Before any Testnet order write:

- `CRYPTO_SCANNER_TESTNET_EXECUTION=DISABLED`
- public Testnet access must succeed for the configured universe
- contract metadata and best bid/ask must be valid
- private UNIFIED wallet read must succeed
- Testnet equity must be positive
- positions and open-order reads must succeed

Run:

```bash
crypto-scanner-runtime-preflight
```

A valid operational host returns `preflight_status: PASS_DISARMED`.

The preflight command never creates, amends, or cancels an order.

## Container preflight

Create `.env.runtime` on the host from `.env.runtime.example` and provide the Testnet credentials without committing the file. Keep execution disabled.

```bash
docker compose run --rm crypto-scanner-preflight
```

The container runs as a non-root user, uses a read-only filesystem in Compose, and defaults Testnet execution to disabled.

## Arming boundary

`CRYPTO_SCANNER_TESTNET_EXECUTION=ENABLED` is permitted only after the operational host has passed the disarmed public/private preflight. Enabling it does not bypass signal geometry, freshness, risk sizing, position-count, concentration, exchange metadata, or reconciliation guards.

The first real Testnet order must use the smallest valid risk-sized quantity produced by the engine, carry server-side stop protection, and be reconciled by deterministic `orderLinkId`. A submission acknowledgement is not treated as a fill. Unknown transport outcome must be reconciled before any further submit attempt.
