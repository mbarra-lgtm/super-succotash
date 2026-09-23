-- ssa_sync · 002 — disparo desde pg_cron (mismo patrón que dispatch-mp-odoo)
--
-- Correr SOLO después de validar a mano el probe y un full desde Actions.
-- Horarios en UTC (pg_cron). Chile hoy es UTC-3:
--   incremental  4,24,44 10-23 * * 1-5  → cada 20 min, 07:04–20:44 CL, desfasado
--                                          4 min del sync de Odoo (8,28,48)
--   full         30 9 * * 1-5           → 06:30 CL, antes de que parta el turno

select cron.schedule('dispatch-ssa-sync', '4,24,44 10-23 * * 1-5', $$
  select net.http_post(
    url := 'https://api.github.com/repos/mbarra-lgtm/super-succotash/actions/workflows/ssa-sync.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name='gh_pat_dispatch'),
      'Accept', 'application/vnd.github+json', 'User-Agent', 'supabase-cron',
      'X-GitHub-Api-Version', '2022-11-28', 'Content-Type', 'application/json'),
    body := jsonb_build_object('ref','main','inputs', jsonb_build_object('modo','incremental')));
$$);

select cron.schedule('dispatch-ssa-sync-full', '30 9 * * 1-5', $$
  select net.http_post(
    url := 'https://api.github.com/repos/mbarra-lgtm/super-succotash/actions/workflows/ssa-sync.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name='gh_pat_dispatch'),
      'Accept', 'application/vnd.github+json', 'User-Agent', 'supabase-cron',
      'X-GitHub-Api-Version', '2022-11-28', 'Content-Type', 'application/json'),
    body := jsonb_build_object('ref','main','inputs', jsonb_build_object('modo','full')));
$$);

-- Para apagar:  select cron.unschedule('dispatch-ssa-sync'); select cron.unschedule('dispatch-ssa-sync-full');
