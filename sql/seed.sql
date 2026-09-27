-- Motor GES/CAEC lite — synthetic seed data. NOT real values.
--
-- Load on top of schema.sql. IDs are explicit so tests can refer to them.
-- The comments on each prestación say which case it exists to exercise.

-- ───────────────────────────────────────────────────────────
-- RULES
-- ───────────────────────────────────────────────────────────
-- GES01 changes on 2026-01-01 (tope UF 122 → 130). valid_to is inclusive (§7.1).
INSERT INTO parametros_ges
    (regla_id, codigo_ges, valid_from, valid_to, tope_cotizaciones, tope_uf,
     fuente, cargado_por, cargado_en)
VALUES
    (1, 'GES01', '2025-01-01', '2025-12-31', 29, 122.0,
     'Decreto sintético 2025', 'seed', '2024-12-15T09:00:00'),
    (2, 'GES01', '2026-01-01', NULL,         29, 130.0,
     'Decreto sintético 2026', 'seed', '2025-11-15T09:00:00');

INSERT INTO parametros_caec
    (regla_id, valid_from, valid_to, factor_base, factor_multiple,
     piso_uf, techo_base_uf, techo_multiple_uf, fuente, cargado_por, cargado_en)
VALUES
    (1, '2025-01-01', NULL, 30, 43, 60.0, 126.0, 181.0,
     'Condiciones CAEC sintéticas', 'seed', '2024-12-15T09:00:00');

-- Month-end UF, 2024-12 → 2026-12 (+0.3%/month). 2024-12-31 is included because
-- the CAEC deductible uses the UF of the month before ventana_inicio (plan D3).
INSERT INTO valores_uf (fecha, valor_clp) VALUES
    ('2024-12-31', 38400.00),
    ('2025-01-31', 38515.20),
    ('2025-02-28', 38630.75),
    ('2025-03-31', 38746.64),
    ('2025-04-30', 38862.88),
    ('2025-05-31', 38979.47),
    ('2025-06-30', 39096.40),
    ('2025-07-31', 39213.69),
    ('2025-08-31', 39331.34),
    ('2025-09-30', 39449.33),
    ('2025-10-31', 39567.68),
    ('2025-11-30', 39686.38),
    ('2025-12-31', 39805.44),
    ('2026-01-31', 39924.86),
    ('2026-02-28', 40044.63),
    ('2026-03-31', 40164.76),
    ('2026-04-30', 40285.26),
    ('2026-05-31', 40406.11),
    ('2026-06-30', 40527.33),
    ('2026-07-31', 40648.91),
    ('2026-08-31', 40770.86),
    ('2026-09-30', 40893.17),
    ('2026-10-31', 41015.85),
    ('2026-11-30', 41138.90),
    ('2026-12-31', 41262.32);

-- ───────────────────────────────────────────────────────────
-- BENEFICIARIES
-- ───────────────────────────────────────────────────────────
-- 1: GES. 29 × 5.0 = 145 UF, so the tope (122 / 130) binds and the rule change matters.
-- 2: CAEC, referred 2025-04-01. 30 × 3.5 = 105 UF, inside the [60, 126] clamp.
INSERT INTO beneficiarios (beneficiario_id, cotizacion_pactada_uf, fecha_derivacion_caec)
VALUES
    (1, 5.0, NULL),
    (2, 3.5, '2025-04-01');

-- ───────────────────────────────────────────────────────────
-- PRESTACIONES
-- ───────────────────────────────────────────────────────────
-- Beneficiary 1 (GES01). Qualifying copagos start 2025-03-10, so the derived
-- window is 2025-03-10 → 2026-03-09 and rule 1 (122 UF) applies (plan D1).
-- Deductible = 122 UF × UF(2025-02-28) = 122 × 38 630.75 ≈ 4.71M CLP.
-- Counting copagos: 1.0M, 2.2M, 3.7M, 4.5M (after 6), then:
--   known as of today      → +0.8M (7) = 5.3M, crossing on 2025-11-20 (prestación 7)
--   known before 2026-03-05 → +0.6M (8) = 5.1M, crossing on 2026-02-10 (prestación 8)
-- So the late arrival moves the crossing date.
INSERT INTO prestaciones
    (prestacion_id, beneficiario_id, fecha_prestacion, recibido_en, codigo_ges,
     en_red_ges, en_red_caec, monto_arancel, monto_copago)
VALUES
    -- in window, counts
    (1,  1, '2025-03-10', '2025-03-12T10:00:00', 'GES01', 1, 0, 5000000, 1000000),
    (2,  1, '2025-05-20', '2025-05-22T10:00:00', 'GES01', 1, 0, 6000000, 1200000),
    -- outside the GES network: excluded (§5.1)
    (3,  1, '2025-06-01', '2025-06-03T10:00:00', 'GES01', 0, 0, 2000000,  400000),
    -- not GES: excluded (§5.1)
    (4,  1, '2025-07-01', '2025-07-02T10:00:00', NULL,    1, 0, 1000000,  300000),
    -- in window, counts
    (5,  1, '2025-08-05', '2025-08-07T10:00:00', 'GES01', 1, 0, 7500000, 1500000),
    (6,  1, '2025-10-15', '2025-10-16T10:00:00', 'GES01', 1, 0, 4000000,  800000),
    -- LATE ARRIVAL: happened 2025-11-20, known only 2026-03-05 (plan D5).
    -- Inside the window, so it must use the 2025 rule, not today's (§6).
    (7,  1, '2025-11-20', '2026-03-05T10:00:00', 'GES01', 1, 0, 4000000,  800000),
    -- in window under the 2026 rule's validity, but the window's rule is still rule 1 (D1)
    (8,  1, '2026-02-10', '2026-02-11T10:00:00', 'GES01', 1, 0, 3000000,  600000),
    -- after ventana_fin (2026-03-09): excluded
    (9,  1, '2026-04-10', '2026-04-11T10:00:00', 'GES01', 1, 0, 2000000,  400000);

-- Beneficiary 2 (CAEC, referred 2025-04-01). Qualifying copagos start 2025-04-10,
-- so the window is 2025-04-10 → 2026-04-09 and the UF used is 2025-03-31 (D3).
-- Deductible = 105 × 38 746.64 ≈ 4.07M CLP. Copagos: 2.0M, then 4.5M → crossing at prestación 13.
INSERT INTO prestaciones
    (prestacion_id, beneficiario_id, fecha_prestacion, recibido_en, codigo_ges,
     en_red_ges, en_red_caec, monto_arancel, monto_copago)
VALUES
    -- before the referral date: excluded (§5.2)
    (10, 2, '2025-03-15', '2025-03-17T10:00:00', NULL, 0, 1,  3000000,  900000),
    -- counts
    (11, 2, '2025-04-10', '2025-04-11T10:00:00', NULL, 0, 1,  8000000, 2000000),
    -- outside the CAEC network: excluded (§5.2)
    (12, 2, '2025-06-20', '2025-06-21T10:00:00', NULL, 0, 0,  2000000,  600000),
    -- counts; accumulated 4.5M crosses the deductible
    (13, 2, '2025-09-01', '2025-09-02T10:00:00', NULL, 0, 1, 10000000, 2500000),
    -- counts; fully covered by the insurer
    (14, 2, '2026-01-15', '2026-01-16T10:00:00', NULL, 0, 1,  4000000, 1000000);
