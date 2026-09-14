-- ============================================================================
-- Datos Abiertos de ChileCompra · Licitaciones
-- ============================================================================
-- Aterrizaje de los archivos mensuales lic-da/{AÑO}-{MES}.zip (una fila por
-- oferta por línea) y consolidación idempotente hacia las tablas canónicas
-- que ya consume el panel (mp_licitaciones, mp_licitacion_comprador,
-- mp_licitacion_items, mp_adjudicaciones).
--
-- Regla de oro: Datos Abiertos NUNCA pisa un dato que la API ya trajo.
-- Sólo inserta lo que falta y rellena nulos. La API sigue siendo la fuente
-- viva; Datos Abiertos es la fuente histórica y la de las ofertas.
--
-- Aplicada al proyecto lxuwltssfnofapyzcwnh el 14-09-2026.
-- ============================================================================

-- ── 1. Cabecera por licitación (TODO el mercado) ─────────────────────────────
create table if not exists public.mp_da_licitaciones (
  codigo_externo                 text primary key,
  codigo_interno                 bigint,
  link                           text,
  nombre                         text,
  descripcion                    text,
  criterios_evaluacion           text,
  tipo_adquisicion               text,
  codigo_estado                  integer,
  estado                         text,
  codigo_organismo               text,
  nombre_organismo               text,
  sector                         text,
  rut_unidad                     text,
  codigo_unidad                  text,
  nombre_unidad                  text,
  direccion_unidad               text,
  comuna_unidad                  text,
  region_unidad                  text,
  codigo_tipo                    integer,
  tipo                           text,
  tipo_convocatoria              text,
  moneda                         text,
  etapas                         integer,
  cantidad_reclamos              integer,
  fecha_creacion                 timestamptz,
  fecha_publicacion              timestamptz,
  fecha_cierre                   timestamptz,
  fecha_acto_apertura_economica  timestamptz,
  fecha_adjudicacion             timestamptz,
  fecha_estimada_adjudicacion    timestamptz,
  monto_estimado                 numeric,
  visibilidad_monto              text,
  fuente_financiamiento          text,
  numero_oferentes               integer,
  es_core                        boolean not null default false,
  mes_archivo                    text not null,
  loaded_at                      timestamptz not null default now()
);
create index if not exists ix_mp_da_lic_pub    on public.mp_da_licitaciones (fecha_publicacion);
create index if not exists ix_mp_da_lic_mes    on public.mp_da_licitaciones (mes_archivo);
create index if not exists ix_mp_da_lic_core   on public.mp_da_licitaciones (es_core) where es_core;
create index if not exists ix_mp_da_lic_org    on public.mp_da_licitaciones (codigo_organismo);

comment on table public.mp_da_licitaciones is
  'Cabecera de TODAS las licitaciones publicadas en el mes (Datos Abiertos ChileCompra, lic-da). Es el denominador del universo. Sólo trae licitaciones con al menos una oferta.';

-- ── 2. Ofertas por línea (core + competidores) ───────────────────────────────
create table if not exists public.mp_da_ofertas (
  line_hash                 text primary key,
  codigo_externo            text not null references public.mp_da_licitaciones(codigo_externo) on delete cascade,
  correlativo               integer,
  codigo_item               bigint,
  codigo_producto_onu       bigint,
  rubro1                    text,
  rubro2                    text,
  rubro3                    text,
  nombre_producto_generico  text,
  nombre_linea              text,
  descripcion_linea         text,
  unidad_medida             text,
  cantidad                  numeric,
  codigo_proveedor          text,
  codigo_sucursal           text,
  rut_proveedor             text,
  nombre_proveedor          text,
  razon_social_proveedor    text,
  nombre_oferta             text,
  estado_oferta             text,          -- Aceptada | Rechazada
  cantidad_ofertada         numeric,
  moneda_oferta             text,
  monto_unitario_oferta     numeric,
  valor_total_ofertado      numeric,
  cantidad_adjudicada       numeric,
  monto_linea_adjudica      numeric,
  fecha_envio_oferta        timestamptz,
  oferta_seleccionada       boolean,
  mes_archivo               text not null,
  loaded_at                 timestamptz not null default now()
);
create index if not exists ix_mp_da_of_lic  on public.mp_da_ofertas (codigo_externo);
create index if not exists ix_mp_da_of_rut  on public.mp_da_ofertas (rut_proveedor);
create index if not exists ix_mp_da_of_sel  on public.mp_da_ofertas (codigo_externo) where oferta_seleccionada;

comment on table public.mp_da_ofertas is
  'Una fila por oferta por línea (Datos Abiertos). Incluye ofertas perdedoras con monto y admisibilidad. Se cargan las licitaciones core y toda oferta de un RUT de mp_competidores.';

-- ── 3. Consolidación hacia las tablas canónicas ──────────────────────────────
create or replace function public.fn_mp_da_consolidar(p_mes text)
returns table (paso text, filas bigint)
language plpgsql
set search_path to public, pg_temp
as $$
declare
  v_now timestamptz := now();
  v_n   bigint;
begin
  -- 3.1 mp_licitaciones: insertar las que la API nunca vio
  -- monto_estimado es columna GENERADA desde raw->>'MontoEstimado' (regexp '[^0-9]'):
  -- se escribe vía raw y como entero en texto, o "166666667.0" se vuelve 1666666670.
  insert into mp_licitaciones (codigo_externo, nombre, descripcion, estado, codigo_estado,
                               tipo, codigo_tipo, moneda, etapas, cantidad_reclamos,
                               fecha_publicacion, fecha_cierre, fecha_adjudicacion,
                               url_detalle, raw, raw_hash, last_sync_at, inserted_at, updated_at)
  select d.codigo_externo, d.nombre, d.descripcion, d.estado, d.codigo_estado,
         d.tipo, d.codigo_tipo, d.moneda, d.etapas, d.cantidad_reclamos,
         d.fecha_publicacion, d.fecha_cierre, d.fecha_adjudicacion,
         d.link,
         jsonb_build_object('MontoEstimado', round(d.monto_estimado)::bigint::text,
                            '_source', 'datos_abiertos', '_mes', p_mes),
         'da:' || p_mes, v_now, v_now, v_now
  from mp_da_licitaciones d
  where d.mes_archivo = p_mes
    and not exists (select 1 from mp_licitaciones l where l.codigo_externo = d.codigo_externo);
  get diagnostics v_n = row_count;
  paso := 'mp_licitaciones insertadas'; filas := v_n; return next;

  -- 3.2 mp_licitaciones: rellenar nulos de las que ya existían (nunca pisar)
  update mp_licitaciones l
     set raw                = case when l.monto_estimado is null and d.monto_estimado is not null
                                   then coalesce(l.raw, '{}'::jsonb)
                                        || jsonb_build_object('MontoEstimado', round(d.monto_estimado)::bigint::text)
                                   else l.raw end,
         fecha_publicacion  = coalesce(l.fecha_publicacion, d.fecha_publicacion),
         fecha_adjudicacion = coalesce(l.fecha_adjudicacion, d.fecha_adjudicacion),
         fecha_cierre       = coalesce(l.fecha_cierre, d.fecha_cierre),
         descripcion        = coalesce(l.descripcion, d.descripcion),
         estado             = coalesce(l.estado, d.estado),
         codigo_estado      = coalesce(l.codigo_estado, d.codigo_estado),
         tipo               = coalesce(l.tipo, d.tipo),
         moneda             = coalesce(l.moneda, d.moneda),
         updated_at         = v_now
    from mp_da_licitaciones d
   where d.codigo_externo = l.codigo_externo and d.mes_archivo = p_mes
     and (l.monto_estimado is null or l.fecha_publicacion is null or l.fecha_adjudicacion is null
          or l.fecha_cierre is null or l.descripcion is null or l.estado is null
          or l.codigo_estado is null or l.tipo is null or l.moneda is null);
  get diagnostics v_n = row_count;
  paso := 'mp_licitaciones completadas'; filas := v_n; return next;

  -- 3.3 comprador
  insert into mp_licitacion_comprador (codigo_externo, codigo_organismo, nombre_organismo, rut_unidad,
                                       codigo_unidad, nombre_unidad, direccion_unidad, comuna_unidad, region_unidad)
  select d.codigo_externo, d.codigo_organismo, d.nombre_organismo, d.rut_unidad,
         d.codigo_unidad, d.nombre_unidad, d.direccion_unidad, d.comuna_unidad, d.region_unidad
  from mp_da_licitaciones d
  where d.mes_archivo = p_mes
    and not exists (select 1 from mp_licitacion_comprador c where c.codigo_externo = d.codigo_externo);
  get diagnostics v_n = row_count;
  paso := 'mp_licitacion_comprador insertados'; filas := v_n; return next;

  -- 3.4 items (sólo de las licitaciones con ofertas cargadas)
  insert into mp_licitacion_items (codigo_externo, correlativo, codigo_producto, categoria,
                                   nombre_producto, descripcion, unidad_medida, cantidad)
  select distinct on (o.codigo_externo, o.correlativo)
         o.codigo_externo, o.correlativo, o.codigo_producto_onu,
         concat_ws(' / ', initcap(o.rubro1), initcap(o.rubro2), initcap(o.rubro3)),
         o.nombre_producto_generico, o.descripcion_linea, o.unidad_medida, o.cantidad
  from mp_da_ofertas o
  where o.mes_archivo = p_mes and o.correlativo is not null
    and not exists (select 1 from mp_licitacion_items i
                     where i.codigo_externo = o.codigo_externo and i.correlativo = o.correlativo)
  order by o.codigo_externo, o.correlativo, o.line_hash;
  get diagnostics v_n = row_count;
  paso := 'mp_licitacion_items insertados'; filas := v_n; return next;

  -- 3.5 adjudicaciones: insertar las líneas seleccionadas que no están
  insert into mp_adjudicaciones (licitacion_id, item_no, proveedor_rut, proveedor_nombre,
                                 cantidad, monto_unitario, monto_total, moneda, line_hash, monto_total_fuente)
  select o.codigo_externo, o.correlativo, o.rut_proveedor, o.nombre_proveedor,
         o.cantidad_adjudicada,
         case when coalesce(o.cantidad_adjudicada,0) > 0 then o.monto_linea_adjudica / o.cantidad_adjudicada end,
         o.monto_linea_adjudica, coalesce(o.moneda_oferta, 'CLP'), o.line_hash, 'datos_abiertos'
  from mp_da_ofertas o
  where o.mes_archivo = p_mes and o.oferta_seleccionada
    and o.correlativo is not null and o.rut_proveedor is not null
    and coalesce(o.monto_linea_adjudica, 0) > 0
  on conflict (licitacion_id, item_no, proveedor_rut) do nothing;
  get diagnostics v_n = row_count;
  paso := 'mp_adjudicaciones insertadas'; filas := v_n; return next;

  -- 3.6 adjudicaciones: rellenar el monto que la API dejó en null
  update mp_adjudicaciones a
     set monto_total        = o.monto_linea_adjudica,
         monto_unitario     = coalesce(a.monto_unitario,
                                case when coalesce(o.cantidad_adjudicada,0) > 0
                                     then o.monto_linea_adjudica / o.cantidad_adjudicada end),
         cantidad           = coalesce(a.cantidad, o.cantidad_adjudicada),
         moneda             = coalesce(a.moneda, o.moneda_oferta, 'CLP'),
         monto_total_fuente = 'datos_abiertos'
    from mp_da_ofertas o
   where o.mes_archivo = p_mes and o.oferta_seleccionada
     and o.codigo_externo = a.licitacion_id and o.correlativo = a.item_no
     and o.rut_proveedor = a.proveedor_rut
     and a.monto_total is null and coalesce(o.monto_linea_adjudica, 0) > 0;
  get diagnostics v_n = row_count;
  paso := 'mp_adjudicaciones montos rellenados'; filas := v_n; return next;

  -- 3.7 frescura para los tableros
  perform stamp_freshness('mp_lic_da', null, null, 'ingesta_lic_datos_abiertos:' || p_mes);
  paso := 'freshness'; filas := 1; return next;
end;
$$;

comment on function public.fn_mp_da_consolidar(text) is
  'Lleva un mes cargado en mp_da_* a las tablas canónicas sin pisar lo que trajo la API. Idempotente.';

-- ── 4. Vista de competencia por oferta ───────────────────────────────────────
-- Lo que Competencia.tsx quería: quién ofertó qué, a cuánto, y qué pasó.
create or replace view public.v_mp_da_competencia as
select
  o.codigo_externo,
  l.nombre                                   as licitacion,
  l.nombre_organismo,
  l.region_unidad,
  l.fecha_publicacion,
  l.fecha_adjudicacion,
  l.estado                                   as estado_licitacion,
  l.numero_oferentes,
  l.monto_estimado,
  l.es_core,
  o.correlativo,
  o.nombre_linea,
  o.rubro3,
  o.cantidad,
  o.rut_proveedor,
  o.nombre_proveedor,
  c.razon_social                             as competidor_canonico,
  coalesce(c.es_bti, false)                  as es_bti,
  o.estado_oferta,
  o.oferta_seleccionada,
  o.monto_unitario_oferta,
  o.valor_total_ofertado,
  o.monto_linea_adjudica,
  -- ranking de precio entre las ofertas admisibles de la misma línea (1 = más barata)
  case when o.estado_oferta = 'Aceptada' then
    rank() over (partition by o.codigo_externo, o.correlativo, (o.estado_oferta = 'Aceptada')
                 order by o.valor_total_ofertado asc nulls last)
  end                                        as ranking_precio,
  -- brecha contra la oferta ganadora de la línea
  round(100.0 * (o.valor_total_ofertado
        / nullif(min(o.valor_total_ofertado) filter (where o.oferta_seleccionada)
                 over (partition by o.codigo_externo, o.correlativo), 0) - 1), 1) as brecha_vs_ganador_pct
from public.mp_da_ofertas o
join public.mp_da_licitaciones l on l.codigo_externo = o.codigo_externo
left join public.mp_competidores c
       on replace(upper(c.rut), '.', '') = replace(upper(o.rut_proveedor), '.', '');

comment on view public.v_mp_da_competencia is
  'Ofertas por línea con ranking de precio y brecha contra el ganador. Fuente: Datos Abiertos.';

-- ── 5. RLS: mismo criterio que el resto de mp_* (lectura para el equipo) ─────
alter table public.mp_da_licitaciones enable row level security;
alter table public.mp_da_ofertas       enable row level security;
drop policy if exists mp_da_lic_read on public.mp_da_licitaciones;
drop policy if exists mp_da_of_read  on public.mp_da_ofertas;
create policy mp_da_lic_read on public.mp_da_licitaciones for select to authenticated using (true);
create policy mp_da_of_read  on public.mp_da_ofertas       for select to authenticated using (true);
-- La escritura la hace el service_role desde GitHub Actions (bypassa RLS).
