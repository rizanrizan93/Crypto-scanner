begin;

create index if not exists ix_signals_run_id
    on public.signals(run_id);
create index if not exists ix_orders_signal_id
    on public.orders(signal_id);
create index if not exists ix_positions_signal_id
    on public.positions(signal_id);
create index if not exists ix_closed_trades_signal_id
    on public.closed_trades(signal_id);

commit;
