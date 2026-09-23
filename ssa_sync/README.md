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
3. **Probe:** Actions → *SSA → Supabase* → Run workflow → `modo = probe`. No
   escribe nada; muestra el sobre de la respuesta y las llaves de cada endpoint
   (sin nombres de colaboradores, el repo es público). Confirmar tres cosas:
   - cómo se llama el campo con el nombre de la OF (el sync prueba `of`,
     `nombre`, `of_nombre`, `name`, `codigo`);
   - si las asignaciones traen un id propio (si no, se usa un hash estable);
   - si las asignaciones traen `of_odoo_id` (si no, se resuelve por la actividad).
4. **Full a mano:** `modo = full`. Deja el watermark en `data_freshness('ssa_ofs')`.
5. **pg_cron:** correr `sql/002_pg_cron.sql` (incremental cada 20 min + full diario).

## Ojo

- GitHub Actions corre desde internet: `ssa.bertonati.cl` tiene que ser
  alcanzable desde afuera. Si solo responde en la red de la planta, el probe
  fallará por timeout y hay que correr el sync en un equipo interno.
- El incremental supone que un cambio en una actividad o asignación marca su OF
  como modificada. El full diario corrige cualquier cosa que se escape.
- El esquema `ssa` que ya existe en Supabase es el prototipo antiguo (con RUT y
  pin): no se toca ni se mezcla.
