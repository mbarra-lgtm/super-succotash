-- ssa_sync · 002 — campos confirmados contra la API real (probe 23-09-2026)
--
-- El 001 se escribió solo con lo documentado en API_2.md. El probe mostró que la
-- API entrega bastante más, y dos detalles que había que corregir:
--   · el estado de la OF viene en `estado_of`, no en `estado`;
--   · las fechas llegan SIN offset (son hora de planta): el sync les estampa
--     -03:00 antes de escribirlas, o Postgres las leería como UTC.
--
-- Ya aplicado en Supabase el 23-09-2026 (migración ssa_sync_002_campos_confirmados).

alter table public.ssa_ofs
  add column if not exists of_primaria          text,
  add column if not exists es_primaria          boolean,
  add column if not exists nota_venta           text,
  add column if not exists cliente_nombre       text,
  add column if not exists proyecto             text,
  add column if not exists proyecto_etapa       text,
  add column if not exists producto             text,
  add column if not exists cerrada_en_ssa       boolean,
  add column if not exists fecha_compromiso     date,
  add column if not exists cantidad_planificada numeric,
  add column if not exists cantidad_producida   numeric;

alter table public.ssa_actividades
  add column if not exists of_primaria_odoo_id   bigint,
  add column if not exists nota_venta_odoo_id    bigint,
  add column if not exists lead_odoo_id          bigint,
  add column if not exists centro                text,
  add column if not exists cerrada               boolean,
  add column if not exists horas_unitarias       numeric,
  add column if not exists unidades              numeric,
  add column if not exists unidades_reconocidas  numeric,
  add column if not exists factor                numeric,
  add column if not exists valor_congelado       numeric,
  add column if not exists asignaciones          integer,
  add column if not exists asignaciones_abiertas integer;

alter table public.ssa_asignaciones
  add column if not exists actividad           text,
  add column if not exists nota_venta_odoo_id  bigint,
  add column if not exists lead_odoo_id        bigint,
  add column if not exists centro              text,
  add column if not exists emitida_por_odoo_id bigint,
  add column if not exists resultado           text,
  add column if not exists participacion       numeric,
  add column if not exists completitud         numeric,
  add column if not exists es_reasignacion     boolean,
  add column if not exists emitida             timestamptz,
  add column if not exists cerrada             timestamptz;

create index if not exists ssa_asg_emitida_idx on public.ssa_asignaciones (emitida);

-- Carga abierta por centro de trabajo: dónde está el cuello de botella hoy.
create or replace view public.v_ssa_carga_centro as
select a.centro_odoo_id, a.centro,
       count(*) filter (where not coalesce(a.cerrada, false))                     as actividades_abiertas,
       count(distinct a.of_odoo_id) filter (where not coalesce(a.cerrada, false)) as ofs_abiertas,
       round(sum(case when coalesce(a.cerrada, false) then 0
                      else coalesce(a.horas_unitarias, 0) * coalesce(a.unidades, 0) end)::numeric, 1) as horas_abiertas,
       count(*) filter (where a.no_planificada)                                   as actividades_np
from public.ssa_actividades a
where a.centro_odoo_id is not null
group by 1, 2;

-- Ciclo real (emitida → cerrada) por centro. Es la distribución que necesita la
-- simulación: sin esto los tiempos de proceso serían inventados.
create or replace view public.v_ssa_ciclo_centro as
select a.centro_odoo_id, a.centro,
       count(*) as cierres,
       round(avg(extract(epoch from (g.cerrada - g.emitida)) / 3600)::numeric, 2) as horas_ciclo_prom,
       round((percentile_cont(0.5) within group (
              order by extract(epoch from (g.cerrada - g.emitida)) / 3600))::numeric, 2) as horas_ciclo_p50,
       round((percentile_cont(0.9) within group (
              order by extract(epoch from (g.cerrada - g.emitida)) / 3600))::numeric, 2) as horas_ciclo_p90
from public.ssa_asignaciones g
join public.ssa_actividades a on a.actividad_odoo_id = g.actividad_odoo_id
where g.cerrada is not null and g.emitida is not null and g.cerrada > g.emitida
group by 1, 2;
