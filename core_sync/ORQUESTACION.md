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

## Las dos vías de ingesta

| | Actas CORE | Prensa (RSS) |
|---|---|---|
| Script | `scrape_core.py` → `clasificar_senales.py` | `ingesta_prensa.py` |
| Velocidad | Lenta: el acta se publica 2-6 semanas después de la sesión | Rápida: la nota sale el mismo día |
| Confiabilidad | Alta: número de acuerdo, monto exacto, votación | Media: titular y bajada, sin detalle |
| Ruido | Burocrático (glosas presupuestarias) | Crónica policial y de incendios |
| Filtro | Positivo: pasa casi todo, decide el modelo | **Negativo pesa doble** que el positivo |
| Llamadas al modelo | Una por acta | Una por lote de 20 notas |

**La prensa dispara, el CORE confirma.** Cuando el acta llega semanas después,
`fn_core_dedupe_key` la une a la señal que la prensa ya había levantado; no se
duplica, se enriquece con el número de acuerdo.

### El filtro invertido de prensa

En las actas, "ambulancia" casi no aparece y cuando aparece importa. En la
prensa chilena es sobre todo crónica: *"fue trasladado en ambulancia al
hospital"*. Por eso `filtrar_nota()` calcula
`puntaje = positivos − 2 × negativos` y descarta si no es mayor que cero:

```
"Joven resultó herido tras accidente y fue trasladado en ambulancia"   → −19  descarta
"Cinco carros bomba concurrieron al incendio en Talcahuano"            →  −3  descarta
"Core del Maule aprueba $980 millones del FNDR para 7 ambulancias"     →  +8  clasifica
"Municipalidad de Curicó entrega nuevo carro bomba"                    →  +3  clasifica
```

El factor 2 se calibra con `core_senal_feedback`. Si aparecen falsos negativos,
bajarlo antes que borrar keywords negativas.

## Tercera vía: avisos legales de Defensa

`ingesta_avisos_defensa.py` (13-10-2026). El Ejército, la Armada y la FACh
compran fuera de Mercado Público todo lo financiado por la Ley 21.174 y lo
excluido por la Ley 19.886: lo anuncian como aviso legal en
diarioavisoslegales.cl y El Mercurio. El espejo MP no lo ve nunca.

```
Google News RSS ──> URL real ──> aviso + PDF ──> Claude ──> core_senales
 (4 consultas)      (batch-      (directo o      (tabla)    (1 por licitación,
                     execute)     web_fetch)                  fecha_limite + contacto)
```

| | Prensa | Avisos de Defensa |
|---|---|---|
| Qué dice el título | El hecho | Nada: "LLAMADO A PROPUESTA PÚBLICA – EJÉRCITO DE CHILE" |
| Dónde está el dato | Titular y bajada | Tabla en un PDF: número, rubro, correo, cierre |
| Filtro | Keywords, negativo pesa doble | Ninguno: se lee cada aviso (2-4 al mes) |
| Lectura | No se baja la nota | Se lee el PDF con `web_fetch` (Cloudflare bloquea el GET directo) |
| Dato que manda | Monto | Cierre de solicitud de bases → `core_senales.fecha_limite` |

Cosas que hay que saber:

- **Sin REPE no hay bases.** El Ejército entrega bases sólo a empresas
  inscritas, habilitadas y vigentes en el Registro Especial de Proveedores del
  Ejército; la Armada y la FACh tienen sus propios registros.
- **El "encaje" entra como prioridad** en `fn_core_score` (alto=1, medio=3,
  bajo=5): con etapa licitación, encaje alto ≈ 57 (correo a todos), medio ≈ 46
  (comercial), bajo ≈ 33 (sólo panel). La mayoría de los llamados son botas,
  repuestos y aviación: encaje bajo, no llegan al correo.
- **Un aviso que no se pudo leer igual se avisa** (score ≈ 37, sólo a quien
  tiene umbral 35) y se reintenta 21 días. Cuando se lee, la señal pendiente se
  descarta sola y entran las licitaciones.
- **Dedupe por número + correo**: el mismo llamado se publica 2 días seguidos
  y en página y PDF; DIVMANT y Aviación usan la misma numeración (07-2026).
  Si se republica con otra fecha límite, se corre `fecha_limite` (prórroga).
- `acuerdo_numero` guarda el número de licitación; `v_core_defensa_llamados`
  lista los llamados con días para el cierre.
- Un aviso puntual se carga a mano con
  `python ingesta_avisos_defensa.py --url <url del aviso o del PDF>`.

## Cuarta vía: Plan Anual de Compras (PAC)

`ingesta_pac.py` (25-11-2026). Cada organismo publica en enero lo que piensa
comprar en el año, línea por línea, con monto y mes. Es la señal más temprana
*dentro* de Mercado Público, pero indicativa: el PAC no obliga a comprar y el
organismo lo cambia cuando quiere. Medido con "ambulancias" (23-11-2026):
cerca de un tercio de las líneas con el mes vencido terminó en licitación.

```
buscador PAC ──> core_pac_lineas ──fn_core_pac_procesar()──> estado + codigo_mp ──> core_senales
(~45 consultas:   (foto diaria,      organismo, retiradas,                          (fuente_tipo
 rubros 173/191    upsert por        reprogramadas, cruce                            'plan_compras')
 + palabras)       linea_key)        con mp_licitaciones)
```

| | Avisos de Defensa | PAC |
|---|---|---|
| Fuente | PDF en un diario | Export público `VerExcelOportunidades` (tabla HTML) |
| Modelo | Claude lee la tabla | Ninguno: regex sobre el nombre de la línea |
| Etapa | `licitacion_publicada` | `idea` (pasa a `licitacion_publicada` cuando se encuentra la licitación) |
| Confianza | 0,95 | 0,6 planificada · 0,35 vencida sin licitar · 0,9 licitada |

Estados de una línea (`core_pac_lineas.estado`):

- **planificada**: el mes todavía no pasa (más 60 días de gracia).
- **vencida_sin_licitar**: pasó el mes y no hay licitación del organismo que
  calce. Es la señal comercial más interesante: el dinero estaba en el plan.
  Puede haberse comprado por convenio marco o trato directo (no se cruza todavía).
- **licitada**: hay una licitación del mismo organismo (por código MP), publicada
  desde 4 meses antes del mes planificado, cuyo nombre calza con la categoría.
- **retirada**: la línea dejó de aparecer en el PAC. Sólo se marca si *todas*
  las consultas respondieron; si la señal seguía `nueva`, se descarta sola.
- **reprogramada**: el organismo cambió el mes o el monto. Las líneas no tienen
  ID, así que aparece una línea nueva: se une a la vieja por organismo + nombre
  y la señal se mueve a la nueva en vez de duplicarse.

Cosas que hay que saber:

- **El buscador distingue tildes**: "retén móvil" trae filas, "reten movil" no.
  Cada término con tilde va en las dos formas en `PALABRAS`.
- **Sin resultados devuelve `<div></div>`** (15 bytes), no un error. Cualquier
  otra respuesta sin filas se trata como falla de la consulta.
- **Organismo en cuatro pasos**: nombre exacto en `core_organismos` → catálogo
  de compradores de `mp_licitacion_comprador` (nombre de organismo o de unidad)
  → municipalidad por comuna ("MUNICIPALIDAD DE LOS ANGELES AREA SALUD") → se
  crea el organismo con `origen = 'pac'`.
- **Señales sólo para lo vigente**: planificadas y vencidas de los últimos 180
  días. Lo ya licitado al detectarlo y lo vencido hace más tiempo queda en la
  tabla para medir cumplimiento, sin señal.
- **Score**: ambulancia/clínica/carro bomba/rescate/blindado/carrocería/móvil
  policial = encaje alto (prioridad 2); minibús/furgón/van adaptada = medio
  (prioridad 4, casi nunca llega al correo). Una línea planificada de $500
  millones o más ronda 56 y entra al digest (umbral 45); una de $100 millones ronda 47. Las vencidas bajan a ~35-40.
- **Enero**: el PAC del año nuevo se publica en enero. Hasta marzo el script
  procesa también el plan del año anterior, porque las líneas de noviembre y
  diciembre se licitan en enero o febrero.
- `v_core_pac_cumplimiento` da el % de líneas vencidas que llegaron a
  licitación, por tipo de organismo y categoría. Es la respuesta con datos
  propios a "¿cuánto del PAC termina en licitación?".

## Secretos

Además de los de `mp_sync` (`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`), este
pipeline necesita uno nuevo:

| Secret | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | key de la API de Anthropic para el clasificador |

## Workflow

| Archivo | Cadencia (UTC) | Scripts |
|---|---|---|
| `core-radar.yml` | 11:00 y 23:00 L-V (≈07:00 y 19:00 CL) | `scrape_core.py` + `clasificar_senales.py` + `ingesta_prensa.py` + `ingesta_avisos_defensa.py` + `ingesta_pac.py` (1 vez al día) |

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
| `v_core_defensa_llamados` | Llamados de Defensa con número, cierre, días para el cierre y correo |
| `core_pac_lineas` | Foto diaria del Plan Anual de Compras: líneas, estado, organismo y licitación encontrada |
| `fn_core_pac_procesar()` | Organismo, retiradas, reprogramadas, cruce con `mp_licitaciones` y estado |
| `v_core_pac_lineas` | Detalle de líneas con organismo, región y señal |
| `v_core_pac_cumplimiento` | % del PAC que termina en licitación, por tipo de organismo y categoría |

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
