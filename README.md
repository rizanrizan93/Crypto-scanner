# Crypto Scanner

Independent 24/7 crypto market scanner and automated trading research system.

## Strict project isolation

This repository is independent from:

- Forex Scanner
- PASTICUAN / Super Scanner
- EMIR / Cuan-maksimal
- IDX Flow Scanner

It must not share runtime state, databases, secrets, positions, scoring, calibration, or execution state with those projects. Proven engineering patterns may be reused only as patterns.

## Phase 0 safety contract

Current venue and execution scope:

- **Bybit Testnet only**
- **USDT Perpetual focus**
- **LIVE trading hard locked**
- No production Bybit endpoints are accepted by runtime configuration
- Risk per trade: maximum 1% of account equity
- Concurrent positions: maximum 3
- One position per symbol
- Test phase leverage cap: 3x
- No martingale
- No averaging down
- No grid averaging
- No doubling after loss
- Supabase is currently optional/disabled until a dedicated Crypto Scanner database is created

Initial universe:

- BTCUSDT
- ETHUSDT
- SOLUSDT
- XRPUSDT
- BNBUSDT

## Planned architecture

1. **Discovery lane** — universe scan, regime/context, ranking, deeper analysis, candidate creation.
2. **Fast lane** — fresh market revalidation, structure/orderbook validation, entry geometry, execution readiness.
3. **Position management** — exchange-authoritative position monitoring, structural protection, TP/SL reconciliation, evidence-backed early exit.
4. **Closed-trade reconciliation** — fills, fees, funding, realized PnL, holding time, outcome.
5. **Calibration** — staged evidence review by setup, pair, direction, regime, time, MAE/MFE and trajectory.
6. **Monitoring** — Streamlit is monitoring only; it is never the execution engine.

## Build phases

- Phase 0 — repository skeleton, CI, config safety contract
- Phase 1 — Bybit Testnet public connectivity, instrument metadata, public market data
- Phase 2 — private account connectivity, balance/positions/orders read-only
- Phase 3 — technical discovery scanner
- Phase 4 — signal geometry and `EXECUTION_READY`
- Phase 5 — safe Testnet automatic execution
- Phase 6 — reconciliation and position management
- Phase 7 — trajectory, MAE/MFE and closed-trade evidence
- Phase 8 — adaptive calibration
- Phase 9 — professional monitoring dashboard

## Local development

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check .
pytest
```

No API key is required for Phase 0 CI.

## Secrets

Never commit secrets. Future Testnet private connectivity will use GitHub Secrets. Expected names are deliberately not required yet; they will be finalized in Phase 2.

## Database

Bybit remains authoritative for exchange state. A dedicated Supabase project will later be used for durable scanner evidence, trajectories, closed trades, audit history, and calibration. Supabase is not required for Phase 0 or public Phase 1 work.
