# Crypto Scanner

Independent 24/7 crypto market scanner and automated trading research system.

## Strict project isolation

This repository is independent from:

- Forex Scanner
- PASTICUAN / Super Scanner
- EMIR / Cuan-maksimal
- IDX Flow Scanner

It must not share runtime state, databases, secrets, positions, scoring, calibration, or execution state with those projects. Proven engineering patterns may be reused only as patterns.

## Safety contract

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

## Phase 1 public market data

The public Bybit Testnet layer requires no API key and currently provides:

- exact instrument metadata: tick size, quantity step, minimum order quantity, notional and leverage metadata
- ticker snapshot: last/mark/index price, best bid/ask, 24h volume/turnover, open interest and funding
- OHLCV intervals used by the scanner: 1m, 3m, 5m, 15m, 1h and 4h
- open-interest history
- funding-rate history
- WebSocket ticker stream
- WebSocket public trade stream with taker Buy/Sell side
- WebSocket orderbook depth 50 with snapshot/delta reconstruction
- fail-closed local book validation for stale updates and crossed/locked books

Numeric exchange values are parsed as `Decimal`, not binary floating point, so later sizing and price rounding can use exact exchange metadata.

Public connectivity smoke test:

```bash
crypto-scanner-public-smoke --websocket-seconds 12
```

GitHub-hosted runners may execute from a location whose source IP is rejected by Bybit with HTTP 403. The public smoke workflow classifies that specific hosted-runner condition as a diagnostic warning. Other HTTP, schema, WebSocket, data-quality, or orderbook failures remain fatal.

A successful GitHub unit/CI run proves the code contract but is not a substitute for venue-connectivity validation from the eventual permitted operational runtime host. The 24/7 scanner/execution engine must run from infrastructure eligible to access Bybit under the account's jurisdiction and Bybit terms; GitHub Actions remains CI/deployment tooling rather than the trading daemon.

## Phase 2 private account reads

Phase 2 is deliberately **read-only by construction**. The private gateway exposes only authenticated GET access to:

- Unified wallet balance
- USDT linear positions
- USDT linear open orders

The internal signed-request allowlist contains only these three paths. Unknown private paths are rejected before any network request. There are no create, amend, cancel, leverage-change, or other trading mutation methods in the Phase 2 client.

Authentication follows the Bybit V5 HMAC-SHA256 GET contract using one deterministic query string for both signature generation and transmission. Credentials are loaded only from:

- `BYBIT_TESTNET_API_KEY`
- `BYBIT_TESTNET_API_SECRET`

The credential dataclass suppresses both values from its representation. Missing credentials fail closed. Production endpoint overrides remain forbidden.

Once credentials are configured on an eligible operational host, the read-only smoke command is:

```bash
crypto-scanner-private-readonly-smoke
```

It prints balance, open positions, and open-order state but never prints credentials. Because GitHub-hosted runners can be source-IP blocked by Bybit, authenticated venue validation should be performed by the eventual permitted runtime rather than treated as a GitHub-hosted CI gate.

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

No API key is required for Phase 0 or Phase 1. Phase 2 unit tests use mocked credentials and never require real secrets.

## Secrets

Never commit secrets. During Phase 2, the only external secrets expected are the two **Bybit Testnet** credentials named above. Do not add LIVE/Mainnet credentials to this repository or its runtime configuration.

Supabase secrets are not required while persistence is deferred.

## Database

Bybit remains authoritative for exchange state. A dedicated Supabase project will later be used for durable scanner evidence, trajectories, closed trades, audit history, and calibration. Supabase is not required for Phases 0–2 code development.
