# Crypto Scanner persistence

This database is dedicated to `rizanrizan93/Crypto-scanner`. It must not share runtime state, tables, service-role credentials, positions, calibration, or execution records with Forex Scanner, PASTICUAN, EMIR, or IDX Flow Scanner.

## Schema

Migration:

`supabase/migrations/20260906061000_crypto_scanner_persistence_v1.sql`

Schema version:

`crypto-scanner-persistence-v1`

The migration creates backend-only tables for scanner runs, market snapshots, rankings, signals, signal geometry, orders, fills, positions, position trajectory, closed trades, calibration statistics, runtime state, heartbeats, and system events.

RLS is enabled on every persistence table. `PUBLIC`, `anon`, and `authenticated` receive no table privileges. The backend `service_role` is the only runtime role granted read/write access.

## Runtime secrets

Configure both values together:

- `CRYPTO_SCANNER_SUPABASE_URL`
- `CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY`

Never commit or print the service-role key. Do not reuse a key from any IDX or Forex project.

If neither variable exists, Phase 7 remains usable with the optional JSON diagnostic sink. If only one variable exists, runtime fails closed as a configuration error.

## Persistence behavior

Phase 7 uses deterministic position identity based on:

`BINANCE | DEMO | symbol | direction | entry_time_ms`

The identifier is SHA-256 derived and therefore stable across repeated GitHub jobs and across the OPEN -> CLOSED transition. Writes are idempotent PostgREST upserts.

Write order is:

1. `positions`
2. `position_trajectory`
3. `closed_trades` when the episode is closed

Before any write, runtime verifies the `schema_meta.schema_version` row. A mismatch blocks persistence instead of silently writing against an incompatible schema.

## Calibration guard

Connecting Supabase does not by itself make a trajectory calibration-eligible. Phase 7 keeps `calibration_eligible=false` until the automated execution path durably links the original signal and its initial stop to the position. This prevents reconstructed or remembered stop values from being used as R-multiple evidence.

## Smoke check

After the migration and GitHub secrets are configured, run:

`crypto-scanner-persistence-smoke`

It performs a read-only schema/authentication check and expects:

`PASS_PERSISTENCE_READONLY`
