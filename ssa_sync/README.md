# ssa_sync — avance de trabajo desde SSA

Trae la API de consulta de SSA (v1, solo lectura) a Supabase para cruzar el
avance de **trabajo** (actividades y asignaciones) con el avance de **material**
del espejo Odoo. Lo consume el plano de planta vía `v_planta_of_avance_ssa`.

| Tabla | Llave | Qué es |
|---|---|---|
| `ssa_ofs` | `of_odoo_id` | una fila por OF con los totales oficiales de SSA (`avance_pct`, horas, abiertas) |
| `ssa_actividades` | `actividad_odoo_id` | una fila por actividad (`mrp.workorder`), con centro y marca de no planificada |
| `ssa_asignaciones` | `asignacion_key` | quién ejecuta qué, con estado; `raw` trae participación y fechas |

## Activación

1. **Secret:** `SSA_API_KEY` en Settings → Secrets and variables → Actions. Los
   de Supabase ya están.
2. **Tablas:** correr `sql/001_ssa_tablas.sql` en Supabase.
3. **Campos:** correr `sql/002_campos_confirmados.sql` (ya aplicado el 23-09-2026).
4. **Full a mano:** Actions → *SSA → Supabase* → Run workflow → `modo = full`.
   Deja el watermark en `data_freshness('ssa_ofs')`. (`modo = probe` no escribe
   nada y muestra la forma de la respuesta; útil si la API cambia.)
5. **pg_cron:** correr `sql/003_pg_cron.sql` (incremental cada 20 min + full diario).

## Lo que mostró el probe (23-09-2026)

- Sobre: `{total, limite, offset, items}`. 3.361 OFs, 6.001 actividades,
  4.094 asignaciones — el full completo son ~30 llamadas, es barato.
- **El estado de la OF viene en `estado_of`**, no en `estado`.
- **Las fechas llegan sin offset** (`2026-09-23T07:52:00.437636`). Son hora de
  planta: el sync les estampa `-03:00` antes de escribir, o quedarían corridas.
- **Hay ids negativos** y son legítimos: `of_odoo_id: -3` es la OF virtual
  `CD/MANT/PLANTA` (mantención de planta) y las actividades no planificadas
  traen `actividad_odoo_id` negativo. No se filtran; simplemente no cruzan con
  el espejo de Odoo, que es lo correcto.
- El join con el espejo está verificado: `CD/OF/15919` es `of_odoo_id` 15930 en
  ambos lados.
- Las asignaciones traen `emitida` y `cerrada`: de ahí sale el ciclo real por
  centro (`v_ssa_ciclo_centro`), que es lo que faltaba para simular.

## Ojo

- El `raw` de las asignaciones incluye el nombre del colaborador. El probe los
  omite del log porque el repo es público; la tabla sí los guarda.
- El incremental supone que un cambio en una actividad o asignación marca su OF
  como modificada. El full diario corrige cualquier cosa que se escape.
- El esquema `ssa` que ya existe en Supabase es el prototipo antiguo (con RUT y
  pin): no se toca ni se mezcla.
