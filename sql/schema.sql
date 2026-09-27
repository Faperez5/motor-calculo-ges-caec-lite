-- Motor GES/CAEC lite — schema (spec §4).
--
-- Conventions:
--   * Dates are ISO text 'YYYY-MM-DD'; timestamps are ISO text 'YYYY-MM-DDTHH:MM:SS'.
--   * CLP amounts are INTEGER pesos; UF amounts are REAL (plan D4).
--   * foreign_keys is a per-connection setting: extractor.conectar() must turn
--     it on too. The PRAGMA here only covers the session that runs this file.

PRAGMA foreign_keys = ON;

-- ───────────────────────────────────────────────────────────
-- RULES: SCD2. What the regulation said, and since when.
-- Overlaps and gaps are checked by calculo.validar_reglas (§8.1);
-- the indexes below only guarantee one open version.
-- ───────────────────────────────────────────────────────────
CREATE TABLE parametros_ges (
    regla_id          INTEGER PRIMARY KEY,
    codigo_ges        TEXT    NOT NULL,
    valid_from        DATE    NOT NULL,
    valid_to          DATE,                    -- NULL = in force
    tope_cotizaciones INTEGER NOT NULL CHECK (tope_cotizaciones > 0),
    tope_uf           REAL    NOT NULL CHECK (tope_uf > 0),
    fuente            TEXT,                    -- which decree
    cargado_por       TEXT,
    cargado_en        TIMESTAMP NOT NULL,
    CHECK (valid_to IS NULL OR valid_from < valid_to)
);

CREATE UNIQUE INDEX ux_parametros_ges_abierta
    ON parametros_ges (codigo_ges) WHERE valid_to IS NULL;

CREATE TABLE parametros_caec (
    regla_id          INTEGER PRIMARY KEY,
    valid_from        DATE    NOT NULL,
    valid_to          DATE,
    factor_base       INTEGER NOT NULL CHECK (factor_base > 0),        -- 30
    factor_multiple   INTEGER NOT NULL CHECK (factor_multiple > 0),    -- 43
    piso_uf           REAL    NOT NULL CHECK (piso_uf > 0),            -- 60
    techo_base_uf     REAL    NOT NULL,                                -- 126
    techo_multiple_uf REAL    NOT NULL,                                -- 181
    fuente            TEXT,
    cargado_por       TEXT,
    cargado_en        TIMESTAMP NOT NULL,
    CHECK (valid_to IS NULL OR valid_from < valid_to),
    CHECK (piso_uf <= techo_base_uf AND techo_base_uf <= techo_multiple_uf)
);

CREATE UNIQUE INDEX ux_parametros_caec_abierta
    ON parametros_caec ((valid_to IS NULL)) WHERE valid_to IS NULL;

-- UF value per date. Looked up by exact date; a missing date is an error (plan S07).
CREATE TABLE valores_uf (
    fecha             DATE PRIMARY KEY,
    valor_clp         REAL NOT NULL CHECK (valor_clp > 0)
);

-- ───────────────────────────────────────────────────────────
-- FACTS: append-only. A row is never updated.
-- ───────────────────────────────────────────────────────────
CREATE TABLE beneficiarios (
    beneficiario_id       INTEGER PRIMARY KEY,
    cotizacion_pactada_uf REAL NOT NULL CHECK (cotizacion_pactada_uf > 0),
    fecha_derivacion_caec DATE                 -- NULL = not referred
);

CREATE TABLE prestaciones (
    prestacion_id     INTEGER PRIMARY KEY,
    beneficiario_id   INTEGER NOT NULL REFERENCES beneficiarios (beneficiario_id),
    fecha_prestacion  DATE      NOT NULL,      -- VALID TIME
    recibido_en       TIMESTAMP NOT NULL,      -- TRANSACTION TIME
    codigo_ges        TEXT,                    -- NULL = not GES
    en_red_ges        BOOLEAN NOT NULL DEFAULT 1 CHECK (en_red_ges IN (0, 1)),   -- plan D6
    en_red_caec       BOOLEAN NOT NULL CHECK (en_red_caec IN (0, 1)),
    monto_arancel     INTEGER NOT NULL CHECK (monto_arancel >= 0),
    monto_copago      INTEGER NOT NULL CHECK (monto_copago >= 0)
);
-- fecha_prestacion != recibido_en IS the late-data signal.
-- copago <= arancel is a data check (calculo.chequear_datos, §8.3), not a constraint,
-- so bad rows get reported instead of blocking a whole load.

CREATE INDEX ix_prestaciones_beneficiario_fecha
    ON prestaciones (beneficiario_id, fecha_prestacion);

-- ───────────────────────────────────────────────────────────
-- RESULTS: versioned. A calculation is never overwritten.
-- ───────────────────────────────────────────────────────────
CREATE TABLE calculos_resultado (
    id                   INTEGER PRIMARY KEY,
    beneficiario_id      INTEGER NOT NULL REFERENCES beneficiarios (beneficiario_id),
    tipo                 TEXT    NOT NULL CHECK (tipo IN ('GES', 'CAEC')),
    version_calculo      INTEGER NOT NULL CHECK (version_calculo >= 1),
    regla_version_id     INTEGER NOT NULL,     -- LINEAGE: parametros_ges or _caec, per tipo
    ventana_inicio       DATE    NOT NULL,     -- DERIVED, can move
    ventana_fin          DATE    NOT NULL,
    acumulado            INTEGER NOT NULL,     -- CLP
    deducible_aplicado   INTEGER NOT NULL,     -- CLP
    fecha_cruce          DATE,                 -- NULL = deductible not reached
    calculado_en         TIMESTAMP NOT NULL,
    conocido_hasta       TIMESTAMP NOT NULL,   -- transaction-time cut the run used (D5)
    version_codigo       TEXT    NOT NULL,     -- git commit that ran; '-dirty' = uncommitted changes
    motivo_recalculo     TEXT    NOT NULL CHECK (motivo_recalculo IN (
                             'inicial', 'datos_tardios', 'ventana_desplazada',
                             'cambio_normativo', 'correccion')),
    vigente              BOOLEAN NOT NULL CHECK (vigente IN (0, 1)),
    CHECK (ventana_inicio <= ventana_fin),
    CHECK (conocido_hasta <= calculado_en),    -- a run can't know the future
    UNIQUE (beneficiario_id, tipo, version_calculo)
);

-- At most one current result per beneficiary and type.
CREATE UNIQUE INDEX ux_calculos_resultado_vigente
    ON calculos_resultado (beneficiario_id, tipo) WHERE vigente = 1;

CREATE TABLE calculos_detalle (
    id                   INTEGER PRIMARY KEY,
    calculo_id           INTEGER NOT NULL REFERENCES calculos_resultado (id),
    prestacion_id        INTEGER NOT NULL REFERENCES prestaciones (prestacion_id),
    copago               INTEGER NOT NULL,
    paga_beneficiario    INTEGER NOT NULL CHECK (paga_beneficiario >= 0),
    cubre_seguro         INTEGER NOT NULL CHECK (cubre_seguro >= 0),
    acumulado_hasta_aqui INTEGER NOT NULL,
    CHECK (paga_beneficiario + cubre_seguro = copago)
);
