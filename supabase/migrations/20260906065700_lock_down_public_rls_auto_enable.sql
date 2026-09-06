begin;

revoke all on function public.rls_auto_enable() from PUBLIC;
revoke all on function public.rls_auto_enable() from anon, authenticated;
grant execute on function public.rls_auto_enable() to service_role;

commit;
