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
| `mp-licitaciones-oc.yml` | cada 1 h, min 15 | `sync_activas.py` |
| `mp-oc-diario.yml` | 09:45 (≈05:45 CL) | `sync_oc.py` |
| `mp-diario.yml` | 10:00 (≈06:00 CL) | `sync_estados_diario.py` + `sync_crm.py` |
| `mp-oc-backfill-proveedor.yml` | 02:00 | `backfill_oc_por_proveedor.py` |
| `mp-refetch-adjudicaciones.yml` | 03:00 | `refetch_adjudicaciones.py` |
| `mp-backfill.yml` | 04:30 (≈00:30 CL) | `backfill_estados_lic.py` + `backfill_oc_detalle.py` |
| `mp-oc-datos-abiertos.yml` | día 22, 06:00 | `ingesta_oc_datos_abiertos.py` |
| `mp-lic-datos-abiertos.yml` | día 22, 06:30 | `ingesta_lic_datos_abiertos.py` (+ `fn_mp_da_consolidar`) |
| `mp-odoo.yml` | cada 20 min L-V (pg_cron) | `sync_odoo_supabase.py` |
| `mp-odoo-full.yml` | 1x/día L-V (pg_cron) | `sync_odoo_supabase.py` full |

Todos tienen `workflow_dispatch` para correr a mano desde la pestaña Actions.
La frecuencia se ajustó a 1–2 h (antes 30 min) para aliviar Supabase.

### El cursor de activas (ago-2026)

Bajar `sync_activas.py` de 30 min a 2 h destapó un bug latente: el cursor se
reseteaba a 0 cada medianoche UTC. A 30 min (48 corridas × 200 = 9.600
posiciones/día) alcanzaba a dar dos vueltas a las ~4.000 activas y el reset no
se notaba; a 2 h (2.400/día) el barrido volvía todos los días al mismo tramo
inicial de la lista ordenada por `codigo_externo` y **la cola nunca se
visitaba**. Las activas sobre la posición ~2.400 llegaron a 5+ días sin
sincronizar, y las licitaciones nuevas que caían en ese rango no se insertaban
nunca: la captura diaria pasó de ~420 a 28 entre el 21 y el 27 de agosto.

Correcciones:

- El cursor es round-robin real, persiste entre días y da la vuelta con módulo.
- Cada corrida procesa **primero** las del listado que no están en BD, para que
  una licitación nueva entre el mismo día sin importar su posición alfabética.
- El prefetch de hashes cubre el listado completo, troceado en lotes de 400
  (el filtro `in.(...)` viaja en la URL).
- Un 429 persistente corta la corrida en vez de contarse como error por
  licitación: antes el cursor avanzaba igual y esas licitaciones quedaban
  saltadas hasta la vuelta siguiente, que con el reset diario no llegaba.
- Los backfills largos usan `TICKET_BACKFILL` si está cargado, para no competir
  por la cuota del ticket de activas en la misma ventana.

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


### Licitaciones desde Datos Abiertos: la fuente histórica y la de las ofertas (sep-2026)

Hasta septiembre de 2026 el universo de `mp_licitaciones` partía en **julio 2025**:
`backfill_noche.py` (que recorre la API día por día hacia atrás) nunca se enganchó
a un workflow y guardaba su checkpoint en un archivo local, así que murió con el
Programador de Windows. Y los montos de `mp_adjudicaciones` tenían cobertura de
**1,0 % en 2025** contra 58,5 % en 2026, sesgada además hacia el grupo (nuestras
filas sí tenían monto; las de la competencia no). Cualquier share calculado sobre
2025 era un artefacto.

Los dos huecos se cierran con el mismo archivo mensual de Datos Abiertos, el
hermano del que ya usamos para OC:

```
https://transparenciachc.blob.core.windows.net/lic-da/{AÑO}-{MES}.zip   (mes sin cero)
```

- Existe desde **2023-1** hasta el mes en curso; 14–26 MB zip, ~340 MB CSV, CP1252, `;`.
- Indexado por **mes de publicación**. Marzo 2025 trae 8.484 licitaciones (el
  espejo tenía 665 de ese mes).
- **Una fila por oferta por línea**: todas las ofertas —perdedoras incluidas— con
  `Valor Total Ofertado`, `Estado Oferta` (Aceptada/Rechazada = admisibilidad),
  `Oferta seleccionada`, `MontoLineaAdjudica`, `NumeroOferentes`, región, rubros ONU.
  Es lo que la API transaccional no entrega nunca.
- Se **regenera**: el de 2025-3 tiene fecha 16-04-2026. Por eso el workflow recarga
  siempre el mes vencido y los dos anteriores.
- Límite conocido: sólo trae licitaciones con al menos una oferta. Las desiertas sin
  oferentes no aparecen (~2 % del universo).

`ingesta_lic_datos_abiertos.py` aterriza en dos tablas nuevas y luego consolida:

| Tabla | Qué guarda | Volumen |
|---|---|---|
| `mp_da_licitaciones` | cabecera de TODAS las licitaciones del mes (denominador del universo) | ~8.500/mes |
| `mp_da_ofertas` | ofertas por línea de las licitaciones **core** y de cualquier oferta de un RUT de `mp_competidores` | ~300–500/mes |

`fn_mp_da_consolidar(mes)` lleva eso a las tablas canónicas **sin pisar nada que
haya traído la API**: inserta las licitaciones/compradores/ítems que faltan,
inserta las adjudicaciones seleccionadas que no están y rellena `monto_total`
donde la API lo dejó en null (`monto_total_fuente = 'datos_abiertos'`). Ojo:
`mp_licitaciones.monto_estimado` es una columna generada desde `raw->>'MontoEstimado'`,
así que se escribe vía `raw` y como entero en texto.

`v_mp_da_competencia` expone las ofertas con ranking de precio por línea y brecha
contra el ganador — la base del termómetro real de Competencia.

**Consecuencias para el resto del pipeline:**

- `backfill_noche.py` queda **obsoleto** para licitaciones (sigue sirviendo su parte
  de Compra Ágil y OC si alguien lo revive). No engancharlo.
- `refetch_adjudicaciones.py` deja de ser necesario para el histórico; sigue útil
  para el mes en curso, antes de que salga el archivo. Su scope `bti` ahora lee
  `crm_projects.identificacion` además de `mp_tender_code` (740 → ~3.700 códigos).
- Al comparar años: **jul-2025 en adelante** el universo viene de la API y de Datos
  Abiertos a la vez (se deduplican por `codigo_externo`); **antes de jul-2025** viene
  sólo de Datos Abiertos. La cobertura es homogénea desde 2023 una vez hecha la
  carga inicial.
- `mp_licitaciones_completo` y `v_mp_mercado_completo` están vacías y nunca se
  llenaron: borrarlas o apuntar la vista a `mp_da_licitaciones`.

**Carga inicial** (una vez, desde Actions → Run workflow):
`meses = 2024-1,2024-2,…,2025-6`. Cada mes son 1–3 minutos; en dos tandas de
nueve meses no se acerca al límite de 6 h.

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
