-- YA APLICADA en Supabase (AccessRose) el 13-10-2026 como migración radar_avisos_defensa.
-- Se versiona para que el esquema del radar quede reproducible.

alter table core_sources drop constraint core_sources_fuente_tipo_check;
alter table core_sources add constraint core_sources_fuente_tipo_check check (fuente_tipo = any (array[
  'acuerdo_core','acta_core','tabla_sesion_core','acta_concejo','rss_prensa','bip','youtube',
  'portal_transparencia','aviso_legal','otro']));

alter table core_organismos drop constraint core_organismos_tipo_check;
alter table core_organismos add constraint core_organismos_tipo_check check (tipo = any (array[
  'gobierno_regional','municipalidad','servicio_salud','bomberos','carabineros','ministerio',
  'fuerzas_armadas','otro']));

alter table core_senales add column if not exists fecha_limite date;
alter table core_senales add column if not exists contacto text;

insert into core_organismos (tipo, nombre, nombre_corto, region, prioridad, sitio_web, notas, activo) values
  ('fuerzas_armadas','Ejército de Chile','Ejército','Metropolitana',2,'https://www.ejercito.cl',
   'Compras fuera de Mercado Público (Ley 21.174 / exclusiones Ley 19.886) vía DIVAE. Requiere REPE vigente para pedir bases.', true),
  ('fuerzas_armadas','Armada de Chile','Armada','Valparaíso',2,'https://www.armada.cl',
   'Propuestas públicas fuera de Mercado Público. Requiere Registro Especial de Proveedores de la Armada.', true),
  ('fuerzas_armadas','Fuerza Aérea de Chile','FACh','Metropolitana',2,'https://www.fach.mil.cl',
   'Propuestas públicas fuera de Mercado Público. Registro de proveedores SISREP.', true);

-- Fuentes (config final después de la medición de cobertura del 13-10-2026)
insert into core_sources (source_type, landing_url, enabled, notes, slug, nombre, fuente_tipo, parser, frecuencia_horas, config, rate_limit_ms) values
 ('aviso_legal',
  'https://news.google.com/rss/search?q=site:diarioavisoslegales.cl+(ej%C3%A9rcito+OR+armada+OR+%22fuerza+a%C3%A9rea%22+OR+FACh)+(%22propuesta+p%C3%BAblica%22+OR+licitaci%C3%B3n+OR+proveedores+OR+precalificaci%C3%B3n)&hl=es-419&gl=CL&ceid=CL:es-419',
  true, 'diarioavisoslegales.cl está detrás de Cloudflare: se descubre vía Google News y se lee con web_fetch de la API de Anthropic.',
  'avisos-defensa-diarioavisoslegales', 'Avisos legales · Defensa (diarioavisoslegales.cl)', 'aviso_legal', 'rss_google_news_avisos', 12,
  '{"dias_atras": 45, "dominios": ["diarioavisoslegales.cl"], "consultas": ["site:diarioavisoslegales.cl ejército", "site:diarioavisoslegales.cl \"propuesta pública\"", "site:diarioavisoslegales.cl armada"]}', 3000),
 ('aviso_legal',
  'https://news.google.com/rss/search?q=%22Divisi%C3%B3n+de+Adquisiciones+del+Ej%C3%A9rcito%22+OR+%22Registro+Especial+de+Proveedores+del+Ej%C3%A9rcito%22+OR+%22llamado+a+propuesta+p%C3%BAblica%22+(ej%C3%A9rcito+OR+armada+OR+FACh)&hl=es-419&gl=CL&ceid=CL:es-419',
  true, 'Red ancha: mismos llamados publicados en otros medios.',
  'avisos-defensa-red-ancha', 'Avisos legales · Defensa (otros medios)', 'aviso_legal', 'rss_google_news_avisos', 24,
  '{"dias_atras": 45, "requiere": "propuesta p[uú]blica|licitaci|adquisici|proveedores|precalificaci|llamado", "senal_si_falla": false}', 3000);

-- v_core_senales_enriquecidas: se agregan fecha_limite y contacto AL FINAL
-- (create or replace view sólo permite agregar columnas al final; la usan
-- fn_core_senales_para_digest, v_core_radar_diario y v_core_senales_panel).
create or replace view v_core_senales_enriquecidas as
 SELECT s.id, s.fecha_evento,
    ((s.created_at AT TIME ZONE 'America/Santiago'::text))::date AS fecha_deteccion_cl,
    s.categoria, s.etapa, s.titulo, s.resumen, s.por_que_importa, s.unidades, s.monto_clp,
    COALESCE(s.region, o.region) AS region, COALESCE(s.comuna, o.comuna) AS comuna,
    o.id AS organismo_id, o.nombre AS organismo, o.tipo AS organismo_tipo, o.prioridad AS organismo_prioridad,
    o.crm_es_cliente, s.score, s.confianza, s.estado_gestion, s.crm_project_id, s.codigo_bip, s.codigo_mp,
    s.acuerdo_numero, s.fuente_tipo, s.fuente_url, src.nombre AS fuente_nombre,
    d.title AS documento_titulo, d.act_date AS documento_fecha, s.senal_padre_id,
    ( SELECT count(*) FROM core_senal_feedback f WHERE f.senal_id = s.id AND f.util) AS votos_util,
    ( SELECT count(*) FROM core_senal_feedback f WHERE f.senal_id = s.id AND NOT f.util) AS votos_ruido,
    s.created_at, d.autor_medio AS medio, d.published_date AS medio_fecha,
    s.fecha_limite, s.contacto
   FROM core_senales s
     LEFT JOIN core_organismos o ON o.id = s.organismo_id
     LEFT JOIN core_sources src ON src.id = s.source_id
     LEFT JOIN core_documents d ON d.id = s.document_id;

create or replace view v_core_defensa_llamados as
select s.id, o.nombre_corto as organismo, s.acuerdo_numero as numero, s.titulo, s.resumen,
       s.categoria, s.fecha_evento as publicado, s.fecha_limite as cierre_solicitud,
       s.fecha_limite - (now() at time zone 'America/Santiago')::date as dias_para_cierre,
       s.contacto, s.score, s.confianza, s.estado_gestion, s.fuente_url,
       (s.fecha_limite is null or s.fecha_limite >= (now() at time zone 'America/Santiago')::date) as abierto
  from core_senales s
  left join core_organismos o on o.id = s.organismo_id
 where s.fuente_tipo = 'aviso_legal';
