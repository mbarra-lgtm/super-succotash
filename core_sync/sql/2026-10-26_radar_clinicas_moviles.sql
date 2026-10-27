-- ═══════════════════════════════════════════════════════════════════════
-- Radar: categoría clinica_movil (26-10-2026)
--
-- El acta CORE Biobío N°9 (14-05-2026) aprobó tres clínicas veterinarias
-- móviles (Antuco, Cabrero, Tucapel) y una dental (Santa Bárbara) y el radar
-- no registró ninguna. Tres causas, las tres arregladas en este cambio:
--   1. Sin keywords de clínica: el clasificador solo lee los pasajes alrededor
--      de una keyword, así que esos tramos nunca llegaron al modelo.
--   2. El prompt describía a Bertonati sin clínicas móviles (radar-v4 las suma).
--   3. Sin categoría: el CHECK y el enum no tenían dónde ponerlas.
--
-- YA APLICADO en AccessRose como migración radar_categoria_clinica_movil.
-- ═══════════════════════════════════════════════════════════════════════

alter table public.core_senales drop constraint core_senales_categoria_check;
alter table public.core_senales add constraint core_senales_categoria_check
  check (categoria = any (array['ambulancia','carro_bomba','blindado','movil_policial','rescate','carroceria','clinica_movil','otro']));

alter table public.core_keywords drop constraint core_keywords_categoria_check;
alter table public.core_keywords add constraint core_keywords_categoria_check
  check (categoria = any (array['ambulancia','carro_bomba','blindado','movil_policial','rescate','carroceria','clinica_movil','contexto','otro']));

-- fn_core_prefiltro hace strpos sin tildes y en minúsculas: 'veterinaria movil'
-- también encuentra 'Clínica Veterinaria Móvil'. Los plurales van aparte porque
-- 'veterinarias moviles' no contiene 'veterinaria movil'.
-- Sin keywords negativas: distinguir compra de uso ("operativo de la clínica
-- móvil") lo hace el modelo. En prensa una negativa pesa x2 y botaría notas
-- buenas del tipo "compran clínica veterinaria móvil para operativos".
insert into public.core_keywords (termino, categoria, es_negativo, peso, activa, notas) values
  ('clinica movil',            'clinica_movil', false, 3, true, 'Genérico; Acta CORE Biobío N°9 14-05-2026'),
  ('clinicas moviles',         'clinica_movil', false, 3, true, null),
  ('veterinaria movil',        'clinica_movil', false, 3, true, 'Mulchén 26-08-2026 dice "Veterinaria Móvil" sin "clínica"'),
  ('veterinarias moviles',     'clinica_movil', false, 3, true, null),
  ('dental movil',             'clinica_movil', false, 3, true, 'Santa Bárbara, Acta N°9 14-05-2026'),
  ('dentales moviles',         'clinica_movil', false, 3, true, null),
  ('oftalmologica movil',      'clinica_movil', false, 3, true, 'Coronel, Acta N°23 12-12-2025'),
  ('oftalmologicas moviles',   'clinica_movil', false, 3, true, null),
  ('unidad medica movil',      'clinica_movil', false, 3, true, null),
  ('unidades medicas moviles', 'clinica_movil', false, 3, true, null),
  ('box movil',                'clinica_movil', false, 2, true, null),
  ('mamografo movil',          'clinica_movil', false, 3, true, null),
  ('laboratorio movil',        'clinica_movil', false, 2, true, null);

create or replace function public.fn_categoria_cotizacion(p_nombre text)
 returns text language sql immutable as $function$
  select case
    when p_nombre ~* '(ambulancia|\mAEB\M|\mAEA\M|\mSAMU\M)' then 'ambulancia'
    when p_nombre ~* '(carro bomba|material mayor|carro de bomba)' then 'carro_bomba'
    when p_nombre ~* '(blindad|lanza aguas)'                      then 'blindado'
    when p_nombre ~* '(rescate|primera intervencion|primera intervención)' then 'rescate'
    when p_nombre ~* '(seguridad ciudadana|movil policial|móvil policial)' then 'movil_policial'
    when p_nombre ~* '(cl[ií]nica|veterinari|dental|oftalmol[oó]gic|unidad m[eé]dica|mam[oó]grafo)' then 'clinica_movil'
    else null
  end;
$function$;

-- ── Reclasificar el histórico (correr DESPUÉS de mergear radar-v4) ─────────
-- Las actas con clínicas ya están clasificadas, así que el pipeline no las
-- vuelve a mirar. Esto las devuelve a la cola; las señales de ambulancia que
-- ya existen no se duplican porque core_senales tiene on_conflict dedupe_key.
--
-- update public.core_documents
--    set clasificado_at = null, prefiltro_ok = true
--  where extraction_method <> 'rss'
--    and translate(lower(extracted_text), 'áéíóúüñ', 'aeiouun')
--        ~ '(veterinarias? movil|dental(es)? movil|oftalmologicas? movil|clinicas? movil|unidad(es)? medicas? movil|mamografo movil)';
