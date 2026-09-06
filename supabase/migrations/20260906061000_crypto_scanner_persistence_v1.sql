begin;

create table if not exists public.schema_meta (
    key text primary key,
    value text not null,
    updated_at timestamptz not null default now()
);

insert into public.schema_meta(key, value)
values ('schema_version', 'crypto-scanner-persistence-v1')
on conflict (key) do update
set value = excluded.value, updated_at = now();

create table if not exists public.scanner_runs (
    run_id text primary key,
    started_at_ms bigint not null check (started_at_ms >= 0),
    completed_at_ms bigint check (completed_at_ms is null or completed_at_ms >= started_at_ms),
    status text not null,
    source text not null,
    git_sha text,
    execution_armed boolean not null default false,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.market_snapshots (
    run_id text not null references public.scanner_runs(run_id) on delete cascade,
    symbol text not null,
    captured_at_ms bigint not null check (captured_at_ms >= 0),
    last_price numeric,
    mark_price numeric,
    index_price numeric,
    bid_price numeric,
    ask_price numeric,
    volume_24h numeric,
    open_interest numeric,
    funding_rate numeric,
    raw jsonb not null default '{}'::jsonb,
    primary key (run_id, symbol, captured_at_ms)
);

create table if not exists public.pair_rankings (
    run_id text not null references public.scanner_runs(run_id) on delete cascade,
    symbol text not null,
    ranked_at_ms bigint not null check (ranked_at_ms >= 0),
    rank integer not null check (rank > 0),
    score numeric not null,
    direction text,
    status text not null,
    regime text,
    evidence_coverage numeric,
    reasons jsonb not null default '[]'::jsonb,
    primary key (run_id, symbol)
);

create table if not exists public.signals (
    signal_id text primary key,
    run_id text references public.scanner_runs(run_id) on delete set null,
    symbol text not null,
    direction text not null check (direction in ('LONG', 'SHORT')),
    setup text not null,
    regime text,
    status text not null,
    score numeric,
    created_at_ms bigint not null check (created_at_ms >= 0),
    expires_at_ms bigint,
    evidence jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.signal_geometry (
    signal_id text primary key references public.signals(signal_id) on delete cascade,
    entry_mode text not null,
    entry_price numeric not null check (entry_price > 0),
    stop_loss numeric not null check (stop_loss > 0),
    tp1 numeric check (tp1 is null or tp1 > 0),
    tp2 numeric not null check (tp2 > 0),
    risk_per_unit numeric not null check (risk_per_unit > 0),
    rr_tp1 numeric,
    rr_tp2 numeric not null,
    geometry_created_at_ms bigint not null check (geometry_created_at_ms >= 0),
    raw jsonb not null default '{}'::jsonb
);

create table if not exists public.orders (
    client_order_id text primary key,
    venue_order_id text unique,
    signal_id text references public.signals(signal_id) on delete set null,
    symbol text not null,
    side text not null check (side in ('BUY', 'SELL')),
    position_side text not null default 'BOTH',
    order_type text not null,
    status text not null,
    qty numeric not null check (qty > 0),
    price numeric,
    avg_price numeric,
    reduce_only boolean not null default false,
    close_position boolean not null default false,
    created_at_ms bigint,
    updated_at_ms bigint,
    raw jsonb not null default '{}'::jsonb
);

create table if not exists public.fills (
    symbol text not null,
    venue_trade_id text not null,
    venue_order_id text,
    client_order_id text,
    side text not null check (side in ('BUY', 'SELL')),
    position_side text not null default 'BOTH',
    qty numeric not null check (qty > 0),
    price numeric not null check (price > 0),
    quote_qty numeric,
    realized_pnl numeric not null default 0,
    commission numeric not null default 0,
    commission_asset text,
    maker boolean,
    time_ms bigint not null check (time_ms >= 0),
    raw jsonb not null default '{}'::jsonb,
    primary key (symbol, venue_trade_id)
);

create table if not exists public.positions (
    position_id text primary key,
    signal_id text references public.signals(signal_id) on delete set null,
    venue text not null default 'BINANCE',
    environment text not null default 'DEMO',
    symbol text not null,
    direction text not null check (direction in ('LONG', 'SHORT')),
    state text not null check (state in ('OPEN', 'CLOSED')),
    entry_qty numeric not null check (entry_qty > 0),
    remaining_qty numeric not null check (remaining_qty >= 0),
    average_entry_price numeric not null check (average_entry_price > 0),
    initial_stop_loss numeric,
    tp1 numeric,
    tp2 numeric,
    leverage numeric,
    opened_at_ms bigint not null check (opened_at_ms >= 0),
    closed_at_ms bigint,
    latest_mark_price numeric,
    unrealized_pnl numeric,
    updated_at_ms bigint not null check (updated_at_ms >= opened_at_ms),
    source jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists ux_positions_one_open_symbol
on public.positions(venue, environment, symbol)
where state = 'OPEN';

create table if not exists public.position_trajectory (
    position_id text not null references public.positions(position_id) on delete cascade,
    measured_until_ms bigint not null check (measured_until_ms >= 0),
    state text not null check (state in ('OPEN', 'CLOSED')),
    current_price numeric not null check (current_price > 0),
    reference_qty numeric not null check (reference_qty > 0),
    favorable_extreme_price numeric not null check (favorable_extreme_price > 0),
    adverse_extreme_price numeric not null check (adverse_extreme_price > 0),
    mfe_per_unit numeric not null check (mfe_per_unit >= 0),
    mae_per_unit numeric not null check (mae_per_unit >= 0),
    mfe_pct numeric not null check (mfe_pct >= 0),
    mae_pct numeric not null check (mae_pct >= 0),
    current_pnl_per_unit numeric not null,
    initial_stop_loss numeric,
    mfe_r numeric,
    mae_r numeric,
    holding_time_ms bigint not null check (holding_time_ms >= 0),
    observation_count integer not null check (observation_count >= 0),
    quality text not null,
    history_complete boolean not null,
    realized_pnl numeric,
    commission numeric,
    funding_fee numeric,
    net_pnl numeric,
    calibration_eligible boolean not null default false,
    note text,
    created_at timestamptz not null default now(),
    primary key (position_id, measured_until_ms)
);

create table if not exists public.closed_trades (
    trade_key text primary key,
    position_id text not null unique references public.positions(position_id) on delete cascade,
    signal_id text references public.signals(signal_id) on delete set null,
    symbol text not null,
    direction text not null check (direction in ('LONG', 'SHORT')),
    entry_time_ms bigint not null,
    exit_time_ms bigint not null check (exit_time_ms >= entry_time_ms),
    entry_qty numeric not null check (entry_qty > 0),
    exit_qty numeric not null check (exit_qty > 0),
    average_entry_price numeric not null check (average_entry_price > 0),
    average_exit_price numeric not null check (average_exit_price > 0),
    realized_pnl numeric not null,
    commission numeric not null,
    funding_fee numeric not null,
    net_pnl numeric not null,
    mfe_pct numeric,
    mae_pct numeric,
    mfe_r numeric,
    mae_r numeric,
    holding_time_ms bigint not null,
    history_complete boolean not null,
    observation_count integer not null,
    calibration_eligible boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.calibration_stats (
    calibration_id text primary key,
    generated_at_ms bigint not null check (generated_at_ms >= 0),
    symbol text,
    setup text,
    regime text,
    direction text,
    sample_size integer not null check (sample_size >= 0),
    win_rate numeric,
    expectancy_r numeric,
    median_mae_r numeric,
    median_mfe_r numeric,
    profit_factor numeric,
    applied boolean not null default false,
    config_version text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.runtime_state (
    state_key text primary key,
    version bigint not null default 1 check (version > 0),
    state jsonb not null default '{}'::jsonb,
    updated_at_ms bigint not null check (updated_at_ms >= 0),
    updated_at timestamptz not null default now()
);

create table if not exists public.heartbeats (
    component text primary key,
    observed_at_ms bigint not null check (observed_at_ms >= 0),
    status text not null,
    git_sha text,
    details jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create table if not exists public.system_events (
    event_id text primary key,
    occurred_at_ms bigint not null check (occurred_at_ms >= 0),
    severity text not null,
    component text not null,
    code text not null,
    symbol text,
    detail text not null,
    context jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists ix_market_snapshots_symbol_time
    on public.market_snapshots(symbol, captured_at_ms desc);
create index if not exists ix_pair_rankings_time
    on public.pair_rankings(ranked_at_ms desc, rank);
create index if not exists ix_signals_symbol_time
    on public.signals(symbol, created_at_ms desc);
create index if not exists ix_orders_symbol_time
    on public.orders(symbol, updated_at_ms desc);
create index if not exists ix_fills_symbol_time
    on public.fills(symbol, time_ms desc);
create index if not exists ix_positions_symbol_state
    on public.positions(symbol, state, updated_at_ms desc);
create index if not exists ix_trajectory_position_time
    on public.position_trajectory(position_id, measured_until_ms desc);
create index if not exists ix_closed_trades_symbol_exit
    on public.closed_trades(symbol, exit_time_ms desc);
create index if not exists ix_system_events_time
    on public.system_events(occurred_at_ms desc);

-- Backend-only persistence. No browser/client role receives table access.
do $$
declare
    table_name text;
begin
    foreach table_name in array array[
        'schema_meta',
        'scanner_runs',
        'market_snapshots',
        'pair_rankings',
        'signals',
        'signal_geometry',
        'orders',
        'fills',
        'positions',
        'position_trajectory',
        'closed_trades',
        'calibration_stats',
        'runtime_state',
        'heartbeats',
        'system_events'
    ]
    loop
        execute format('alter table public.%I enable row level security', table_name);
        execute format('revoke all on table public.%I from PUBLIC', table_name);
        execute format('revoke all on table public.%I from anon, authenticated', table_name);
        execute format(
            'grant select, insert, update, delete on table public.%I to service_role',
            table_name
        );
    end loop;
end $$;

commit;
