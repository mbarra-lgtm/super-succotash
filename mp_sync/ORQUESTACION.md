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

`refetch_adjudicaciones.py` reusa `TICKET_ACTIVAS`.

> El `TICKET_BACKFILL` queda de reserva; los backfills usan `TICKET_ACTIVAS`/`TICKET_OC`.

## 2. Workflows (en `.github/workflows/`)

| Archivo | Cadencia (UTC) | Scripts |
|---|---|---|
| `mp-compra-agil.yml` | cada 2 h, min 5 | `sync_compra_agil.py` |
| `mp-licitaciones-oc.yml` | cada 2 h, min 15 | `sync_activas.py` |
| `mp-oc-diario.yml` | 09:45 (≈05:45 CL) | `sync_oc.py` |
| `mp-diario.yml` | 10:00 (≈06:00 CL) | `sync_estados_diario.py` + `sync_crm.py` |
| `mp-oc-backfill-proveedor.yml` | 02:00 | `backfill_oc_por_proveedor.py` |
| `mp-refetch-adjudicaciones.yml` | 03:00 | `refetch_adjudicaciones.py` |
| `mp-backfill.yml` | 04:30 (≈00:30 CL) | `backfill_estados_lic.py` + `backfill_oc_detalle.py` |
| `mp-oc-datos-abiertos.yml` | día 22, 06:00 | `ingesta_oc_datos_abiertos.py` |
| `mp-odoo.yml` | cada 20 min L-V (pg_cron) | `sync_odoo_supabase.py` |
| `mp-odoo-full.yml` | 1x/día L-V (pg_cron) | `sync_odoo_supabase.py` full |

Todos tienen `workflow_dispatch` para correr a mano desde la pestaña Actions.
La frecuencia se ajustó a 1–2 h (antes 30 min) para aliviar Supabase.

### Las dos vías de OC, y por qué importa

`sync_oc.py` (API, diaria) trae las OC del día con alcance dirigido — unas 42 mil
en total, 2014→hoy. `ingesta_oc_datos_abiertos.py` (día 22) trae el **mercado
completo** del mes vencido, casi un millón de filas desde dic-2025. Son escalas
distintas por diseño, y el corte está en 2025Q4.

**Consecuencia para el análisis:** cualquier participación de mercado calculada
a través de ese corte es un artefacto — el denominador se multiplica por ~25. Para
tener share comparable en toda la historia hay que barrer por RUT con
`backfill_oc_por_proveedor.py` (que ahora lee los 59 RUTs de `mp_competidores`),
no recargar años de Datos Abiertos.

También implica que el mes en curso y el anterior no son publicables hasta que
corra la carga del día 22. `sync_oc.py` estampa `data_freshness('mp_oc')` para
que los tableros puedan decidirlo solos.

### El monto adjudicado

La API de Mercado Público **no devuelve `MontoTotal`**: entrega `MontoUnitario` y
la `Cantidad` adjudicada. Todo parser de `Adjudicacion` tiene que hacer
`monto_total = MontoUnitario × Cantidad`, priorizando la `Cantidad` del objeto
`Adjudicacion` sobre la del ítem de la licitación (difieren en ~1,4% de los casos
y usar la del ítem sobreestima).

Falta de ese fallback en `backfill_estados_lic.py` dejó 367.326 filas sin monto
hasta ago-2026. La columna `mp_adjudicaciones.monto_total_fuente` distingue el
monto calculado en el backfill SQL (`calc_cant_item`, aproximado) del que escribe
la ingesta (`api_mu_x_cantidad`, exacto).

`backfill_estados_lic.py` no alcanza a las licitaciones afectadas porque solo
selecciona las que tienen `estado is null`. Para esas está
`refetch_adjudicaciones.py`, que las direcciona explícitamente.

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
