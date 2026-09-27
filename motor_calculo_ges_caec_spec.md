# Motor de cálculo GES/CAEC — versión lite

> Proyecto de práctica. Datos sintéticos, reglas simplificadas.
> No es una implementación de producción ni una referencia normativa.

---

## 1. Qué es esto

Un motor de cálculo para deducibles GES y CAEC: consolida prestaciones desde fuentes
heterogéneas, aplica reglas normativas **versionadas en el tiempo**, y produce un
resultado **auditable** que explica cómo llegó a cada número.

El objetivo no es la sofisticación del cálculo —las reglas son aritmética simple—
sino la **correctitud bajo cambio**: qué pasa cuando la normativa cambia
retroactivamente, o cuando llegan datos tardíos que alteran un período ya calculado.

### Qué hace a esto un "motor" y no un script

1. **Las reglas son datos, no código.** Un cambio normativo no requiere tocar Python
   ni redesplegar. Prueba: ¿puede cambiar una regla sin un cambio de código?
2. **El resultado incluye su propia justificación.** No solo "el copago es 195.000",
   sino qué versión de regla se aplicó, sobre qué ventana, con qué insumos.
3. **Determinista y reproducible.** Mismos insumos + misma versión de regla →
   mismo resultado, siempre.

---

## 2. Conceptos de fondo

### 2.1 Bitemporalidad

Dos ejes de tiempo, y confundirlos es la fuente de los bugs más sutiles:

| | Qué es | En este proyecto |
|---|---|---|
| **Valid time** | Cuándo el hecho fue verdadero en el mundo real | `fecha_prestacion`, `valid_from`/`valid_to` de las reglas |
| **Transaction time** | Cuándo el sistema se enteró del hecho | `recibido_en`, `calculado_en` |

Permite responder dos preguntas distintas: *"qué era verdad"* y *"qué creíamos
nosotros"*. Para poder hacerlo, **la información no se descarta nunca**, ni siquiera
si resultó errónea.

> **El ejemplo canónico (Martin Fowler):** un sistema de nóminas sabe que un empleado
> gana $100/día desde el 1 de enero. El 25 de febrero se corre la nómina con esa tarifa.
> El 15 de marzo se descubre que, con efecto desde el 15 de febrero, la tarifa cambió
> a $211/día. ¿Cuál era la tarifa el 20 de febrero? Hay dos respuestas correctas, y
> cuál aplica depende de si preguntas por valid time o transaction time.

Es un patrón estandarizado: SQL:2011 incorporó `PERIOD FOR SYSTEM_TIME`.
Postgres no lo implementa nativamente, así que acá se construye a mano.

Referencias: `martinfowler.com` (bitemporal history) · `juxt.pro/blog/value-of-bitemporality` ·
`v1-docs.xtdb.com/concepts/bitemporality`

### 2.2 SCD2 (Slowly Changing Dimension tipo 2)

Cada versión de una regla es una fila nueva con su intervalo de vigencia.
Nada se sobrescribe. Es lo que dbt llama *snapshots*.

### 2.3 Funciones puras

Una función pura: (1) mismos inputs → mismo output, (2) sin efectos secundarios.
No lee de la base, no escribe, no llama a `datetime.now()`, no muta sus argumentos.

Acá importa por tres razones:
- **Testeo:** `assert deducible_caec_uf(3.5) == 105` vs. necesitar una base montada.
- **Auditabilidad:** el resultado depende solo de los argumentos, que quedan registrados.
- **Reproducibilidad:** `datetime.now()` dentro del cálculo destruye la idempotencia.
  La fecha de cálculo es un parámetro, no algo que la función averigua.

---

## 3. Arquitectura

### 3.1 Capas

```
repositorio/    → IMPURO: lee SQL, devuelve objetos Python planos
motor/          → PURO: reglas + prestaciones → resultado
persistencia/   → IMPURO: escribe la nueva versión del resultado
```

El I/O vive en los bordes; la lógica de negocio queda pura en el centro
("functional core, imperative shell"). La parte con más probabilidad de estar
mal es también la más fácil de testear.

### 3.2 Estructura del repo

```
motor-ges-caec/
├── README.md
├── requirements.txt          # versiones fijas, no rangos
├── Dockerfile
├── src/
│   ├── __init__.py
│   ├── motor.py              # CLI / entrypoint
│   ├── repositorio.py        # lectura SQL
│   ├── persistencia.py       # escritura de resultados
│   ├── reglas.py             # effective-dated lookup
│   ├── calculo.py            # PURO: deducible, acumulado, desglose
│   └── validacion.py         # chequeos de consistencia
├── sql/
│   └── schema.sql
├── data/
│   └── sintetico/            # generadores de datos de prueba
└── tests/
    ├── test_calculo.py
    ├── test_reglas.py
    └── test_recalculo.py
```

### 3.3 Decisiones de diseño (y por qué)

| Decisión | Razón |
|---|---|
| **SQL/SCD2 como fuente de verdad** | Historial consultable, join por fecha, integridad referencial con prestaciones |
| **YAML descartado** | Se evaluó para tener revisión por PR. Trabajando solo es una capa extra sin el beneficio que la justifica. Trazabilidad se logra con `fuente`, `cargado_por`, `cargado_en` + script de carga en vez de INSERTs manuales. Reconsiderar si el equipo crece o si alguien debe aprobar cada cambio |
| **Recálculo completo, no incremental** | Más lento pero mucho más fácil de razonar y verificar. Como la ventana puede desplazarse, lo incremental se complica rápido. El volumen no justifica la complejidad. Correctitud sobre astucia |
| **Batch/CLI, no servicio** | Herramienta interna de cálculo. NO API, NO Kubernetes → sobre-ingeniería |
| **Docker por reproducibilidad** | Un cálculo regulado debe dar el mismo número en dos años. Versiones fijas en `requirements.txt` |
| **SQLite** | Suficiente para el alcance; el patrón es idéntico en Postgres |

---

## 4. Modelo de datos

```sql
-- ───────────────────────────────────────────────────────────
-- REGLAS: SCD2. Qué decía la norma y desde cuándo.
-- ───────────────────────────────────────────────────────────
CREATE TABLE parametros_ges (
    regla_id          INTEGER PRIMARY KEY,
    codigo_ges        TEXT    NOT NULL,
    valid_from        DATE    NOT NULL,
    valid_to          DATE,                    -- NULL = vigente
    tope_cotizaciones INTEGER NOT NULL,
    tope_uf           REAL    NOT NULL,
    fuente            TEXT,                    -- qué decreto
    cargado_por       TEXT,
    cargado_en        TIMESTAMP NOT NULL
);

CREATE TABLE parametros_caec (
    regla_id          INTEGER PRIMARY KEY,
    valid_from        DATE    NOT NULL,
    valid_to          DATE,
    factor_base       INTEGER NOT NULL,        -- 30
    factor_multiple   INTEGER NOT NULL,        -- 43
    piso_uf           REAL    NOT NULL,        -- 60
    techo_base_uf     REAL    NOT NULL,        -- 126
    techo_multiple_uf REAL    NOT NULL,        -- 181
    fuente            TEXT,
    cargado_por       TEXT,
    cargado_en        TIMESTAMP NOT NULL
);

-- Valor UF por fecha. Lookup efectivo-fechado también.
CREATE TABLE valores_uf (
    fecha             DATE PRIMARY KEY,
    valor_clp         REAL NOT NULL
);

-- ───────────────────────────────────────────────────────────
-- HECHOS: append-only. Nunca se actualiza una fila.
-- ───────────────────────────────────────────────────────────
CREATE TABLE prestaciones (
    prestacion_id     INTEGER PRIMARY KEY,
    beneficiario_id   INTEGER NOT NULL,
    fecha_prestacion  DATE    NOT NULL,   -- VALID TIME
    recibido_en       TIMESTAMP NOT NULL, -- TRANSACTION TIME
    codigo_ges        TEXT,               -- NULL si no es GES
    en_red_caec       BOOLEAN NOT NULL,
    monto_arancel     REAL    NOT NULL,
    monto_copago      REAL    NOT NULL
);
-- fecha_prestacion != recibido_en ES la señal de dato tardío.
-- No se puede reconstruir después si solo se guarda una.

CREATE TABLE beneficiarios (
    beneficiario_id   INTEGER PRIMARY KEY,
    cotizacion_pactada_uf REAL NOT NULL,
    fecha_derivacion_caec DATE           -- NULL si no derivado
);

-- ───────────────────────────────────────────────────────────
-- RESULTADOS: versionados. Nunca se sobrescribe un cálculo.
-- ───────────────────────────────────────────────────────────
CREATE TABLE calculos_resultado (
    id                   INTEGER PRIMARY KEY,
    beneficiario_id      INTEGER NOT NULL,
    tipo                 TEXT NOT NULL,        -- 'GES' | 'CAEC'
    version_calculo      INTEGER NOT NULL,
    regla_version_id     INTEGER NOT NULL,     -- LINEAGE
    ventana_inicio       DATE NOT NULL,        -- DERIVADA, puede moverse
    ventana_fin          DATE NOT NULL,
    acumulado            REAL NOT NULL,
    deducible_aplicado   REAL NOT NULL,
    fecha_cruce          DATE,                 -- NULL si no se alcanzó
    calculado_en         TIMESTAMP NOT NULL,
    motivo_recalculo     TEXT NOT NULL,
    vigente              BOOLEAN NOT NULL
);
-- motivo: inicial | datos_tardios | ventana_desplazada
--       | cambio_normativo | correccion

CREATE TABLE calculos_detalle (
    id                   INTEGER PRIMARY KEY,
    calculo_id           INTEGER NOT NULL REFERENCES calculos_resultado(id),
    prestacion_id        INTEGER NOT NULL REFERENCES prestaciones(prestacion_id),
    copago               REAL NOT NULL,
    paga_beneficiario    REAL NOT NULL,
    cubre_seguro         REAL NOT NULL,
    acumulado_hasta_aqui REAL NOT NULL
);
```

---

## 5. La mecánica

### 5.1 GES

- Copago máximo 20% del arancel en isapre.
- Deducible: 29 cotizaciones por evento, tope anual UF 122.
- Con más de un evento en el grupo: 43 cotizaciones, tope UF 181.
- Acumula 12 meses desde el primer copago.
- No cuentan copagos fuera de la Red GES ni prestaciones no GES.

### 5.2 CAEC

- Exclusivo de isapres. Sobre el deducible, cubre 100% del resto.
- Deducible: 30 × cotización pactada, **mínimo 60 UF, máximo 126 UF**.
- Más de un beneficiario o más de una enfermedad: 43 cotizaciones, máximo 181 UF.
- Solo acumula desde que el beneficiario fue derivado y admitido a la Red.
- No cuentan copagos fuera de la red CAEC.
- **UF a usar:** valor al último día del mes anterior a la prestación.

> ⚠️ Los valores exactos cambian. Verificar en superdesalud.gob.cl.
> Lo que importa acá es la mecánica, no memorizar cifras.

### 5.3 La ventana de 12 meses es DERIVADA

Esto es lo central del proyecto y la parte no obvia.

La acumulación corre 12 meses **desde el primer copago**, no por año calendario.
Entonces la fecha de inicio no es un parámetro almacenado: es un valor **derivado
del conjunto de prestaciones conocidas**, y se recalcula cada vez que ese conjunto
cambia.

**Si llega una prestación tardía anterior a la que se creía primera, la ventana se
desplaza hacia atrás.** Eso puede:
- sacar prestaciones que estaban dentro
- meter prestaciones que estaban fuera
- cambiar el acumulado, el punto de cruce del deducible y el split

Por eso `ventana_inicio` / `ventana_fin` se guardan **explícitamente en cada versión
de resultado**: precisamente porque pueden moverse, y el cambio tiene que quedar visible.

En términos bitemporales: la ventana parte con el primer copago en **valid time**,
pero cuál prestación es la primera solo se sabe **a un cierto transaction time**.
Datos tardíos cambian lo segundo sin cambiar lo primero.

**Pregunta abierta (decisión de negocio, no técnica):** ¿la ventana parte del primer
copago *cronológicamente* o del primero *conocido al momento*? Divergen exactamente
en el caso de datos tardíos.

---

## 6. Los dos tipos de recálculo

| | Cambio normativo | Datos tardíos |
|---|---|---|
| Qué cambió | La regla | Los hechos |
| Regla a aplicar | La nueva, según vigencia | **La histórica del período** |
| Mecanismo | Nueva versión SCD2 | Marcar período sucio y recalcular |

> ⚠️ **Bug sutil a evitar:** los datos tardíos deben recalcularse con la **regla
> vigente en ese período**, no con la regla de hoy.

### Cambio normativo retroactivo

Cierro la versión anterior con un `valid_to` y agrego la nueva con su propio
`valid_from`. **Nunca sobrescribo un cálculo ya hecho** — genero una nueva versión
del resultado y dejo registrado qué regla usó cada una.

Como los cambios normativos se anuncian con anticipación, la nueva versión se carga
apenas se anuncia, aunque su vigencia sea futura o retroactiva. El mismo query de
effective-dated lookup detecta automáticamente qué prestaciones quedaron con una
regla desactualizada.

### Límite de alcance

El motor **determina el resultado correcto** y deja trazado por qué cambió: qué
versión de regla aplicó, qué datos vio, y si la ventana se movió. Lo que se hace con
esa diferencia —si hay devolución o cobro— es del negocio, pero el sistema tiene que
hacerla **visible y explicable**.

Pendiente de definir con negocio: ¿hay un cierre de período después del cual no se
recalcula? (Es práctica estándar en contabilidad: las correcciones post-cierre pasan
por un proceso de ajuste, no por reescritura silenciosa.)

---

## 7. Patrones de implementación

### 7.1 Effective-dated lookup

```sql
SELECT p.*, pg.tope_uf, pg.regla_id
FROM prestaciones p
LEFT JOIN parametros_ges pg
    ON p.codigo_ges = pg.codigo_ges
   AND p.fecha_prestacion >= pg.valid_from
   AND p.fecha_prestacion <= COALESCE(pg.valid_to, '9999-12-31')
WHERE p.codigo_ges IS NOT NULL;
```

`COALESCE` en el extremo abierto evita un `CASE` separado.
**`LEFT JOIN` a propósito:** con `INNER JOIN`, una prestación que cae en un hueco de
reglas desaparece silenciosamente. Con `LEFT`, aparece con `tope_uf IS NULL` y un
chequeo de consistencia la detecta.

### 7.2 Acumulado corriente

```sql
SELECT
    beneficiario_id,
    fecha_prestacion,
    monto_copago,
    SUM(monto_copago) OVER (
        PARTITION BY beneficiario_id
        ORDER BY fecha_prestacion
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS acumulado
FROM prestaciones
WHERE en_red_caec = 1
ORDER BY beneficiario_id, fecha_prestacion;
```

⚠️ Sin `ORDER BY` **dentro** del `OVER()` no es acumulado: devuelve el total de la
partición repetido en cada fila. El `ORDER BY` externo solo ordena la visualización.
`ROWS` (no `RANGE`) para que empates de fecha no compartan valor acumulado.

### 7.3 El clamp del deducible

```python
def deducible_caec_uf(cotizacion_pactada_uf, num_eventos=1, params=None):
    """Deducible CAEC en UF. Función pura."""
    if num_eventos < 1:
        raise ValueError("num_eventos debe ser >= 1")

    factor = params.factor_base if num_eventos == 1 else params.factor_multiple
    techo  = params.techo_base_uf if num_eventos == 1 else params.techo_multiple_uf

    bruto = factor * cotizacion_pactada_uf
    return max(params.piso_uf, min(bruto, techo))
```

⚠️ `min(60, x) == 60` **no** es un chequeo de piso: es verdadero siempre que `x >= 60`,
justo cuando el piso NO debe activarse. Y `max(126, x)` como condición es siempre
truthy porque devuelve un número, no un booleano. El clamp correcto es
`max(piso, min(valor, techo))`.

### 7.4 El split en el umbral

```python
def desglose(prestaciones_ordenadas, deducible_clp):
    """Divide cada copago entre beneficiario y seguro. Función pura."""
    acumulado = 0
    filas = []
    for p in prestaciones_ordenadas:
        copago = p["monto_copago"]
        restante = max(0, deducible_clp - acumulado)
        paga_beneficiario = min(copago, restante)
        cubre_seguro = copago - paga_beneficiario
        acumulado += copago
        filas.append({
            "prestacion_id": p["prestacion_id"],
            "fecha": p["fecha_prestacion"],
            "copago": copago,
            "paga_beneficiario": paga_beneficiario,
            "cubre_seguro": cubre_seguro,
            "acumulado": acumulado,
        })
    return filas
```

Sin caso especial para la fila que cruza, sin flag, sin segunda pasada.
Antes del umbral `min` devuelve el copago completo; después, `restante` es 0.

### 7.5 Derivar la ventana

```python
def derivar_ventana(prestaciones_calificadas):
    """La ventana parte con el primer copago conocido. Puede moverse
    si después llega una prestación anterior."""
    if not prestaciones_calificadas:
        return None
    inicio = min(p["fecha_prestacion"] for p in prestaciones_calificadas)
    fin = inicio + relativedelta(months=12) - timedelta(days=1)
    return inicio, fin
```

---

## 8. Validación y testing

### 8.1 Validación al cargar reglas

Se valida el *rule set* al cargarlo, no al usarlo. Detectar un set inconsistente en
la carga es mucho mejor que descubrirlo tres meses después en un cálculo mal hecho.

- Sin solapamiento de `valid_from`/`valid_to` para el mismo código
- Sin huecos entre versiones consecutivas
- `valid_from < valid_to` cuando `valid_to` no es NULL
- Campos obligatorios presentes
- Exactamente una versión vigente (`valid_to IS NULL`) por código

*(Analogía: un motor SQL rechaza una query malformada antes de ejecutar nada.)*

### 8.2 Tests

```python
# Valor conocido — atrapa el bug del clamp
def test_deducible_base():
    assert deducible_caec_uf(3.5, params=P) == 105

# Bordes del clamp
def test_deducible_piso():  assert deducible_caec_uf(1.0, params=P) == 60
def test_deducible_techo(): assert deducible_caec_uf(10.0, params=P) == 126

# Invariantes — atrapan bugs no anticipados
def test_desglose_suma_al_copago():
    for f in desglose(PRESTACIONES, 4_095_000):
        assert f["paga_beneficiario"] + f["cubre_seguro"] == f["copago"]

def test_nunca_negativo():
    for f in desglose(PRESTACIONES, 4_095_000):
        assert f["paga_beneficiario"] >= 0 and f["cubre_seguro"] >= 0

# Fronteras de vigencia — el más relevante al dominio
def test_frontera_exacta_del_cambio():
    assert regla_vigente("GES01", date(2025,12,31), REGLAS).tope_uf == 122
    assert regla_vigente("GES01", date(2026,1,1),  REGLAS).tope_uf == 130

# Idempotencia — recalcular sin cambios da idéntico resultado
def test_recalculo_idempotente():
    assert calcular(DATOS, REGLAS) == calcular(DATOS, REGLAS)

# Dato tardío usa la regla histórica, no la de hoy
def test_dato_tardio_usa_regla_del_periodo():
    r = calcular_periodo(DATOS_CON_TARDIO, periodo="2025-06", reglas=REGLAS)
    assert r.regla_version_id == REGLA_2025.regla_id

# La ventana se desplaza cuando llega una prestación anterior
def test_ventana_se_desplaza_con_dato_anterior():
    v1 = derivar_ventana(PRESTACIONES)
    v2 = derivar_ventana(PRESTACIONES + [PRESTACION_ANTERIOR])
    assert v2[0] < v1[0]
```

Un test de regresión no es un tipo aparte: es cualquier test que se conserva y
re-ejecuta para detectar que un cambio rompió algo que antes funcionaba.

`pytest` autodescubre `test_*.py` y funciones `test_*`.

### 8.3 Chequeos de consistencia sobre datos

Distinto de testear código. Se corren sobre los datos reales en cada ejecución:
montos no negativos, copago ≤ arancel, sin `codigo_ges` huérfano, sin prestación sin
regla aplicable (el caso `LEFT JOIN` + `NULL`), reconciliación de totales.

---

## 9. Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY sql/ ./sql/
ENTRYPOINT ["python", "-m", "src.motor"]
```

```
# requirements.txt — versiones fijas, no rangos
pandas==2.2.0
openpyxl==3.1.2
python-dateutil==2.8.2
pytest==8.0.0
```

```bash
docker build -t motor-ges-caec .
docker run --rm -v $(pwd)/data:/app/data motor-ges-caec --periodo 2026-03
```

`COPY requirements.txt` antes que `COPY src/` para que la instalación quede cacheada
y no se re-ejecute en cada cambio de código.

**Docker acá es por reproducibilidad, no por escalamiento.** Un cálculo regulado tiene
que dar el mismo número en dos años; un upgrade de pandas que cambie un redondeo es
exactamente el tipo de deriva silenciosa que rompe una auditoría.

---

## 10. Plan de construcción

**Fase 1 — corte vertical delgado.** Un problema GES, un beneficiario, punta a punta:
ingesta → reglas versionadas → cálculo → resultado auditable. Simplificado pero
completo. Valida la arquitectura antes de invertir en amplitud.

**Fase 2 — CAEC.** Deducible con clamp, filtro de red, fecha de derivación, split.

**Fase 3 — la ventana derivada.** Derivación desde el primer copago, y el caso de
desplazamiento por dato tardío. Es la parte más interesante del proyecto.

**Fase 4 — recálculo.** Los dos tipos, con `motivo_recalculo` y versionado de resultados.

**Fase 5 — ingesta desde Excel.** Formatos heterogéneos → normalización → carga.

**Fase 6 — CI.** GitHub Actions corriendo pytest en cada push.

---

## 11. Alcance y límites

Qué **no** es este proyecto:
- No es una implementación normativa correcta. Las reglas están simplificadas.
- No es arquitectura empresarial. Es escala de proyecto.
- No usa datos reales. Todo sintético.
- No cubre la decisión de negocio sobre devoluciones/cobros tras un recálculo.

Qué sí demuestra:
- Modelamiento bitemporal y versionado SCD2 de reglas
- Effective-dated lookups y su testeo en las fronteras
- Separación funcional pura / I/O en los bordes
- Tests de regresión, invariantes e idempotencia
- Manejo de datos tardíos y ventanas derivadas
- Reproducibilidad vía contenedor con dependencias fijas
