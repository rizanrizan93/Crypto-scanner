# Binance Futures Demo operational runtime

This document applies only to the independent Crypto Scanner project.

## Hard boundary

The application accepts only Binance Futures Demo URLs. Mainnet/LIVE remains locked in code.
GitHub Actions is a bounded scanner/maintenance runner, not authorization for production trading.

Required secrets:

- `BINANCE_DEMO_API_KEY`
- `BINANCE_DEMO_API_SECRET`
- `CRYPTO_SCANNER_SUPABASE_URL`
- `CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY`

The Supabase credentials must belong to the dedicated Crypto Scanner project. Never reuse another
project's database or service-role key.

## Disarmed preflight

Keep `CRYPTO_SCANNER_TESTNET_EXECUTION=DISABLED`, then run:

```bash
crypto-scanner-runtime-preflight
crypto-scanner-persistence-smoke
crypto-scanner-phase6-audit
```

Preflight must prove Demo endpoint identity, credentials, positive account state, One-way Mode,
instrument metadata, and durable schema compatibility. It performs no order writes.

## Arming

`CRYPTO_SCANNER_TESTNET_EXECUTION=ENABLED` permits Demo writes only; all signal, freshness, sizing,
portfolio-risk, leverage, protection, and reconciliation gates still apply.

The scheduled `Crypto Scanner Demo Runtime` workflow is disarmed for entry. Manual dispatch exposes
`enable_demo_orders`; setting it to true permits exactly one scanner cycle. Lifecycle maintenance
remains serialized and armed so it can repair protection, ratchet stops, close an unrecoverably
unprotected scanner position, and remove scanner-owned orphans.

Before manually arming, require all of the following:

1. CI and disarmed public/private/persistence preflight pass.
2. Phase 6 reports no unsafe or ambiguous open position.
3. No unresolved post-fill or stack transaction blocker exists.
4. The current calibration report is reviewed; a small sample is not evidence of edge.
5. The operator accepts that Binance Demo orders—not LIVE orders—may be created.

## Post-fill protection and recovery

Entry ACK is not a fill. The coordinator reconciles the deterministic entry id and user trades,
checks exact net-position side/quantity, validates actual fill risk, then compares planned SL/TP2
with current `MARK_PRICE` using a two-tick/one-basis-point safety gap.

The required exchange state is exactly one full-size `reduceOnly` STOP_MARKET and one full-size
`reduceOnly` TAKE_PROFIT_MARKET (TP2). TP1 is an advisory analytical checkpoint, not an order.

If trigger validation or protector submission fails:

1. submit one deterministic full-size `reduceOnly` market exit;
2. reconcile that exact client id, including unknown transport outcomes;
3. verify Binance reports the symbol flat;
4. cancel only scanner-owned (`cs-`) orphan conditional orders;
5. persist a `...FLATTENED` failure status.

If the emergency exit cannot be proven filled, runtime fails with
`FILLED_PROTECTION_FAILED_EMERGENCY_EXIT_FAILED`; this is an operator blocker and must not be retried
blindly.

## TP1/TP2 policy

Partial TP1 execution is intentionally disabled until an event-driven partial-fill handler can
resize the stop immediately and audit the new quantity. This avoids an over-sized reduce-only stop
between TP1 fill and a later polling cycle. TP2 and SL therefore protect the full remaining position.

## Incident checklist

When a runtime job fails:

1. leave automatic entry disarmed;
2. inspect the order status and deterministic client ids in durable storage;
3. inspect Binance position plus open algo orders using read-only commands;
4. run lifecycle maintenance once only when the identity chain is unambiguous;
5. confirm flat/protected state with Phase 6 audit before any new manual entry cycle.

Never solve an unknown write result by submitting a second order with a new identity.
