-- ssa_sync · 001 — tablas destino de la API de consulta de SSA (v1)
--
-- Van en public con prefijo ssa_ (NO en el esquema `ssa`): ese esquema es el
-- prototipo antiguo de SSA, trae RUT y pin_hash de colaboradores y no debe
-- mezclarse con esto. La API ya viene sin RUT.
--
-- Columnas tipadas solo para los campos DOCUMENTADOS en API_2.md; todo lo demás
-- queda en `raw` hasta confirmarlo con el probe. `synced_at` lo estampa cada
-- corrida y es lo que usa el sync para borrar lo que ya no vino.

create table if not exists public.ssa_ofs (
  of_odoo_id              bigint primary key,          -- mrp.production
  of_nombre               text,
  of_primaria_odoo_id     bigint,
  nota_venta_odoo_id      bigint,                      -- sale.order
  lead_odoo_id            bigint,                      -- crm.lead
  cliente_odoo_id         bigint,                      -- res.partner
  estado                  text,
  actividades             integer,
  actividades_abiertas    integer,
  horas_planificadas      numeric,
  uet                     numeric,
  asignaciones            integer,
  asignaciones_abiertas   integer,
  unidades_reconocidas    numeric,
  unidades_comprometidas  numeric,
  unidades_totales        numeric,
  avance_pct              numeric,                     -- oficial SSA: reconocidas / totales
  raw                     jsonb not null,
  synced_at               timestamptz not null
);
create index if not exists ssa_ofs_lead_idx   on public.ssa_ofs (lead_odoo_id);
create index if not exists ssa_ofs_nombre_idx on public.ssa_ofs (of_nombre);

create table if not exists public.ssa_actividades (
  actividad_odoo_id  bigint primary key,               -- mrp.workorder
  of_odoo_id         bigint,
  centro_odoo_id     bigint,                           -- mrp.workcenter
  nombre             text,
  estado             text,
  no_planificada     boolean,
  np_regularizado    boolean,
  raw                jsonb not null,
  synced_at          timestamptz not null
);
create index if not exists ssa_act_of_idx     on public.ssa_actividades (of_odoo_id);
create index if not exists ssa_act_centro_idx on public.ssa_actividades (centro_odoo_id);

create table if not exists public.ssa_asignaciones (
  asignacion_key       text primary key,               -- id de SSA, o hash estable si no expone uno
  actividad_odoo_id    bigint,
  of_odoo_id           bigint,
  colaborador_odoo_id  bigint,                         -- hr.employee
  estado               text,
  raw                  jsonb not null,
  synced_at            timestamptz not null
);
create index if not exists ssa_asg_of_idx  on public.ssa_asignaciones (of_odoo_id);
create index if not exists ssa_asg_act_idx on public.ssa_asignaciones (actividad_odoo_id);
create index if not exists ssa_asg_col_idx on public.ssa_asignaciones (colaborador_odoo_id);

-- Solo el service role escribe (el sync). La lectura, para usuarios autenticados
-- de AccessPoint, igual que el resto del espejo.
alter table public.ssa_ofs          enable row level security;
alter table public.ssa_actividades  enable row level security;
alter table public.ssa_asignaciones enable row level security;
drop policy if exists ssa_ofs_read on public.ssa_ofs;
drop policy if exists ssa_act_read on public.ssa_actividades;
drop policy if exists ssa_asg_read on public.ssa_asignaciones;
create policy ssa_ofs_read on public.ssa_ofs          for select to authenticated using (true);
create policy ssa_act_read on public.ssa_actividades  for select to authenticated using (true);
create policy ssa_asg_read on public.ssa_asignaciones for select to authenticated using (true);

-- Avance de material (espejo Odoo) contra avance de trabajo (SSA), por OF en planta.
-- Es la vista que consume el plano de planta.
create or replace view public.v_planta_of_avance_ssa as
select
  o.identificacion,
  o.of_odoo_id,
  o.of_name,
  o.of_state,
  o.progress_pct                                   as avance_material_pct,
  s.avance_pct                                     as avance_trabajo_pct,
  s.horas_planificadas,
  round(s.horas_planificadas * (1 - coalesce(s.avance_pct, 0) / 100.0), 1) as horas_restantes_est,
  s.actividades,
  s.actividades_abiertas,
  s.asignaciones_abiertas,
  s.unidades_comprometidas,
  (s.asignaciones_abiertas = 0 and coalesce(s.actividades_abiertas, 0) > 0) as sin_trabajo_emitido,
  s.synced_at                                      as ssa_synced_at,
  (s.of_odoo_id is null)                           as sin_datos_ssa
from public.v_planta_proyecto_ofs o
left join public.ssa_ofs s on s.of_odoo_id = o.of_odoo_id
where o.of_state <> 'cancel';
