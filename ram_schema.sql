-- ═══════════════════════════════════════════════════════════════════════════
-- ram_schema.sql — RAM SNIPER : schéma SQLite (module DDR4 UDIMM desktop)
-- ═══════════════════════════════════════════════════════════════════════════
-- Toutes les tables sont préfixées `ram_` : elles cohabitent dans le MÊME
-- fichier soldier.db que les tables historiques (listings/purchases/builds/
-- sales/kv_settings) sans jamais entrer en collision.
--
-- Le périmètre métier (DDR4 UDIMM desktop non-ECC uniquement) est verrouillé
-- au niveau du SCHÉMA, pas seulement dans le code : les CHECK sur
-- ram_reference.generation / form_factor / ecc rendent physiquement impossible
-- l'insertion d'une SO-DIMM, d'une DDR3 ou d'une barrette ECC dans la base de
-- référence. Une régression de code ne peut pas polluer le socle de scoring.
--
-- Convention de dates :
--   *_le / *_date en REAL  = timestamp Unix (time.time()), comme soldier_db
--   prix_ref_maj_le        = TEXT 'YYYY-MM-DD' (date de calibrage, lisible)
-- Convention booléens : INTEGER 0/1 avec CHECK.
-- Convention JSON : TEXT contenant du JSON (listes de photos, drapeaux…).
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- 1. ram_reference — LE SOCLE
-- ───────────────────────────────────────────────────────────────────────────
-- Sans elle aucun score n'a de sens : c'est elle qui dit combien vaut une
-- barrette à la revente et à quelle vitesse elle tourne. Pré-remplie par
-- ram_reference_data.py (80+ références réelles, part numbers exacts),
-- recalibrée par ram_calibration.py.
CREATE TABLE IF NOT EXISTS ram_reference (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number           TEXT    NOT NULL UNIQUE,   -- PN constructeur exact (CMK32GX4M2E3200C16)
    pn_normalise          TEXT    NOT NULL,          -- majuscules, sans - _ . / espaces → matching
    marque                TEXT    NOT NULL,
    gamme                 TEXT    NOT NULL,          -- "Vengeance LPX", "Ripjaws V"…
    alias                 TEXT    NOT NULL DEFAULT '[]',  -- JSON: formulations vendeurs

    -- Caractéristiques physiques
    generation            INTEGER NOT NULL DEFAULT 4  CHECK (generation = 4),
    form_factor           TEXT    NOT NULL DEFAULT 'UDIMM' CHECK (form_factor = 'UDIMM'),
    ecc                   INTEGER NOT NULL DEFAULT 0  CHECK (ecc = 0),
    capacite_module_go    INTEGER NOT NULL CHECK (capacite_module_go IN (4, 8, 16, 32)),
    nb_modules            INTEGER NOT NULL DEFAULT 1  CHECK (nb_modules BETWEEN 1 AND 8),
    capacite_totale_go    INTEGER NOT NULL
                          CHECK (capacite_totale_go = capacite_module_go * nb_modules),
    frequence_mhz         INTEGER NOT NULL CHECK (frequence_mhz >= 2133),
    cas_latency           INTEGER,
    voltage               REAL,
    "rank"                TEXT,        -- '1Rx8' | '2Rx8'
    die_type              TEXT,        -- 'Samsung B-die', 'Micron E-die', 'Hynix CJR'…
    rgb                   INTEGER NOT NULL DEFAULT 0 CHECK (rgb IN (0, 1)),
    couleur               TEXT,
    hauteur_mm            REAL,
    low_profile           INTEGER NOT NULL DEFAULT 0 CHECK (low_profile IN (0, 1)),

    -- Valeur marché
    tier                  TEXT    NOT NULL CHECK (tier IN ('S', 'A', 'B', 'C', 'D')),
    prix_ref_occasion_eur REAL    NOT NULL CHECK (prix_ref_occasion_eur > 0),
    prix_ref_maj_le       TEXT,        -- 'YYYY-MM-DD' — alerte dashboard si > 14 jours
    prix_ref_source       TEXT    NOT NULL DEFAULT 'seed'
                          CHECK (prix_ref_source IN ('seed', 'vinted_vendu', 'ebay_termine',
                                                     'leboncoin', 'manuel')),
    prix_ref_n_ventes     INTEGER NOT NULL DEFAULT 0,   -- taille d'échantillon du calibrage
    liquidite             INTEGER NOT NULL CHECK (liquidite BETWEEN 1 AND 5),
    delai_rotation_jours  INTEGER,

    -- Exploitation
    pn_verifie            INTEGER NOT NULL DEFAULT 1 CHECK (pn_verifie IN (0, 1)),
    actif                 INTEGER NOT NULL DEFAULT 1 CHECK (actif IN (0, 1)),
    notes                 TEXT,
    cree_le               REAL    NOT NULL,
    maj_le                REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ram_ref_pn      ON ram_reference(pn_normalise);
CREATE INDEX IF NOT EXISTS idx_ram_ref_tier    ON ram_reference(tier, liquidite);
CREATE INDEX IF NOT EXISTS idx_ram_ref_specs   ON ram_reference(capacite_module_go, nb_modules,
                                                                frequence_mhz, cas_latency);
CREATE INDEX IF NOT EXISTS idx_ram_ref_calib   ON ram_reference(prix_ref_maj_le);
CREATE INDEX IF NOT EXISTS idx_ram_ref_marque  ON ram_reference(marque, gamme);


-- ───────────────────────────────────────────────────────────────────────────
-- 2. ram_annonce — le flux scrapé
-- ───────────────────────────────────────────────────────────────────────────
-- Toute annonce vue, même rejetée : c'est la matière première du --replay et
-- du réglage du scoring dans le temps. Rien n'est jamais supprimé.
CREATE TABLE IF NOT EXISTS ram_annonce (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT    NOT NULL CHECK (source IN ('vinted', 'leboncoin', 'ebay',
                                                           'manuel', 'replay')),
    source_id           TEXT,
    url                 TEXT    NOT NULL UNIQUE,
    titre               TEXT    NOT NULL,
    description         TEXT,
    mot_cle             TEXT,           -- requête qui a fait remonter l'annonce

    -- Prix : JAMAIS scorer sur prix_affiche seul → prix_total fait foi
    prix_affiche        REAL    NOT NULL,
    frais_port          REAL    NOT NULL DEFAULT 0,
    frais_protection    REAL    NOT NULL DEFAULT 0,
    prix_total          REAL    NOT NULL,
    main_propre         INTEGER NOT NULL DEFAULT 0 CHECK (main_propre IN (0, 1)),

    -- Vendeur / logistique
    vendeur_pseudo      TEXT,
    vendeur_note        REAL,
    vendeur_ventes      INTEGER,
    localisation        TEXT,
    code_postal         TEXT,
    departement         TEXT,

    -- Photos
    photos              TEXT    NOT NULL DEFAULT '[]',   -- JSON: liste d'URLs
    nb_photos           INTEGER NOT NULL DEFAULT 0,
    photos_hash         TEXT,                            -- sha1 des URLs → clé de cache vision

    publie_le           REAL,
    detecte_le          REAL    NOT NULL,
    maj_le              REAL,
    vue_le              REAL,           -- dernière fois revue dans un scan (encore en ligne ?)
    encore_en_ligne     INTEGER NOT NULL DEFAULT 1 CHECK (encore_en_ligne IN (0, 1)),

    -- Identification texte (étape 1)
    ref_id              INTEGER REFERENCES ram_reference(id) ON DELETE SET NULL,
    pn_detecte          TEXT,
    pn_normalise        TEXT,
    capacite_module_go  INTEGER,
    nb_modules          INTEGER,
    capacite_totale_go  INTEGER,
    frequence_mhz       INTEGER,
    cas_latency         INTEGER,
    marque_detectee     TEXT,
    gamme_detectee      TEXT,
    tier                TEXT,
    est_kit             INTEGER NOT NULL DEFAULT 0 CHECK (est_kit IN (0, 1)),
    confiance_texte     REAL,           -- 0-1 : à quel point l'identification texte tient

    -- Scoring
    pre_score           REAL,
    score_final         REAL,
    revente_estimee     REAL,
    marge_estimee       REAL,
    marge_pct           REAL,
    marge_reelle        REAL,
    marge_reelle_pct    REAL,
    qualite_annonce     REAL,
    score_vendeur       REAL,
    score_logistique    REAL,

    -- Verdicts
    statut_verif        TEXT NOT NULL DEFAULT 'non_verifie'
                        CHECK (statut_verif IN ('non_verifie', 'confirme', 'probable',
                                                'a_verifier', 'rejete', 'quota_epuise')),
    statut              TEXT NOT NULL DEFAULT 'nouveau'
                        CHECK (statut IN ('nouveau', 'notifie', 'ignore', 'achete',
                                          'archive', 'rejete')),
    exclusion           TEXT,           -- 'sodimm'|'ecc'|'ddr3'|'ddr5'|'capacite'|'marge'…
    rejet_motif         TEXT,
    drapeaux            TEXT NOT NULL DEFAULT '[]',   -- JSON
    brut                TEXT            -- payload d'origine (JSON) pour --replay
);

CREATE INDEX IF NOT EXISTS idx_ram_annonce_detecte  ON ram_annonce(detecte_le DESC);
CREATE INDEX IF NOT EXISTS idx_ram_annonce_statut   ON ram_annonce(statut, statut_verif);
CREATE INDEX IF NOT EXISTS idx_ram_annonce_score    ON ram_annonce(pre_score DESC);
CREATE INDEX IF NOT EXISTS idx_ram_annonce_pn       ON ram_annonce(pn_normalise);
CREATE INDEX IF NOT EXISTS idx_ram_annonce_ref      ON ram_annonce(ref_id);
CREATE INDEX IF NOT EXISTS idx_ram_annonce_source   ON ram_annonce(source, source_id);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. ram_vision_analyse — cache des analyses Gemini
-- ───────────────────────────────────────────────────────────────────────────
-- cache_cle = sha1(url annonce + hash des URLs photos) : une annonce dont les
-- photos n'ont pas bougé n'est JAMAIS réanalysée (le quota gratuit est rare).
CREATE TABLE IF NOT EXISTS ram_vision_analyse (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    annonce_id            INTEGER REFERENCES ram_annonce(id) ON DELETE CASCADE,
    cache_cle             TEXT    NOT NULL UNIQUE,
    provider              TEXT    NOT NULL DEFAULT 'gemini',
    modele                TEXT,
    statut                TEXT    NOT NULL CHECK (statut IN ('ok', 'parse_erreur', 'echec',
                                                             'quota', 'photos_absentes')),

    -- Champs du JSON strict renvoyé par le modèle
    est_ddr4_desktop      INTEGER,
    generation_suspectee  TEXT,
    est_sodimm            INTEGER,
    est_ecc               INTEGER,
    est_registered        INTEGER,
    part_number_lu        TEXT,
    pn_normalise          TEXT,
    marque                TEXT,
    nb_barrettes_visibles INTEGER,
    capacite_par_barrette TEXT,
    nb_puces_par_face     INTEGER,
    code_semaine          TEXT,
    sticker_authentique   INTEGER,
    etat_contacts         TEXT,
    etat_dissipateur      TEXT,
    rgb                   INTEGER,
    couleur               TEXT,
    hauteur_estimee       TEXT,
    photo_lisible         INTEGER,
    drapeaux              TEXT    NOT NULL DEFAULT '[]',
    confiance             REAL,

    -- Télémétrie
    photos_envoyees       INTEGER NOT NULL DEFAULT 0,
    latence_ms            INTEGER,
    reponse_brute         TEXT,
    erreur                TEXT,
    cree_le               REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ram_vision_annonce ON ram_vision_analyse(annonce_id);
CREATE INDEX IF NOT EXISTS idx_ram_vision_date    ON ram_vision_analyse(cree_le DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 4. ram_vision_file — file de priorité du worker vision
-- ───────────────────────────────────────────────────────────────────────────
-- Une seule ligne par annonce (UNIQUE). `differe` = quota épuisé : l'annonce
-- repasse automatiquement en 'en_attente' à la réinitialisation du quota si
-- elle est encore en ligne. Rien n'est jamais perdu.
CREATE TABLE IF NOT EXISTS ram_vision_file (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    annonce_id      INTEGER NOT NULL UNIQUE REFERENCES ram_annonce(id) ON DELETE CASCADE,
    priorite        REAL    NOT NULL DEFAULT 0,   -- = pre_score, tri décroissant
    statut          TEXT    NOT NULL DEFAULT 'en_attente'
                    CHECK (statut IN ('en_attente', 'en_cours', 'fait', 'echec',
                                      'differe', 'abandonne')),
    tentatives      INTEGER NOT NULL DEFAULT 0,
    derniere_erreur TEXT,
    cree_le         REAL    NOT NULL,
    pris_le         REAL,
    traite_le       REAL
);

CREATE INDEX IF NOT EXISTS idx_ram_file_ordre ON ram_vision_file(statut, priorite DESC, cree_le);


-- ───────────────────────────────────────────────────────────────────────────
-- 5. ram_vision_quota — compteurs de quota persistés (minute + jour)
-- ───────────────────────────────────────────────────────────────────────────
-- Persisté en base : un redémarrage du worker ne remet pas les compteurs à
-- zéro et ne fait donc pas cramer le quota journalier.
CREATE TABLE IF NOT EXISTS ram_vision_quota (
    provider  TEXT    NOT NULL DEFAULT 'gemini',
    portee    TEXT    NOT NULL CHECK (portee IN ('minute', 'jour')),
    fenetre   TEXT    NOT NULL,        -- '2026-08-08T14:23' | '2026-08-08'
    compteur  INTEGER NOT NULL DEFAULT 0,
    plafond   INTEGER,                 -- copie du plafond YAML au moment du comptage
    maj_le    REAL    NOT NULL,
    PRIMARY KEY (provider, portee, fenetre)
);


-- ───────────────────────────────────────────────────────────────────────────
-- 6. ram_kit — kits assortis (barrettes appariées)
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ram_kit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nom                 TEXT,
    part_number         TEXT,
    pn_normalise        TEXT,
    nb_modules          INTEGER NOT NULL DEFAULT 2,
    capacite_module_go  INTEGER,
    capacite_totale_go  INTEGER,
    frequence_mhz       INTEGER,
    cas_latency         INTEGER,
    meme_batch          INTEGER NOT NULL DEFAULT 0 CHECK (meme_batch IN (0, 1)),
    code_semaine        TEXT,
    qualite             TEXT CHECK (qualite IN ('parfait', 'batch_different', 'heterogene')),
    prix_revient_total  REAL,
    prix_cible          REAL,
    statut              TEXT NOT NULL DEFAULT 'assemble'
                        CHECK (statut IN ('assemble', 'en_test', 'liste', 'vendu', 'defait')),
    prix_vente          REAL,
    marge_nette         REAL,
    cree_le             REAL NOT NULL,
    vendu_le            REAL,
    notes               TEXT
);


-- ───────────────────────────────────────────────────────────────────────────
-- 7. ram_stock — inventaire, barrette par barrette
-- ───────────────────────────────────────────────────────────────────────────
-- UNE LIGNE = UNE BARRETTE. C'est ce qui rend l'appariement possible : un kit
-- 2×16 acheté d'un bloc crée deux lignes reliées au même ram_kit.
CREATE TABLE IF NOT EXISTS ram_stock (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    annonce_id           INTEGER REFERENCES ram_annonce(id)  ON DELETE SET NULL,
    ref_id               INTEGER REFERENCES ram_reference(id) ON DELETE SET NULL,
    kit_id               INTEGER REFERENCES ram_kit(id)       ON DELETE SET NULL,

    part_number          TEXT,
    pn_normalise         TEXT,
    marque               TEXT,
    gamme                TEXT,
    capacite_module_go   INTEGER NOT NULL,
    frequence_mhz        INTEGER,
    cas_latency          INTEGER,
    "rank"               TEXT,
    rgb                  INTEGER DEFAULT 0,
    couleur              TEXT,
    code_semaine         TEXT,           -- sticker : batch → kit parfait si identique
    numero_serie         TEXT,

    -- Coûts
    prix_achat           REAL NOT NULL,
    frais_port           REAL NOT NULL DEFAULT 0,
    frais_protection     REAL NOT NULL DEFAULT 0,
    prix_revient         REAL,
    source               TEXT,

    statut               TEXT NOT NULL DEFAULT 'commande'
                         CHECK (statut IN ('commande', 'recu', 'en_test', 'teste_ok',
                                           'teste_hs', 'apparie', 'liste', 'vendu',
                                           'retourne')),

    -- Banc de test
    test_date            REAL,
    test_banc            TEXT,
    memtest_passes       INTEGER,
    memtest_ok           INTEGER CHECK (memtest_ok IN (0, 1)),
    memtest_screenshot   TEXT,
    xmp_stable           INTEGER CHECK (xmp_stable IN (0, 1)),
    frequence_max_stable INTEGER,
    test_notes           TEXT,

    -- Vente
    plateforme_vente     TEXT,
    prix_vente           REAL,
    frais_vente          REAL,
    marge_nette          REAL,

    achete_le            REAL,
    recu_le              REAL,
    liste_le             REAL,
    vendu_le             REAL,
    delai_rotation_jours REAL,          -- vendu_le - achete_le : LE KPI décisif

    notes                TEXT,
    cree_le              REAL NOT NULL,
    maj_le               REAL
);

CREATE INDEX IF NOT EXISTS idx_ram_stock_statut ON ram_stock(statut);
CREATE INDEX IF NOT EXISTS idx_ram_stock_pn     ON ram_stock(pn_normalise);
CREATE INDEX IF NOT EXISTS idx_ram_stock_kit    ON ram_stock(kit_id);


-- ───────────────────────────────────────────────────────────────────────────
-- 8. ram_appariement — radar kits (l'arbitrage principal)
-- ───────────────────────────────────────────────────────────────────────────
-- Une barrette unitaire en stock + une annonce au même PN = candidat.
CREATE TABLE IF NOT EXISTS ram_appariement (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id           INTEGER NOT NULL REFERENCES ram_stock(id)   ON DELETE CASCADE,
    annonce_id         INTEGER NOT NULL REFERENCES ram_annonce(id) ON DELETE CASCADE,
    part_number        TEXT,
    type_appariement   TEXT NOT NULL CHECK (type_appariement IN ('parfait', 'batch_different',
                                                                 'specs_seules')),
    meme_code_semaine  INTEGER NOT NULL DEFAULT 0 CHECK (meme_code_semaine IN (0, 1)),
    prix_cible         REAL,             -- prix max à payer pour que le kit reste rentable
    prix_kit_revient   REAL,
    prix_kit_revente   REAL,
    marge_kit_estimee  REAL,
    bonus_kit_eur      REAL,             -- gain vs revente des 2 barrettes séparément
    statut             TEXT NOT NULL DEFAULT 'candidat'
                       CHECK (statut IN ('candidat', 'notifie', 'acquis', 'expire', 'ignore')),
    cree_le            REAL NOT NULL,
    notifie_le         REAL,
    UNIQUE (stock_id, annonce_id)
);

CREATE INDEX IF NOT EXISTS idx_ram_appar_statut ON ram_appariement(statut, marge_kit_estimee DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 9. ram_notification — messages Telegram (pour l'édition en place)
-- ───────────────────────────────────────────────────────────────────────────
-- On garde message_id : c'est lui qui permet l'`editMessageText` de l'étape 2.
CREATE TABLE IF NOT EXISTS ram_notification (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    annonce_id     INTEGER REFERENCES ram_annonce(id)     ON DELETE CASCADE,
    appariement_id INTEGER REFERENCES ram_appariement(id) ON DELETE CASCADE,
    type           TEXT NOT NULL DEFAULT 'annonce'
                   CHECK (type IN ('annonce', 'appariement', 'systeme')),
    chat_id        TEXT NOT NULL,
    message_id     INTEGER,
    mode           TEXT NOT NULL DEFAULT 'edit' CHECK (mode IN ('edit', 'second_message')),
    etat           TEXT NOT NULL
                   CHECK (etat IN ('non_verifie', 'confirme', 'probable', 'a_verifier',
                                   'rejete', 'quota_epuise')),
    texte          TEXT,
    envoye_le      REAL NOT NULL,
    edite_le       REAL,
    nb_editions    INTEGER NOT NULL DEFAULT 0,
    erreur         TEXT
);

CREATE INDEX IF NOT EXISTS idx_ram_notif_annonce ON ram_notification(annonce_id);
CREATE INDEX IF NOT EXISTS idx_ram_notif_date    ON ram_notification(envoye_le DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 10. ram_prix_observation — matière première du calibrage
-- ───────────────────────────────────────────────────────────────────────────
-- Ventes réellement conclues (Vinted vendu, eBay terminé). ram_calibration.py
-- en tire une médiane par part number et met à jour ram_reference.
CREATE TABLE IF NOT EXISTS ram_prix_observation (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id             INTEGER REFERENCES ram_reference(id) ON DELETE CASCADE,
    part_number        TEXT,
    pn_normalise       TEXT,
    source             TEXT NOT NULL CHECK (source IN ('vinted_vendu', 'ebay_termine',
                                                       'leboncoin', 'manuel')),
    prix               REAL NOT NULL,
    frais_port         REAL DEFAULT 0,
    prix_net_vendeur   REAL,
    capacite_module_go INTEGER,
    nb_modules         INTEGER,
    frequence_mhz      INTEGER,
    cas_latency        INTEGER,
    url                TEXT,
    vendu_le           REAL,
    collecte_le        REAL NOT NULL,
    UNIQUE (source, url)
);

CREATE INDEX IF NOT EXISTS idx_ram_obs_pn   ON ram_prix_observation(pn_normalise, vendu_le DESC);
CREATE INDEX IF NOT EXISTS idx_ram_obs_ref  ON ram_prix_observation(ref_id, vendu_le DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 11. ram_journal_decision — chaque achat / refus, pour affiner le scoring
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ram_journal_decision (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    annonce_id     INTEGER REFERENCES ram_annonce(id) ON DELETE SET NULL,
    action         TEXT NOT NULL CHECK (action IN ('achat', 'refus', 'ignore', 'message',
                                                   'archive', 'auto_rejet')),
    motif          TEXT,
    pre_score      REAL,
    score_final    REAL,
    marge_attendue REAL,
    statut_verif   TEXT,
    decide_par     TEXT NOT NULL DEFAULT 'humain' CHECK (decide_par IN ('humain', 'auto')),
    decide_le      REAL NOT NULL,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ram_journal_date ON ram_journal_decision(decide_le DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 12. ram_pn_candidat — part numbers vus mais absents de la base de référence
-- ───────────────────────────────────────────────────────────────────────────
-- La base de référence ne peut pas être exhaustive dès le premier jour. Tout
-- PN lu (texte ou Gemini) et inconnu atterrit ici avec son compteur
-- d'occurrences → on qualifie en priorité ceux qui reviennent souvent.
CREATE TABLE IF NOT EXISTS ram_pn_candidat (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pn_normalise  TEXT NOT NULL UNIQUE,
    part_number   TEXT,
    marque_devine TEXT,
    occurrences   INTEGER NOT NULL DEFAULT 1,
    prix_min_vu   REAL,
    prix_max_vu   REAL,
    exemple_url   TEXT,
    exemple_titre TEXT,
    statut        TEXT NOT NULL DEFAULT 'a_qualifier'
                  CHECK (statut IN ('a_qualifier', 'integre', 'hors_perimetre', 'ignore')),
    vu_le         REAL NOT NULL,
    maj_le        REAL NOT NULL
);


-- ───────────────────────────────────────────────────────────────────────────
-- 13. ram_scan_stat — hygiène de scraping (tenir 6 mois sans blacklist)
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ram_scan_stat (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    mot_cle      TEXT,
    requetes     INTEGER NOT NULL DEFAULT 0,
    resultats    INTEGER NOT NULL DEFAULT 0,
    nouveaux     INTEGER NOT NULL DEFAULT 0,
    erreurs      INTEGER NOT NULL DEFAULT 0,
    http_429     INTEGER NOT NULL DEFAULT 0,
    http_403     INTEGER NOT NULL DEFAULT 0,
    dernier_run  REAL,
    derniere_err TEXT,
    UNIQUE (source, mot_cle)
);


-- ───────────────────────────────────────────────────────────────────────────
-- 14. ram_migration — versionnage du schéma
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ram_migration (
    version     INTEGER PRIMARY KEY,
    description TEXT,
    applique_le REAL NOT NULL
);


-- ═══════════════════════════════════════════════════════════════════════════
--  VUES — utilisées telles quelles par le dashboard
-- ═══════════════════════════════════════════════════════════════════════════

-- Références dont le prix n'a pas été recalibré depuis > 14 jours.
-- En contexte de pénurie DRAM les prix ne montent que dans un sens : un prix
-- de référence périmé fait rater des affaires correctes.
DROP VIEW IF EXISTS v_ram_reference_perimee;
CREATE VIEW v_ram_reference_perimee AS
SELECT id, part_number, marque, gamme, tier, prix_ref_occasion_eur,
       prix_ref_maj_le, prix_ref_source, prix_ref_n_ventes,
       CAST(julianday('now') - julianday(COALESCE(prix_ref_maj_le, '1970-01-01')) AS INTEGER)
           AS jours_depuis_calibrage
FROM ram_reference
WHERE actif = 1
  AND julianday('now') - julianday(COALESCE(prix_ref_maj_le, '1970-01-01')) > 14
ORDER BY jours_depuis_calibrage DESC;

-- Radar kits : barrettes unitaires en stock, prêtes à être appariées.
DROP VIEW IF EXISTS v_ram_stock_non_apparie;
CREATE VIEW v_ram_stock_non_apparie AS
SELECT s.*, r.gamme AS ref_gamme, r.tier AS ref_tier,
       r.prix_ref_occasion_eur AS ref_prix_kit
FROM ram_stock s
LEFT JOIN ram_reference r ON r.id = s.ref_id
WHERE s.kit_id IS NULL
  AND s.statut IN ('recu', 'en_test', 'teste_ok', 'apparie', 'liste')
ORDER BY s.cree_le DESC;

-- Live feed : les annonces des dernières 24 h avec leur analyse vision.
DROP VIEW IF EXISTS v_ram_feed_24h;
CREATE VIEW v_ram_feed_24h AS
SELECT a.*, v.confiance AS vision_confiance, v.statut AS vision_statut,
       v.part_number_lu, v.drapeaux AS vision_drapeaux,
       n.message_id AS telegram_message_id
FROM ram_annonce a
LEFT JOIN ram_vision_analyse v ON v.annonce_id = a.id
LEFT JOIN ram_notification  n ON n.annonce_id = a.id AND n.type = 'annonce'
WHERE a.detecte_le > (strftime('%s', 'now') - 86400)
ORDER BY a.detecte_le DESC;
