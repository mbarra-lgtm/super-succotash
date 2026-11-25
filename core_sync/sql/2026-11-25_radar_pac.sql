-- ═══════════════════════════════════════════════════════════════════════
-- Radar: Plan Anual de Compras (PAC) de Mercado Público (25-11-2026)
--
-- Cuarta vía de ingesta (ingesta_pac.py). El PAC es la señal más temprana
-- dentro de Mercado Público, pero indicativa: de las líneas de ambulancias
-- 2026 con el mes ya vencido, cerca de un tercio terminó en licitación.
--
--   core_pac_lineas         foto diaria del PAC (todas las líneas cosechadas,
--                           core y no core), con estado y cruce a licitación
--   fn_core_pac_procesar()  organismo, retiradas, reprogramadas, licitación,
--                           estado. La llama ingesta_pac.py después del upsert
--   v_core_pac_lineas       detalle para el script y el panel
--   v_core_pac_cumplimiento cuánto del PAC termina en licitación, por tipo
--                           de organismo y categoría
--
-- PENDIENTE de aplicar en Supabase (AccessRose). Idempotente salvo los
-- ALTER de constraints, que se reemplazan.
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. Tipo de fuente ──────────────────────────────────────────────────────
alter table public.core_sources drop constraint core_sources_fuente_tipo_check;
alter table public.core_sources add constraint core_sources_fuente_tipo_check check (fuente_tipo = any (array[
  'acuerdo_core','acta_core','tabla_sesion_core','acta_concejo','rss_prensa','bip','youtube',
  'portal_transparencia','aviso_legal','plan_compras','otro']));

-- ── 2. Líneas del PAC ──────────────────────────────────────────────────────
create table if not exists public.core_pac_lineas (
  id                  bigserial primary key,
  -- md5(ORGANISMO|NOMBRE|total|aaaa-mm-dd) calculado en ingesta_pac.py.
  -- El PAC no publica un ID de línea.
  linea_key           text not null unique,
  anio                smallint not null,
  organizacion        text not null,                 -- tal cual viene del PAC
  organizacion_norm   text generated always as (public.fn_norm_txt(organizacion)) stored,
  nombre              text not null,
  cantidad            numeric,
  monto_unitario      numeric,
  cantidad_oc         integer,
  total               numeric,
  fecha_inicio        date not null,                 -- mes planificado (día 1)
  linea_producto      text,                          -- "[29-03] Adquisición de Vehículos"
  tipo_compra         text,                          -- "Procedimiento concursal", "Convenio Marco"...
  consultas           text[] not null default '{}',  -- qué búsquedas la trajeron

  -- Clasificación (reglas en ingesta_pac.py, versión en reglas_version)
  es_core             boolean not null default false,
  categoria           text,
  encaje              text check (encaje in ('alto','medio')),
  patron_mp           text,                          -- regex para reconocer la licitación
  reglas_version      text,

  -- Organismo (fn_core_pac_procesar)
  organismo_id        bigint references public.core_organismos(id) on delete set null,
  mp_codigo_organismo text,
  region_mp           text,
  comuna_mp           text,

  -- Ciclo de vida
  first_seen_at       timestamptz not null default now(),
  last_seen_at        timestamptz not null default now(),
  removed_at          timestamptz,                   -- dejó de aparecer en el PAC
  reprograma_de_id    bigint references public.core_pac_lineas(id) on delete set null,
  estado              text not null default 'planificada' check (estado in
                        ('planificada','vencida_sin_licitar','licitada','retirada','reprogramada')),

  -- Licitación encontrada en mp_licitaciones
  codigo_mp           text,
  licitacion_nombre   text,
  licitacion_fecha    timestamptz,
  licitacion_estado   text,

  -- Señal del radar. senal_estado/senal_fecha = lo que la señal refleja hoy;
  -- si difieren de estado/fecha_inicio, ingesta_pac.py la actualiza.
  senal_id            bigint references public.core_senales(id) on delete set null,
  senal_estado        text,
  senal_fecha         date,

  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index if not exists idx_core_pac_lineas_anio   on public.core_pac_lineas (anio, es_core, estado);
create index if not exists idx_core_pac_lineas_org    on public.core_pac_lineas (organizacion_norm);
create index if not exists idx_core_pac_lineas_senal  on public.core_pac_lineas (senal_id);

alter table public.core_pac_lineas enable row level security;
drop policy if exists authenticated_read_core_pac_lineas on public.core_pac_lineas;
create policy authenticated_read_core_pac_lineas on public.core_pac_lineas
  for select to authenticated using (true);

-- ── 3. Procesamiento ───────────────────────────────────────────────────────
-- Todo lo que necesita mp_licitaciones (350 mil filas) se hace aquí y no por
-- REST. Tiempo medido de las consultas pesadas: ~2 s el catálogo de
-- compradores y <0,5 s el cruce de licitaciones (va por índice de organismo).
create or replace function public.fn_core_pac_procesar(
  p_anio              integer,
  p_corrida           timestamptz,
  p_cosecha_completa  boolean default true,
  p_dias_gracia       integer default 60)
returns jsonb
language plpgsql
set search_path = public, pg_temp
as $function$
declare
  v_hoy          date := (now() at time zone 'America/Santiago')::date;
  v_retiradas    int := 0;
  v_reprog       int := 0;
  v_org_nombre   int := 0;
  v_org_codigo   int := 0;
  v_org_comuna   int := 0;
  v_org_creados  int := 0;
  v_licitadas    int := 0;
  v_estados      jsonb;
begin
  -- a) Retiradas: sólo si todas las consultas respondieron. Una consulta caída
  --    no puede hacer pasar por retirada a las líneas que sólo ella trae.
  if p_cosecha_completa then
    update core_pac_lineas
       set removed_at = now(), updated_at = now()
     where anio = p_anio and removed_at is null
       and last_seen_at < p_corrida - interval '1 minute';
    get diagnostics v_retiradas = row_count;
  end if;

  -- b) Reprogramadas: mismo organismo y nombre, otra fecha o monto. La línea
  --    vieja desapareció, la nueva apareció en esta corrida. La señal pasa a
  --    la nueva (no se duplica).
  with par as (
    select distinct on (n.id) n.id as nueva, v.id as vieja, v.senal_id
      from core_pac_lineas n
      join core_pac_lineas v
        on v.anio = n.anio and v.id <> n.id
       and v.organizacion_norm = n.organizacion_norm
       and fn_norm_txt(v.nombre) = fn_norm_txt(n.nombre)
     where n.anio = p_anio
       and n.removed_at is null and n.reprograma_de_id is null
       and n.first_seen_at >= p_corrida - interval '1 minute'
       and v.removed_at is not null and v.estado <> 'reprogramada'
     order by n.id, v.removed_at desc
  ), nuevas as (
    update core_pac_lineas n
       set reprograma_de_id = par.vieja,
           senal_id = coalesce(n.senal_id, par.senal_id),
           updated_at = now()
      from par where n.id = par.nueva
    returning n.id
  )
  update core_pac_lineas v
     set estado = 'reprogramada', senal_id = null,
         senal_estado = 'reprogramada', senal_fecha = v.fecha_inicio, updated_at = now()
    from par where v.id = par.vieja;
  get diagnostics v_reprog = row_count;

  -- c) Organismo. Cuatro intentos, del más seguro al más laxo.
  --    1. Nombre igual al de core_organismos (los nombres del PAC son los de MP).
  update core_pac_lineas p
     set organismo_id = o.id, updated_at = now()
    from core_organismos o
   where p.anio = p_anio and p.organismo_id is null
     and fn_norm_txt(o.nombre) = p.organizacion_norm;
  get diagnostics v_org_nombre = row_count;

  --    2. Catálogo de compradores de licitaciones: el nombre del PAC puede ser
  --       el del organismo o el de la unidad ("I. Municipalidad de X - Depto.
  --       Salud"). Da el código MP y la región aunque no esté en core_organismos.
  create temp table _pac_comprador on commit drop as
  with pend as (
    select distinct organizacion_norm as nom
      from core_pac_lineas where anio = p_anio and organismo_id is null
  ), g as (
    select nombre_organismo as nombre, codigo_organismo, 1 as prio, count(*) as n,
           mode() within group (order by region_unidad) as region,
           mode() within group (order by comuna_unidad) as comuna, nombre_organismo
      from mp_licitacion_comprador group by nombre_organismo, codigo_organismo
    union all
    select nombre_unidad, codigo_organismo, 2, count(*),
           mode() within group (order by region_unidad),
           mode() within group (order by comuna_unidad), min(nombre_organismo)
      from mp_licitacion_comprador group by nombre_unidad, codigo_organismo
  )
  select distinct on (pend.nom) pend.nom, g.codigo_organismo, g.nombre_organismo,
         g.region, nullif(btrim(g.comuna), '') as comuna
    from pend join g on fn_norm_txt(g.nombre) = pend.nom
   where g.codigo_organismo is not null
   order by pend.nom, g.prio, g.n desc;

  update core_pac_lineas p
     set mp_codigo_organismo = t.codigo_organismo, region_mp = fn_cli_region(t.region),
         comuna_mp = t.comuna, updated_at = now()
    from _pac_comprador t
   where p.anio = p_anio and p.organizacion_norm = t.nom
     and p.mp_codigo_organismo is distinct from t.codigo_organismo;

  update core_pac_lineas p
     set organismo_id = o.id, updated_at = now()
    from core_organismos o
   where p.anio = p_anio and p.organismo_id is null
     and o.mp_codigo_organismo = p.mp_codigo_organismo;
  get diagnostics v_org_codigo = row_count;

  --    3. Municipalidades por comuna: "MUNICIPALIDAD DE LOS ANGELES AREA SALUD"
  --       → la municipalidad de la comuna Los Angeles.
  update core_pac_lineas p
     set organismo_id = o.id, updated_at = now()
    from core_organismos o
   where p.anio = p_anio and p.organismo_id is null
     and p.organizacion_norm ~ 'MUNICIPALIDAD'
     and o.tipo = 'municipalidad' and o.comuna is not null
     and o.nombre ~* '^(i\.? |ilustre )?municipalidad'
     and fn_norm_txt(o.comuna) = btrim(substring(p.organizacion_norm from
           'MUNICIPALIDAD (?:DE )?(.+?)(?: - | AREA | UNIDAD | DEPTO| DEPARTAMENTO| DIRECCION|$)'));
  get diagnostics v_org_comuna = row_count;

  --    4. Líneas core cuyo comprador existe en MP pero no en core_organismos:
  --       se crea el organismo (origen 'pac'), igual que hace mp_sync con 'mp'.
  insert into core_organismos (tipo, nombre, region, comuna, mp_codigo_organismo, origen, notas)
  select distinct on (t.codigo_organismo)
         case
           when n ~ 'MUNICIPALIDAD|MUNICIPAL'                        then 'municipalidad'
           when n ~ 'GOBIERNO REGIONAL'                              then 'gobierno_regional'
           when n ~ 'HOSPITAL'                                       then 'hospital'
           when n ~ 'SERVICIO (DE |NACIONAL DE )?SALUD'              then 'servicio_salud'
           when n ~ 'BOMBEROS'                                       then 'bomberos'
           when n ~ 'CARABINEROS'                                    then 'carabineros'
           when n ~ 'POLICIA|GENDARMERIA'                            then 'policia'
           when n ~ 'EJERCITO|ARMADA|FUERZA AEREA'                   then 'fuerzas_armadas'
           when n ~ 'UNIVERSIDAD'                                    then 'universidad'
           when n ~ '^(MINISTERIO|SUBSECRETARIA|SERVICIO NACIONAL|INSTITUTO|DIRECCION GENERAL)' then 'ministerio'
           else 'otro'
         end,
         t.nombre_organismo, fn_cli_region(t.region), t.comuna, t.codigo_organismo, 'pac',
         'Creado por ingesta_pac.py desde el PAC ' || p_anio
    from core_pac_lineas p
    join _pac_comprador t on t.nom = p.organizacion_norm
    cross join lateral (select fn_norm_txt(t.nombre_organismo) as n) x
   where p.anio = p_anio and p.organismo_id is null and p.es_core
  on conflict do nothing;
  get diagnostics v_org_creados = row_count;

  update core_pac_lineas p
     set organismo_id = o.id, updated_at = now()
    from core_organismos o
   where p.anio = p_anio and p.organismo_id is null
     and o.mp_codigo_organismo = p.mp_codigo_organismo;

  -- d) Licitación. Del mismo organismo (por código MP, con índice), publicada
  --    desde 4 meses antes del mes planificado, con un nombre que calce con la
  --    categoría de la línea y que no sea mantención, arriendo ni repuestos.
  --    Si hay varias, la más cercana al mes planificado.
  with lin as (
    select p.id, p.fecha_inicio, p.patron_mp,
           coalesce(o.mp_codigo_organismo, p.mp_codigo_organismo) as cod
      from core_pac_lineas p
      left join core_organismos o on o.id = p.organismo_id
     where p.anio = p_anio and p.es_core and p.patron_mp is not null
       and p.estado <> 'reprogramada'
  ), m as (
    select distinct on (lin.id) lin.id, l.codigo_externo, l.nombre, l.fecha_publicacion, l.estado
      from lin
      join mp_licitacion_comprador c on c.codigo_organismo = lin.cod
      join mp_licitaciones l on l.codigo_externo = c.codigo_externo
     where l.fecha_publicacion >= lin.fecha_inicio - interval '4 months'
       and l.fecha_publicacion <  make_date(p_anio + 1, 7, 1)
       and l.nombre ~* lin.patron_mp
       and l.nombre !~* '(mantenc|mantenim|reparaci|arriendo|arrendamiento|servicio de|seguro|combustible|repuesto|neum[aá]tic|lavado|camilla|insumo|equipamiento|traslado|conversi[oó]n|accesorio)'
     order by lin.id, abs(extract(epoch from (l.fecha_publicacion - lin.fecha_inicio)))
  )
  update core_pac_lineas p
     set codigo_mp = m.codigo_externo, licitacion_nombre = m.nombre,
         licitacion_fecha = m.fecha_publicacion, licitacion_estado = m.estado, updated_at = now()
    from m
   where p.id = m.id and p.codigo_mp is distinct from m.codigo_externo;
  get diagnostics v_licitadas = row_count;

  -- e) Estado. Una línea licitada sigue licitada aunque después la saquen del
  --    plan (los organismos limpian el PAC de lo ya ejecutado).
  update core_pac_lineas p
     set estado = x.nuevo, updated_at = now()
    from (
      select id, case
               when codigo_mp is not null then 'licitada'
               when removed_at is not null then 'retirada'
               when (date_trunc('month', fecha_inicio) + interval '1 month')::date
                    + p_dias_gracia < v_hoy then 'vencida_sin_licitar'
               else 'planificada'
             end as nuevo
        from core_pac_lineas
       where anio = p_anio and estado <> 'reprogramada'
    ) x
   where p.id = x.id and p.estado is distinct from x.nuevo;

  select jsonb_object_agg(estado, n) into v_estados
    from (select estado, count(*) n from core_pac_lineas
           where anio = p_anio and es_core group by estado) s;

  return jsonb_build_object(
    'retiradas', v_retiradas, 'reprogramadas', v_reprog,
    'organismo_por_nombre', v_org_nombre, 'organismo_por_codigo', v_org_codigo,
    'organismo_por_comuna', v_org_comuna, 'organismos_creados', v_org_creados,
    'licitaciones_nuevas', v_licitadas, 'core_por_estado', coalesce(v_estados, '{}'::jsonb));
end;
$function$;

-- Escribe en core_organismos y core_pac_lineas: sólo para service_role.
revoke execute on function public.fn_core_pac_procesar(integer, timestamptz, boolean, integer)
  from public, anon, authenticated;

-- ── 4. Vistas ──────────────────────────────────────────────────────────────
create or replace view public.v_core_pac_lineas as
select p.*,
       o.nombre      as organismo,
       o.tipo        as organismo_tipo,
       o.prioridad   as organismo_prioridad,
       coalesce(o.region, p.region_mp) as region,
       coalesce(o.comuna, p.comuna_mp) as comuna,
       r.fecha_inicio as reprograma_de_fecha,
       s.estado_gestion as senal_estado_gestion,
       s.score       as senal_score,
       (p.senal_estado is not distinct from p.estado
        and p.senal_fecha is not distinct from p.fecha_inicio) as senal_sincronizada,
       case when p.codigo_mp is not null then
         'https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion=' || p.codigo_mp
       end as licitacion_url
  from public.core_pac_lineas p
  left join public.core_organismos o on o.id = p.organismo_id
  left join public.core_pac_lineas r on r.id = p.reprograma_de_id
  left join public.core_senales s on s.id = p.senal_id;

-- Cumplimiento: de las líneas core cuyo mes (más la gracia de 60 días) ya
-- pasó, cuántas llegaron a licitación. Es el número que responde "¿cuánto
-- del PAC termina en licitación?" con datos propios, por tipo de comprador.
create or replace view public.v_core_pac_cumplimiento as
select p.anio,
       coalesce(o.tipo, 'sin_organismo') as organismo_tipo,
       p.categoria,
       count(*)                                                     as lineas,
       count(*) filter (where p.estado in ('licitada','vencida_sin_licitar')) as lineas_vencidas,
       count(*) filter (where p.estado = 'licitada')                as licitadas,
       count(*) filter (where p.estado = 'vencida_sin_licitar')     as vencidas_sin_licitar,
       count(*) filter (where p.estado = 'planificada')             as planificadas,
       count(*) filter (where p.estado = 'retirada')                as retiradas,
       round(100.0 * count(*) filter (where p.estado = 'licitada')
             / nullif(count(*) filter (where p.estado in ('licitada','vencida_sin_licitar')), 0), 1)
                                                                    as pct_licitado,
       sum(p.total)                                                 as monto_total,
       sum(p.total) filter (where p.estado = 'planificada')         as monto_planificado
  from public.core_pac_lineas p
  left join public.core_organismos o on o.id = p.organismo_id
 where p.es_core and p.estado <> 'reprogramada'
 group by 1, 2, 3;

-- ── 5. Fuente ──────────────────────────────────────────────────────────────
insert into public.core_sources (source_type, landing_url, enabled, notes, slug, nombre,
                                 fuente_tipo, parser, frecuencia_horas, config, rate_limit_ms)
values ('plan_compras',
        'https://www.mercadopublico.cl/Home/PlanDeCompra/ResultadoBusquedaOportunidades',
        true,
        'Export público VerExcelOportunidades (sin ticket). Rubros 173 y 191 más palabras clave con y sin tilde. Lo procesa ingesta_pac.py, no scrape_core.py.',
        'pac-mercadopublico', 'Plan Anual de Compras · Mercado Público', 'plan_compras',
        'pac_export', 24, '{"rubros": [173, 191]}'::jsonb, 2000)
on conflict (slug) do nothing;
