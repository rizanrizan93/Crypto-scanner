begin;

create table if not exists public.strategy_forward_evaluations (
    evaluation_id text primary key,
    source_signal_id text not null unique references public.signals(signal_id) on delete cascade,
    strategy_id text not null,
    promotion_stage text not null,
    symbol text not null,
    direction text not null check (direction in ('LONG', 'SHORT')),
    strategy_timeframe text not null,
    execution_timeframe text not null default '5',
    setup text not null,
    regime text not null default 'UNKNOWN',
    signal_created_at_ms bigint not null check (signal_created_at_ms >= 0),
    score numeric,
    planned_entry_price numeric not null check (planned_entry_price > 0),
    planned_stop_loss numeric not null check (planned_stop_loss > 0),
    planned_tp1 numeric check (planned_tp1 is null or planned_tp1 > 0),
    planned_tp2 numeric not null check (planned_tp2 > 0),
    planned_rr_tp2 numeric not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists public.strategy_paper_trades (
    paper_trade_id text primary key,
    evaluation_id text not null unique references public.strategy_forward_evaluations(evaluation_id) on delete cascade,
    strategy_id text not null,
    promotion_stage text not null,
    symbol text not null,
    direction text not null check (direction in ('LONG', 'SHORT')),
    strategy_timeframe text not null,
    execution_timeframe text not null default '5',
    regime text not null default 'UNKNOWN',
    decision_time_ms bigint not null check (decision_time_ms >= 0),
    status text not null check (status in ('PENDING', 'OPEN', 'CLOSED', 'INVALID')),
    entry_time_ms bigint,
    entry_price numeric,
    stop_loss numeric not null check (stop_loss > 0),
    tp1 numeric check (tp1 is null or tp1 > 0),
    tp2 numeric not null check (tp2 > 0),
    initial_risk numeric,
    exit_time_ms bigint,
    exit_price numeric,
    exit_reason text,
    gross_result_r numeric,
    net_result_r numeric,
    round_trip_cost_bps numeric not null default 8 check (round_trip_cost_bps >= 0),
    mfe_r numeric,
    mae_r numeric,
    tp1_touched boolean not null default false,
    last_observed_ms bigint,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (entry_time_ms is null or entry_time_ms >= decision_time_ms),
    check (exit_time_ms is null or entry_time_ms is not null),
    check (exit_time_ms is null or exit_time_ms >= entry_time_ms),
    check (entry_price is null or entry_price > 0),
    check (initial_risk is null or initial_risk > 0),
    check (exit_price is null or exit_price > 0)
);

create index if not exists ix_strategy_forward_eval_strategy_time
    on public.strategy_forward_evaluations(strategy_id, signal_created_at_ms desc);
create index if not exists ix_strategy_forward_eval_slice
    on public.strategy_forward_evaluations(strategy_id, symbol, strategy_timeframe, regime, direction, signal_created_at_ms desc);
create index if not exists ix_strategy_paper_status_time
    on public.strategy_paper_trades(status, decision_time_ms);
create index if not exists ix_strategy_paper_strategy_exit
    on public.strategy_paper_trades(strategy_id, exit_time_ms desc)
    where status = 'CLOSED';
create index if not exists ix_strategy_paper_slice
    on public.strategy_paper_trades(strategy_id, symbol, strategy_timeframe, regime, direction, exit_time_ms desc)
    where status = 'CLOSED';

alter table public.strategy_forward_evaluations enable row level security;
alter table public.strategy_paper_trades enable row level security;

revoke all on table public.strategy_forward_evaluations from PUBLIC, anon, authenticated, service_role;
revoke all on table public.strategy_paper_trades from PUBLIC, anon, authenticated, service_role;
grant select, insert, update, delete on table public.strategy_forward_evaluations to service_role;
grant select, insert, update, delete on table public.strategy_paper_trades to service_role;

commit;
