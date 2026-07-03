# Orquestación mp_sync — GitHub Actions + pg_cron

Arquitectura híbrida: la ingesta pesada (Python) corre en **GitHub Actions** con
cron; los refrescos de vistas materializadas y jobs cortos viven en **pg_cron**
dentro de Supabase. Los cursores se persisten en la tabla `mp_sync_cursor`
(no en archivos locales), porque los runners de GH Actions son efímeros.

## 1. Secretos a cargar en GitHub

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Valor (está en tu `.env`) |
|---|---|
| `SUPABASE_URL` | https://lxuwltssfnofapyzcwnh.supabase.co |
| `SUPABASE_SERVICE_KEY` | el service_role key |
| `TICKET_ACTIVAS` | ticket licitaciones activas |
| `TICKET_CA` | ticket compra ágil |
| `TICKET_OC` | ticket órdenes de compra |
| `TICKET_CRM` | ticket CRM |

> El `TICKET_BACKFILL` queda de reserva; los backfills usan `TICKET_ACTIVAS`/`TICKET_OC`.

## 2. Workflows (en `.github/workflows/`)

| Archivo | Cadencia (UTC) | Scripts |
|---|---|---|
| `mp-compra-agil.yml` | cada 1 h | `sync_compra_agil.py` |
| `mp-licitaciones-oc.yml` | cada 2 h | `sync_activas.py` + `sync_oc.py` |
| `mp-diario.yml` | 10:00 (≈06:00 CL) | `sync_estados_diario.py` + `sync_crm.py` |
| `mp-backfill.yml` | 04:30 (≈00:30 CL) | `backfill_estados_lic.py` + `backfill_oc_detalle.py` |

Todos tienen `workflow_dispatch` para correr a mano desde la pestaña Actions.
La frecuencia se ajustó a 1–2 h (antes 30 min) para aliviar Supabase.

## 3. Pasos para activar

1. Commitear `mp_sync/`, `.github/` y `.gitignore` (hoy el pipeline NO está versionado).
   - Sacar del índice el `.pyc` viejo: `git rm --cached __pycache__/act_optimi.cpython-313.pyc`
2. Cargar los secretos del paso 1.
3. Push a `main` (los cron solo corren en la rama por defecto).
4. Probar a mano cada workflow desde Actions → Run workflow.
5. **Apagar las tareas del Programador de Windows** para no duplicar ingesta.

## 4. pg_cron (ya activo en Supabase)

- `v_mp_panel_activo_ui_mat` se refresca cada hora (`REFRESH ... CONCURRENTLY`).
- Si quieres, se puede agregar un job para podar `cron.job_run_details`.

## 5. Notas

- `CURSOR_BACKEND=supabase` (default). Para correr local sin tocar el cursor de la
  nube: `CURSOR_BACKEND=file`.
- OC: `SLEEP_BETWEEN_OC=3.0` (el endpoint de detalle tira 429 a 2 s).
- Seguridad: rotar el `SUPABASE_SERVICE_KEY` si alguna vez se subió en claro;
  `env.example` ya quedó con placeholders y `.env` está en `.gitignore`.
