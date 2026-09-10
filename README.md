# Crypto Scanner

Independent Binance Futures Demo scanner, execution-safety engine, and trading-research system.

## Runtime boundary

- Venue: **Binance Futures Demo only** (`https://testnet.binancefuture.com`)
- Product: USDT perpetual futures
- Production/LIVE hosts are rejected in code
- Entry writes are disabled unless `CRYPTO_SCANNER_TESTNET_EXECUTION=ENABLED`
- Scheduled and push workflows observe only; a Demo entry cycle requires an explicit manual dispatch
- Binance is authoritative for wallet, positions, fills, and active protection
- A dedicated Crypto Scanner Supabase project is required for managed execution and durable evidence

This repository must not share secrets, database state, signals, positions, or calibration data with
Forex Scanner, IDX Flow Scanner, PASTICUAN, EMIR, or any other project.

## Safety contract

| Control | Current limit |
|---|---:|
| Default planned risk per entry | 0.50% equity |
| Hard risk ceiling per entry | 1.00% equity |
| Aggregate planned portfolio risk | 5.00% equity |
| Logical risk slots | 10 |
| High-correlation BTC/ETH/SOL slots | 2 |
| Same-symbol layers | 3, profitable stacking only |
| Leverage | maximum 3x |

Martingale, averaging down, grid averaging, and doubling after a loss are prohibited. A fresh
same-symbol entry is rejected unless it passes the dedicated profitable-stacking transaction.

After a market fill, the engine re-reconciles fills and the net position, recalculates actual
stop-risk using the fill price, and validates SL/TP2 against the current Binance mark price. If
protection is stale, rejected, or cannot be proven active, a deterministic `reduceOnly` market exit
is sent once, reconciled by client id, the flat state is verified, and scanner-owned orphan
protectors are removed. Unknown write outcomes are never blindly retried.

## Entry, SL, and TP contract

Discovery ranks candidates from closed-candle 5m, 15m, and 1h evidence. The fast lane then requires
fresh quote/order-book data, spread and chase limits, directional confirmation, sufficient evidence
coverage, and fresh structural geometry before a signal becomes `EXECUTION_READY`.

- Entry: market order only after all hard gates pass.
- SL: beyond the confirmed invalidation swing plus a bounded ATR/tick buffer.
- TP1: a durable analytical checkpoint used for geometry and outcome analysis; it is **not** an
  exchange order yet.
- TP2: the single full-size exchange-side take-profit paired with the full-size stop.
- Profit lock: after sufficient MFE, the full-size stop ratchets in bounded 0.50R steps; replacement
  installs and proves the new stop and TP2 before canceling the old pair.

TP1 remains advisory deliberately. Activating a partial TP without event-driven stop resizing could
temporarily leave the remaining stop quantity larger than the position. The result object therefore
reports `tp1_execution_mode=ADVISORY_CHECKPOINT` and `tp1_client_algo_id=null` explicitly.

## Calibration contract

Calibration uses only closed trades with a complete durable
`signal -> geometry -> order -> fill -> position` identity chain and complete trajectory history.

- Fewer than 50 eligible trades: observe only; no parameter mutation.
- 50–99: bounded adjustment, requiring at least 20 new samples.
- 100–199: stronger bounded adjustment, requiring at least 30 new samples.
- 200+: serious calibration, requiring at least 50 new samples.
- Single-factor attribution: at least 50 total samples and 15 per TRUE/FALSE group.
- Interaction attribution: at least 100 total samples and 25 per group.

Calibration never mutates the active strategy directly. It queues a versioned challenger, which
must pass the automated promotion pipeline:

1. Twelve complete months of point-in-time 5m replay over five configured symbols, including an
   8 bps round-trip friction assumption and conservative SL-first intrabar handling.
2. At least 200 historical trades, at least 50 OOS trades, OOS profit factor >=1.20,
   OOS expectancy >=0.10R, drawdown <=10R, three of four positive chronological folds, and two
   neighboring-parameter robustness checks with profit factor >=1.05.
3. Forward Binance Futures Demo evidence from at least 30 complete linked trades across at least
   14 days, profit factor >=1.10, expectancy >=0.05R, drawdown <=6R, and zero protection incidents.

Passing history changes the challenger only to `FORWARD_DEMO`; passing forward evidence promotes
it to champion automatically. Weak forward evidence rolls back to the prior champion. A protection
incident quarantines execution. Every signal records its strategy version, promotion stage, and
exact bounded parameters, so forward evidence cannot be mixed across versions.

Risk, leverage, position limits, and the LIVE lock are never calibration targets. Auto-promotion
authorizes Binance Futures Demo only; LIVE remains hard-locked in code.

## Main components

1. Public market layer: instruments, ticker, OHLCV, open interest, funding, trades, and order book.
2. Discovery lane: regime, trend/structure, momentum, volatility, context, and candidate ranking.
3. Fast lane: freshness, microstructure, geometry, and final execution readiness.
4. Durable execution: deterministic identities, sizing, post-fill validation, SL/TP2, emergency exit.
5. Position management: protection audit, profit lock, stacking recovery, and orphan cleanup.
6. Evidence: fills, trajectories, MFE/MAE, fees/funding, closed trades, and health events.
7. Research/calibration: bounded runtime parameters, attribution, and point-in-time historical replay.

## Local development

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
```

Useful read-only/disarmed commands:

```bash
crypto-scanner-public-smoke
crypto-scanner-private-readonly-smoke
crypto-scanner-runtime-preflight
crypto-scanner-phase6-audit
crypto-scanner-persistence-smoke
crypto-scanner-historical-research --help
crypto-scanner-strategy-promote
```

See [docs/TESTNET_RUNTIME.md](docs/TESTNET_RUNTIME.md) for operational arming and recovery rules and
[docs/PERSISTENCE.md](docs/PERSISTENCE.md) for the dedicated database contract.

## Secrets

Copy `.env.runtime.example` locally and provide only Demo/dedicated-project credentials:

- `BINANCE_DEMO_API_KEY`
- `BINANCE_DEMO_API_SECRET`
- `CRYPTO_SCANNER_SUPABASE_URL`
- `CRYPTO_SCANNER_SUPABASE_SERVICE_ROLE_KEY`

Never commit or print these values. Never place Binance production credentials in this runtime.
