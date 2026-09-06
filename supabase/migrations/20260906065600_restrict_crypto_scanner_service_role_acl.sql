begin;

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
        execute format('revoke all privileges on table public.%I from service_role', table_name);
        execute format(
            'grant select, insert, update, delete on table public.%I to service_role',
            table_name
        );
    end loop;
end $$;

commit;
