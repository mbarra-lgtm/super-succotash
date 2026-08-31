# Radar Comercial CORE — orquestación

Monitoreo diario de acuerdos de los 16 Consejos Regionales para detectar
financiamiento aprobado de ambulancias, carros bomba y blindados **antes** de
que la licitación llegue a Mercado Público. Complementa a `mp_sync`: éste ve la
licitación cuando se publica; el radar ve el dinero cuando se aprueba, 4 a 8
meses antes.

## Flujo

```
core_sources ──scrape_core.py──> core_documents ──clasificar_senales.py──> core_senales
  (16 GORE)     descubre, baja,      (texto +            pasajes →           (categoría,
                OCR, salud           prefiltro)          Claude              etapa, score,
                por fuente)                                                  dedupe)
```

## Secretos

Además de los de `mp_sync` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`), este
pipeline necesita uno nuevo:

| Secret | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | key de la API de Anthropic para el clasificador |

## Workflow

| Archivo | Cadencia (UTC) | Scripts |
|---|---|---|
| `core-radar.yml` | 11:00 y 23:00 L-V (≈07:00 y 19:00 CL) | `scrape_core.py` + `clasificar_senales.py` |

La cadencia real por fuente la decide la base: `core_sources.frecuencia_horas`
(12 h para regiones prioritarias, 24 h para el resto). El workflow solo pregunta
"¿a quién le toca?".

## Esquema en Supabase

| Tabla / vista | Rol |
|---|---|
| `core_organismos` | Catálogo canónico de compradores. Eje común con MP vía `mp_codigo_organismo` y con Odoo vía `res_partner_id` |
| `core_sources` | Fuentes con scheduling y salud (`last_ok_at`, `consecutive_errors`, `last_document_at`) |
| `core_documents` | Documentos con texto, prefiltro y trazabilidad de clasificación |
| `core_senales` | **La unidad de valor**: un hecho comercial con etapa, score, dedupe y estado de gestión |
| `core_senal_feedback` | Marca útil/ruido por señal — es lo que permite recalibrar |
| `core_keywords` | Diccionario del prefiltro, editable sin tocar código |
| `fn_core_prefiltro()` | Extrae pasajes alrededor de cada keyword |
| `fn_core_dedupe_key()` | Clave estable: la misma compra puede llegar del acta, la prensa y MP |
| `fn_core_score()` | Score 0-100 = etapa(40) + tamaño(25) + prioridad organismo(20) + confianza(15) |
| `v_core_radar_diario` | Lo detectado ayer, para el correo |
| `v_core_fuentes_salud` | Semáforo por fuente |

## Lo que enseñó el piloto

Estos números salen de correr el prefiltro contra los 8 documentos que quedaron
de la corrida manual, no de una estimación:

| Hallazgo | Dato | Qué se hizo |
|---|---|---|
| El scraper bajaba cualquier PDF | 5 de 8 no eran actas (un instructivo FIC-R, un horario de atención, PDFs de 2017/2020) | `PATRON_RUIDO` + `PATRON_UTIL` + filtro por año: de 8 quedan 3 |
| 2 regiones publican escaneados | Atacama 17 y O'Higgins 18 chars/página | OCR obligatorio; sin él el documento queda `status='error'`, no `parsed` vacío |
| El keyword matching repite el hecho | 12 filas en `core_projects` para **un solo** acuerdo | `fusionar_pasajes()` + `dedupe_key` único |
| El keyword matching no distingue rubro | "Cuartel de bomberos" ≠ carro bomba | Regla explícita en el prompt |
| Señales reales en 8 documentos | **0** | El problema del radar no es el volumen, es la precisión |

## Costo

Un acta ordinaria del Biobío son 177.000 caracteres. El prefiltro la reduce a
~14 pasajes de 700 (**94% menos texto**). Con el system prompt cacheado,
clasificar un acta cuesta ~US$ 0,015; a ~50 actas al mes, menos de un dólar.

## Volumen esperado

16 CORE × 2-4 sesiones al mes ≈ 50 actas mensuales, de las que se esperan
**2 a 5 señales reales al mes**. Un correo *diario* llegaría vacío 9 de cada 10
días. Antes de conectar el digest hay que decidir: o se suman prensa + MP + BIP
para que el diario tenga contenido, o el CORE va como alerta inmediata + resumen
semanal.

## Salud de las fuentes

```sql
select slug, estado, dias_sin_documento, consecutive_errors, last_error
from v_core_fuentes_salud where estado <> 'ok';
```

`muda` es el estado peligroso: responde 200 pero no entrega documentos hace 60
días. Casi siempre el sitio cambió el HTML, no que no hubo sesiones. Revisar
`config->selector_contenedor` de esa fuente.

## Agregar una fuente

No requiere tocar código si reutiliza un parser existente:

```sql
insert into core_sources (slug, nombre, organismo_id, region, fuente_tipo, parser,
                          landing_url, frecuencia_horas, config)
values ('muni-curico-actas', 'Concejo Municipal de Curicó',
        (select id from core_organismos where nombre = 'Ilustre Municipalidad de Curicó'),
        'Maule', 'acta_concejo', 'html_list',
        'https://www.curico.cl/actas-concejo/', 24,
        '{"selector_contenedor": "#contenido", "max_por_corrida": 10}');
```

## Pendiente

- 4 regiones sin URL de acuerdos cargada: Los Ríos, Los Lagos, Aysén, Magallanes.
- Los ~30 concejos municipales prioritarios.
- El digest por correo (reusa `mp_alert_rules.include_core`, ya existe la bandera).
- Panel `RadarComercialPanel.tsx` en AccessPoint sobre `v_core_senales_enriquecidas`.
