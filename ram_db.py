"""
ram_db.py — Couche base de données du module RAM SNIPER
═══════════════════════════════════════════════════════════════════════════
Même fichier SQLite que SOLDIER (soldier.db), tables préfixées `ram_`.
Le schéma vit dans ram_schema.sql ; les évolutions ultérieures passent par
MIGRATIONS (versionnées dans ram_migration, rejouables sans risque).

Usage CLI :
    python3 ram_db.py init      # crée les tables
    python3 ram_db.py seed      # remplit ram_reference (idempotent)
    python3 ram_db.py show      # échantillon de références
    python3 ram_db.py stats     # état de la base
    python3 ram_db.py calibrage # références périmées (> 14 jours)
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime

import ram_reference_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "soldier.db")
SCHEMA_FILE = os.path.join(BASE_DIR, "ram_schema.sql")

SCHEMA_VERSION = 1

# Évolutions post-v1. Une entrée = (version, description, [instructions SQL]).
# Chaque instruction est rejouable : les erreurs "duplicate column" sont
# ignorées, comme dans soldier_db.MIGRATIONS.
MIGRATIONS = [
    # (2, "exemple", ["ALTER TABLE ram_annonce ADD COLUMN xxx TEXT"]),
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # scrapers + web + vision en parallèle
    conn.execute("PRAGMA busy_timeout = 8000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Crée le schéma et applique les migrations. Idempotent."""
    with open(SCHEMA_FILE, encoding="utf-8") as f:
        schema = f.read()
    with get_db() as conn:
        conn.executescript(schema)
        conn.execute(
            "INSERT OR IGNORE INTO ram_migration (version, description, applique_le) "
            "VALUES (?, ?, ?)", (SCHEMA_VERSION, "schéma initial RAM SNIPER", time.time()))
        deja = {r["version"] for r in conn.execute("SELECT version FROM ram_migration")}
        for version, description, instructions in MIGRATIONS:
            if version in deja:
                continue
            for stmt in instructions:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("INSERT INTO ram_migration (version, description, applique_le) "
                         "VALUES (?, ?, ?)", (version, description, time.time()))


# ─────────────────────── NORMALISATION ───────────────────────
_PN_STRIP = re.compile(r"[^A-Z0-9]")


def normalize_pn(pn):
    """CMK32GX4M2E-3200C16 → CMK32GX4M2E3200C16.
    Sépare-t-on jamais deux PN différents par leur seule ponctuation ? Non :
    les constructeurs n'utilisent les tirets/slashs que comme séparateurs
    décoratifs (F4-3200C14D-16GTZ, KF432C16BB1K2/32)."""
    if not pn:
        return ""
    return _PN_STRIP.sub("", str(pn).upper())


def hash_photos(urls):
    """Clé de cache vision : identifie un jeu de photos indépendamment de
    l'ordre (Vinted réordonne parfois les miniatures d'une requête à l'autre)."""
    if not urls:
        return ""
    joint = "|".join(sorted(str(u) for u in urls))
    return hashlib.sha1(joint.encode("utf-8")).hexdigest()


def cache_key(url, photos):
    return hashlib.sha1(f"{url}::{hash_photos(photos)}".encode("utf-8")).hexdigest()


def _jload(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


# ─────────────────────── RAM_REFERENCE ───────────────────────
def seed_references(force=False, verbose=True):
    """Charge ram_reference_data.REFERENCES en base.

    force=False (défaut) : n'écrase JAMAIS un prix recalibré. Les colonnes
    prix/liquidité/rotation ne sont mises à jour que si la référence n'a
    jamais été calibrée (prix_ref_source = 'seed'). Les caractéristiques
    techniques (fréquence, die, hauteur…) sont toujours resynchronisées :
    elles ne dépendent pas du marché.
    force=True : réécrit tout, y compris les prix. À n'utiliser que pour
    repartir de zéro.
    """
    erreurs = ram_reference_data.verifier_coherence()
    if erreurs:
        raise ValueError("ram_reference_data incohérent :\n  " + "\n  ".join(erreurs))

    now = time.time()
    aujourdhui = date.today().isoformat()
    inseres = maj = inchanges = 0

    with get_db() as conn:
        for r in ram_reference_data.REFERENCES:
            pn_norm = normalize_pn(r["part_number"])
            existant = conn.execute(
                "SELECT id, prix_ref_source FROM ram_reference WHERE part_number = ?",
                (r["part_number"],)).fetchone()

            technique = (
                r["marque"], r["gamme"], json.dumps(r["alias"], ensure_ascii=False),
                r["capacite_module_go"], r["nb_modules"], r["capacite_totale_go"],
                r["frequence_mhz"], r["cas_latency"], r["voltage"], r["rank"],
                r["die_type"], int(r["rgb"]), r["couleur"], r["hauteur_mm"],
                int(r["low_profile"]), r["tier"], int(r["pn_verifie"]), r["notes"],
            )

            if existant is None:
                conn.execute("""
                    INSERT INTO ram_reference (
                        part_number, pn_normalise, marque, gamme, alias,
                        capacite_module_go, nb_modules, capacite_totale_go,
                        frequence_mhz, cas_latency, voltage, "rank", die_type,
                        rgb, couleur, hauteur_mm, low_profile, tier, pn_verifie, notes,
                        prix_ref_occasion_eur, prix_ref_maj_le, prix_ref_source,
                        liquidite, delai_rotation_jours, cree_le, maj_le)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, 'seed', ?, ?, ?, ?)
                """, (r["part_number"], pn_norm) + technique +
                     (r["prix_ref_occasion_eur"], aujourdhui,
                      r["liquidite"], r["delai_rotation_jours"], now, now))
                inseres += 1
                continue

            ecraser_prix = force or existant["prix_ref_source"] == "seed"
            if ecraser_prix:
                conn.execute("""
                    UPDATE ram_reference SET
                        pn_normalise=?, marque=?, gamme=?, alias=?,
                        capacite_module_go=?, nb_modules=?, capacite_totale_go=?,
                        frequence_mhz=?, cas_latency=?, voltage=?, "rank"=?, die_type=?,
                        rgb=?, couleur=?, hauteur_mm=?, low_profile=?, tier=?,
                        pn_verifie=?, notes=?,
                        prix_ref_occasion_eur=?, liquidite=?, delai_rotation_jours=?,
                        maj_le=?
                    WHERE id=?
                """, (pn_norm,) + technique +
                     (r["prix_ref_occasion_eur"], r["liquidite"],
                      r["delai_rotation_jours"], now, existant["id"]))
                maj += 1
            else:
                # Prix issu d'un vrai calibrage : on n'y touche pas.
                conn.execute("""
                    UPDATE ram_reference SET
                        pn_normalise=?, marque=?, gamme=?, alias=?,
                        capacite_module_go=?, nb_modules=?, capacite_totale_go=?,
                        frequence_mhz=?, cas_latency=?, voltage=?, "rank"=?, die_type=?,
                        rgb=?, couleur=?, hauteur_mm=?, low_profile=?, tier=?,
                        pn_verifie=?, notes=?, maj_le=?
                    WHERE id=?
                """, (pn_norm,) + technique + (now, existant["id"]))
                inchanges += 1

    if verbose:
        print(f"ram_reference: {inseres} insérée(s), {maj} mise(s) à jour, "
              f"{inchanges} prix calibré(s) préservé(s)")
    return {"inseres": inseres, "maj": maj, "preserves": inchanges}


def get_reference(ref_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ram_reference WHERE id=?", (ref_id,)).fetchone()
        return dict(row) if row else None


def find_reference_by_pn(pn):
    """Recherche exacte puis par préfixe. Les vendeurs tronquent souvent le PN
    (CMK32GX4M2E3200 sans le C16) : un préfixe d'au moins 10 caractères qui ne
    matche qu'une seule référence est accepté."""
    norm = normalize_pn(pn)
    if not norm:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ram_reference WHERE pn_normalise=? AND actif=1", (norm,)).fetchone()
        if row:
            return dict(row)
        if len(norm) >= 10:
            rows = conn.execute(
                "SELECT * FROM ram_reference WHERE pn_normalise LIKE ? AND actif=1 LIMIT 2",
                (norm + "%",)).fetchall()
            if len(rows) == 1:
                return dict(rows[0])
    return None


def find_references_by_specs(capacite_module=None, nb_modules=None, frequence=None,
                             cas_latency=None, marque=None, gamme=None, limit=10,
                             conservateur=False):
    """Repli quand aucun part number n'est lisible : on cherche par
    caractéristiques. Sert à estimer une valeur de revente, jamais à affirmer
    une identification (un kit vendu comme kit sur cette base serait une
    arnaque involontaire : specs identiques ≠ même batch)."""
    q = ["SELECT * FROM ram_reference WHERE actif=1"]
    params = []
    if capacite_module:
        q.append("AND capacite_module_go=?"); params.append(capacite_module)
    if nb_modules:
        q.append("AND nb_modules=?"); params.append(nb_modules)
    if frequence:
        q.append("AND frequence_mhz=?"); params.append(frequence)
    if cas_latency:
        q.append("AND cas_latency=?"); params.append(cas_latency)
    if marque:
        q.append("AND LOWER(marque)=?"); params.append(marque.lower())
    if gamme:
        q.append("AND LOWER(gamme) LIKE ?"); params.append(f"%{gamme.lower()}%")
    # Quand une caractéristique manque (typiquement la fréquence, absente de la
    # plupart des titres Vinted), on prend la référence la MOINS chère qui
    # colle. Supposer du 3600 CL16 sur une annonce qui ne dit rien surestimerait
    # la revente et ferait acheter trop cher : mieux vaut ne pas alerter que
    # d'alerter à tort.
    if conservateur:
        q.append("ORDER BY prix_ref_occasion_eur ASC, liquidite DESC LIMIT ?")
    else:
        q.append("ORDER BY liquidite DESC, prix_ref_occasion_eur DESC LIMIT ?")
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


# Niveaux de repli, du plus précis au plus vague. Chaque entrée :
# (critères utilisés, confiance associée, libellé pour la notification).
# `freq` absent d'un niveau ⇒ recherche conservatrice (référence la moins chère).
_NIVEAUX_APPROCHE = [
    (("cap", "nb", "freq", "cl", "marque", "gamme"), 0.62, "specs + marque + gamme"),
    (("cap", "nb", "freq", "cl", "marque"),          0.58, "specs + CL + marque"),
    (("cap", "nb", "freq", "cl"),                    0.50, "specs + CL"),
    (("cap", "nb", "freq", "marque"),                0.48, "specs + marque"),
    (("cap", "nb", "freq"),                          0.42, "capacité + fréquence"),
    (("cap", "freq", "marque"),                      0.38, "capacité + fréquence + marque"),
    (("cap", "nb", "marque"),                        0.32, "capacité + marque, fréquence inconnue"),
    (("cap", "marque"),                              0.28, "marque seule, fréquence inconnue"),
    (("cap", "nb"),                                  0.22, "capacité seule"),
    (("cap",),                                       0.18, "capacité seule, estimation plancher"),
]


def find_reference_approchante(capacite_module=None, nb_modules=None, frequence=None,
                               cas_latency=None, marque=None, gamme=None):
    """Repli quand aucun part number n'est lisible.

    Relâche les critères par paliers jusqu'à trouver une référence plausible.
    Retourne (référence, confiance, explication) ou (None, 0, None).

    Sert UNIQUEMENT à estimer une valeur de revente, jamais à affirmer une
    identité : deux kits aux mêmes caractéristiques ne sont pas le même kit et
    ne doivent jamais être vendus comme tel.
    """
    if not capacite_module:
        return None, 0.0, None

    dispo = {"cap": capacite_module, "nb": nb_modules, "freq": frequence,
             "cl": cas_latency, "marque": marque, "gamme": gamme}

    for criteres, confiance, libelle in _NIVEAUX_APPROCHE:
        if any(dispo.get(c) is None for c in criteres):
            continue
        conservateur = "freq" not in criteres
        candidats = find_references_by_specs(
            capacite_module=dispo["cap"],
            nb_modules=dispo["nb"] if "nb" in criteres else None,
            frequence=dispo["freq"] if "freq" in criteres else None,
            cas_latency=dispo["cl"] if "cl" in criteres else None,
            marque=dispo["marque"] if "marque" in criteres else None,
            gamme=dispo["gamme"] if "gamme" in criteres else None,
            conservateur=conservateur, limit=3)
        if candidats:
            return candidats[0], confiance, libelle
    return None, 0.0, None


def prix_plancher(capacite_module, nb_modules=1):
    """Valeur de revente la plus basse crédible pour une capacité donnée.

    Filet de sécurité quand aucune référence ne colle : on prend la médiane des
    références d'entrée de gamme (tiers C et D) de cette capacité. Se recalibre
    tout seul avec le reste de la base, plutôt que d'être figé en dur.
    """
    with get_db() as conn:
        lignes = conn.execute("""
            SELECT prix_ref_occasion_eur / nb_modules AS unitaire
            FROM ram_reference
            WHERE actif=1 AND capacite_module_go=? AND tier IN ('C','D')
            ORDER BY unitaire
        """, (capacite_module,)).fetchall()
    if not lignes:
        with get_db() as conn:
            lignes = conn.execute("""
                SELECT MIN(prix_ref_occasion_eur / nb_modules) AS unitaire
                FROM ram_reference WHERE actif=1 AND capacite_module_go=?
            """, (capacite_module,)).fetchall()
    valeurs = [l["unitaire"] for l in lignes if l["unitaire"]]
    if not valeurs:
        return None
    mediane = valeurs[len(valeurs) // 2]
    return round(mediane * max(1, nb_modules or 1), 2)


def list_references(tier=None, marque=None, actif=True, limit=500, offset=0):
    q = ["SELECT * FROM ram_reference WHERE 1=1"]
    params = []
    if actif is not None:
        q.append("AND actif=?"); params.append(int(actif))
    if tier:
        q.append("AND tier=?"); params.append(tier)
    if marque:
        q.append("AND LOWER(marque)=?"); params.append(marque.lower())
    q.append("ORDER BY tier, marque, gamme, capacite_totale_go DESC, frequence_mhz DESC")
    q.append("LIMIT ? OFFSET ?")
    params += [limit, offset]
    with get_db() as conn:
        return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


def references_perimees(jours=14):
    """Références non recalibrées depuis `jours`. En pénurie DRAM les prix ne
    bougent que dans un sens : un prix vieux d'un mois fait rater des affaires."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, part_number, marque, gamme, tier, prix_ref_occasion_eur,
                   prix_ref_maj_le, prix_ref_source, prix_ref_n_ventes,
                   CAST(julianday('now') - julianday(COALESCE(prix_ref_maj_le,'1970-01-01'))
                        AS INTEGER) AS jours_depuis_calibrage
            FROM ram_reference
            WHERE actif=1
              AND julianday('now') - julianday(COALESCE(prix_ref_maj_le,'1970-01-01')) > ?
            ORDER BY jours_depuis_calibrage DESC
        """, (jours,)).fetchall()
        return [dict(r) for r in rows]


def maj_prix_reference(ref_id, prix, source, n_ventes=0):
    with get_db() as conn:
        conn.execute("""
            UPDATE ram_reference
            SET prix_ref_occasion_eur=?, prix_ref_source=?, prix_ref_n_ventes=?,
                prix_ref_maj_le=?, maj_le=?
            WHERE id=?
        """, (round(float(prix), 2), source, int(n_ventes),
              date.today().isoformat(), time.time(), ref_id))


# ─────────────────────── RAM_ANNONCE ───────────────────────
_ANNONCE_CHAMPS = (
    "source", "source_id", "url", "titre", "description", "mot_cle",
    "prix_affiche", "frais_port", "frais_protection", "prix_total", "main_propre",
    "vendeur_pseudo", "vendeur_note", "vendeur_ventes", "localisation",
    "code_postal", "departement", "photos", "nb_photos", "photos_hash",
    "publie_le", "ref_id", "pn_detecte", "pn_normalise", "capacite_module_go",
    "nb_modules", "capacite_totale_go", "frequence_mhz", "cas_latency",
    "marque_detectee", "gamme_detectee", "tier", "est_kit", "confiance_texte",
    "pre_score", "score_final", "revente_estimee", "marge_estimee", "marge_pct",
    "marge_reelle", "marge_reelle_pct", "qualite_annonce", "score_vendeur",
    "score_logistique", "statut_verif", "statut", "exclusion", "rejet_motif",
    "drapeaux", "brut",
)


def upsert_annonce(data):
    """Insère ou met à jour une annonce (clé = url). Retourne (id, nouvelle).

    Sur une annonce déjà connue, seuls le prix, les photos et l'horodatage de
    revue sont rafraîchis : les scores et verdicts déjà calculés ne sont pas
    écrasés par un simple re-scan (sinon un ✅ CONFIRMÉ repasserait en
    NON VÉRIFIÉ à chaque tour de scan)."""
    now = time.time()
    d = dict(data)
    d.setdefault("frais_port", 0.0)
    d.setdefault("frais_protection", 0.0)
    d["prix_total"] = round(
        float(d.get("prix_affiche", 0)) + float(d["frais_port"]) + float(d["frais_protection"]), 2)
    for champ_json in ("photos", "drapeaux"):
        if isinstance(d.get(champ_json), (list, dict)):
            d[champ_json] = json.dumps(d[champ_json], ensure_ascii=False)
    if isinstance(d.get("brut"), (list, dict)):
        d["brut"] = json.dumps(d["brut"], ensure_ascii=False)
    if d.get("photos") and not d.get("photos_hash"):
        d["photos_hash"] = hash_photos(_jload(d["photos"], []))
    if not d.get("nb_photos"):
        d["nb_photos"] = len(_jload(d.get("photos"), []))
    for bool_champ in ("main_propre", "est_kit"):
        if bool_champ in d:
            d[bool_champ] = int(bool(d[bool_champ]))

    champs = [c for c in _ANNONCE_CHAMPS if c in d]
    with get_db() as conn:
        existant = conn.execute("SELECT id FROM ram_annonce WHERE url=?",
                                (d["url"],)).fetchone()
        if existant:
            maj = {c: d[c] for c in ("prix_affiche", "frais_port", "frais_protection",
                                     "prix_total", "photos", "nb_photos", "photos_hash")
                   if c in d}
            maj["vue_le"] = now
            maj["maj_le"] = now
            maj["encore_en_ligne"] = 1
            sets = ", ".join(f"{c}=?" for c in maj)
            conn.execute(f"UPDATE ram_annonce SET {sets} WHERE id=?",
                         list(maj.values()) + [existant["id"]])
            return existant["id"], False

        cols = list(champs) + ["detecte_le", "maj_le", "vue_le"]
        vals = [d[c] for c in champs] + [now, now, now]
        cur = conn.execute(
            f"INSERT INTO ram_annonce ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", vals)
        return cur.lastrowid, True


def get_annonce(annonce_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ram_annonce WHERE id=?", (annonce_id,)).fetchone()
        return dict(row) if row else None


def maj_annonce(annonce_id, champs):
    if not champs:
        return
    d = dict(champs)
    for champ_json in ("photos", "drapeaux"):
        if isinstance(d.get(champ_json), (list, dict)):
            d[champ_json] = json.dumps(d[champ_json], ensure_ascii=False)
    d["maj_le"] = time.time()
    sets = ", ".join(f"{c}=?" for c in d)
    with get_db() as conn:
        conn.execute(f"UPDATE ram_annonce SET {sets} WHERE id=?",
                     list(d.values()) + [annonce_id])


def annonce_existe(url):
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM ram_annonce WHERE url=?", (url,)).fetchone() is not None


def feed(heures=24, min_score=0, tier=None, statut_verif=None, statut=None, limit=200):
    q = ["""SELECT a.*, v.confiance AS vision_confiance, v.part_number_lu,
                   v.drapeaux AS vision_drapeaux, n.message_id AS telegram_message_id
            FROM ram_annonce a
            LEFT JOIN ram_vision_analyse v ON v.annonce_id = a.id
            LEFT JOIN ram_notification n ON n.annonce_id = a.id AND n.type='annonce'
            WHERE a.detecte_le > ?"""]
    params = [time.time() - heures * 3600]
    if min_score:
        q.append("AND COALESCE(a.score_final, a.pre_score, 0) >= ?"); params.append(min_score)
    if tier:
        q.append("AND a.tier=?"); params.append(tier)
    if statut_verif:
        q.append("AND a.statut_verif=?"); params.append(statut_verif)
    if statut:
        q.append("AND a.statut=?"); params.append(statut)
    q.append("ORDER BY COALESCE(a.score_final, a.pre_score, 0) DESC, a.detecte_le DESC LIMIT ?")
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


# ─────────────────────── FILE + QUOTA VISION ───────────────────────
def enfiler_vision(annonce_id, priorite):
    """Ajoute (ou repriorise) une annonce dans la file du worker vision."""
    with get_db() as conn:
        conn.execute("""
            INSERT INTO ram_vision_file (annonce_id, priorite, statut, cree_le)
            VALUES (?, ?, 'en_attente', ?)
            ON CONFLICT(annonce_id) DO UPDATE SET
                priorite = MAX(priorite, excluded.priorite),
                statut = CASE WHEN statut IN ('fait','en_cours') THEN statut
                              ELSE 'en_attente' END
        """, (annonce_id, float(priorite or 0), time.time()))


def prochaine_annonce_vision():
    """Dépile l'annonce au meilleur pré-score. Le passage en 'en_cours' est
    fait dans la même transaction que la lecture : deux workers vision ne
    peuvent pas prendre la même annonce."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT f.id AS file_id, f.annonce_id, f.priorite, f.tentatives, a.*
            FROM ram_vision_file f
            JOIN ram_annonce a ON a.id = f.annonce_id
            WHERE f.statut = 'en_attente' AND a.encore_en_ligne = 1
            ORDER BY f.priorite DESC, f.cree_le ASC
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        conn.execute("UPDATE ram_vision_file SET statut='en_cours', pris_le=?, "
                     "tentatives=tentatives+1 WHERE id=?", (time.time(), row["file_id"]))
        return dict(row)


def cloturer_vision(file_id, statut, erreur=None):
    with get_db() as conn:
        conn.execute("UPDATE ram_vision_file SET statut=?, traite_le=?, derniere_erreur=? "
                     "WHERE id=?", (statut, time.time(), erreur, file_id))


def differer_vision(file_id, erreur="quota épuisé"):
    """Quota épuisé : l'annonce n'est pas perdue, elle repassera en file à la
    réinitialisation (voir reprendre_differees)."""
    with get_db() as conn:
        conn.execute("UPDATE ram_vision_file SET statut='differe', derniere_erreur=?, "
                     "tentatives=MAX(tentatives-1, 0) WHERE id=?", (erreur, file_id))


def reprendre_differees(max_tentatives=3):
    """Rattrapage : remet en file les annonces différées faute de quota, à
    condition qu'elles soient encore en ligne."""
    with get_db() as conn:
        cur = conn.execute("""
            UPDATE ram_vision_file SET statut='en_attente', derniere_erreur=NULL
            WHERE statut='differe' AND tentatives < ?
              AND annonce_id IN (SELECT id FROM ram_annonce WHERE encore_en_ligne=1)
        """, (max_tentatives,))
        return cur.rowcount


def etat_file_vision():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT statut, COUNT(*) n FROM ram_vision_file GROUP BY statut").fetchall()
        return {r["statut"]: r["n"] for r in rows}


def _fenetres(maintenant=None):
    dt = datetime.fromtimestamp(maintenant or time.time())
    return {"minute": dt.strftime("%Y-%m-%dT%H:%M"), "jour": dt.strftime("%Y-%m-%d")}


def quota_disponible(plafond_minute, plafond_jour, provider="gemini"):
    """(ok, détail). Les fenêtres expirées disparaissent d'elles-mêmes : une
    nouvelle fenêtre = une nouvelle ligne, donc compteur à 0. Pas de tâche de
    remise à zéro à orchestrer."""
    f = _fenetres()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT portee, compteur FROM ram_vision_quota "
            "WHERE provider=? AND ((portee='minute' AND fenetre=?) OR (portee='jour' AND fenetre=?))",
            (provider, f["minute"], f["jour"])).fetchall()
    conso = {r["portee"]: r["compteur"] for r in rows}
    minute = conso.get("minute", 0)
    jour = conso.get("jour", 0)
    detail = {"minute": minute, "plafond_minute": plafond_minute,
              "jour": jour, "plafond_jour": plafond_jour}
    if plafond_jour and jour >= plafond_jour:
        return False, dict(detail, motif="quota journalier épuisé")
    if plafond_minute and minute >= plafond_minute:
        return False, dict(detail, motif="quota minute atteint")
    return True, detail


def consommer_quota(provider="gemini", plafond_minute=None, plafond_jour=None):
    f = _fenetres()
    now = time.time()
    with get_db() as conn:
        for portee, fenetre, plafond in (("minute", f["minute"], plafond_minute),
                                         ("jour", f["jour"], plafond_jour)):
            conn.execute("""
                INSERT INTO ram_vision_quota (provider, portee, fenetre, compteur, plafond, maj_le)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(provider, portee, fenetre)
                DO UPDATE SET compteur = compteur + 1, plafond=excluded.plafond, maj_le=excluded.maj_le
            """, (provider, portee, fenetre, plafond, now))


def purger_quota(jours=7):
    """Les vieilles fenêtres minute s'accumulent (1440/jour) : on les purge."""
    limite = datetime.fromtimestamp(time.time() - jours * 86400).strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("DELETE FROM ram_vision_quota WHERE portee='minute' AND fenetre < ?",
                     (limite,))


def conso_vision_jour(provider="gemini"):
    f = _fenetres()
    with get_db() as conn:
        row = conn.execute("SELECT compteur, plafond FROM ram_vision_quota "
                           "WHERE provider=? AND portee='jour' AND fenetre=?",
                           (provider, f["jour"])).fetchone()
        return dict(row) if row else {"compteur": 0, "plafond": None}


# ─────────────────────── ANALYSES VISION ───────────────────────
_VISION_CHAMPS = (
    "annonce_id", "cache_cle", "provider", "modele", "statut", "est_ddr4_desktop",
    "generation_suspectee", "est_sodimm", "est_ecc", "est_registered",
    "part_number_lu", "pn_normalise", "marque", "nb_barrettes_visibles",
    "capacite_par_barrette", "nb_puces_par_face", "code_semaine",
    "sticker_authentique", "etat_contacts", "etat_dissipateur", "rgb", "couleur",
    "hauteur_estimee", "photo_lisible", "drapeaux", "confiance",
    "photos_envoyees", "latence_ms", "reponse_brute", "erreur",
)


def enregistrer_analyse_vision(data):
    d = {k: v for k, v in data.items() if k in _VISION_CHAMPS}
    if isinstance(d.get("drapeaux"), (list, dict)):
        d["drapeaux"] = json.dumps(d["drapeaux"], ensure_ascii=False)
    for b in ("est_ddr4_desktop", "est_sodimm", "est_ecc", "est_registered",
              "sticker_authentique", "rgb", "photo_lisible"):
        if d.get(b) is not None:
            d[b] = int(bool(d[b]))
    if d.get("part_number_lu") and not d.get("pn_normalise"):
        d["pn_normalise"] = normalize_pn(d["part_number_lu"])
    d["cree_le"] = time.time()
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT OR REPLACE INTO ram_vision_analyse ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        return cur.lastrowid


def analyse_en_cache(cle):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ram_vision_analyse WHERE cache_cle=?", (cle,)).fetchone()
        return dict(row) if row else None


def analyse_de_annonce(annonce_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ram_vision_analyse WHERE annonce_id=? "
                           "ORDER BY cree_le DESC LIMIT 1", (annonce_id,)).fetchone()
        return dict(row) if row else None


# ─────────────────────── STOCK / KITS / APPARIEMENTS ───────────────────────
def creer_stock(data):
    d = dict(data)
    d.setdefault("cree_le", time.time())
    d.setdefault("achete_le", d["cree_le"])
    d["prix_revient"] = round(float(d.get("prix_achat", 0)) + float(d.get("frais_port", 0))
                              + float(d.get("frais_protection", 0)), 2)
    if d.get("part_number"):
        d["pn_normalise"] = normalize_pn(d["part_number"])
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO ram_stock ({', '.join(cols)}) "
                           f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        return cur.lastrowid


def maj_stock(stock_id, champs):
    d = dict(champs)
    d["maj_le"] = time.time()
    # Le délai de rotation est LE KPI : le recalculer à chaque passage en vendu
    # évite d'avoir à y penser côté appelant.
    if d.get("statut") == "vendu":
        d.setdefault("vendu_le", time.time())
        with get_db() as conn:
            row = conn.execute("SELECT achete_le FROM ram_stock WHERE id=?", (stock_id,)).fetchone()
        if row and row["achete_le"]:
            d["delai_rotation_jours"] = round((d["vendu_le"] - row["achete_le"]) / 86400, 1)
    sets = ", ".join(f"{c}=?" for c in d)
    with get_db() as conn:
        conn.execute(f"UPDATE ram_stock SET {sets} WHERE id=?", list(d.values()) + [stock_id])


def list_stock(statut=None, non_apparie=False, limit=500):
    q = ["""SELECT s.*, r.gamme AS ref_gamme, r.tier AS ref_tier,
                   r.prix_ref_occasion_eur AS ref_prix
            FROM ram_stock s LEFT JOIN ram_reference r ON r.id=s.ref_id WHERE 1=1"""]
    params = []
    if statut:
        q.append("AND s.statut=?"); params.append(statut)
    if non_apparie:
        q.append("AND s.kit_id IS NULL AND s.statut IN "
                 "('recu','en_test','teste_ok','apparie','liste')")
    q.append("ORDER BY s.cree_le DESC LIMIT ?")
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


def creer_kit(data, stock_ids=()):
    d = dict(data)
    d.setdefault("cree_le", time.time())
    if d.get("part_number"):
        d["pn_normalise"] = normalize_pn(d["part_number"])
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO ram_kit ({', '.join(cols)}) "
                           f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        kit_id = cur.lastrowid
        for sid in stock_ids:
            conn.execute("UPDATE ram_stock SET kit_id=?, statut='apparie', maj_le=? WHERE id=?",
                         (kit_id, time.time(), sid))
        return kit_id


def creer_appariement(data):
    d = dict(data)
    d.setdefault("cree_le", time.time())
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT OR IGNORE INTO ram_appariement ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        return cur.lastrowid or None


def list_appariements(statut="candidat", limit=100):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.*, a.titre, a.url, a.prix_total, a.source, a.photos,
                   s.part_number AS stock_pn, s.code_semaine AS stock_semaine,
                   s.prix_revient AS stock_revient
            FROM ram_appariement p
            JOIN ram_annonce a ON a.id=p.annonce_id
            JOIN ram_stock s ON s.id=p.stock_id
            WHERE p.statut=? ORDER BY p.marge_kit_estimee DESC LIMIT ?
        """, (statut, limit)).fetchall()
        return [dict(r) for r in rows]


def maj_appariement(appariement_id, champs):
    sets = ", ".join(f"{c}=?" for c in champs)
    with get_db() as conn:
        conn.execute(f"UPDATE ram_appariement SET {sets} WHERE id=?",
                     list(champs.values()) + [appariement_id])


# ─────────────────────── NOTIFICATIONS ───────────────────────
def enregistrer_notification(data):
    d = dict(data)
    d.setdefault("envoye_le", time.time())
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO ram_notification ({', '.join(cols)}) "
                           f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        return cur.lastrowid


def notification_de_annonce(annonce_id, type_notif="annonce"):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM ram_notification WHERE annonce_id=? AND type=? "
                           "ORDER BY envoye_le DESC LIMIT 1",
                           (annonce_id, type_notif)).fetchone()
        return dict(row) if row else None


def maj_notification(notif_id, champs):
    d = dict(champs)
    sets = ", ".join(f"{c}=?" for c in d)
    with get_db() as conn:
        conn.execute(f"UPDATE ram_notification SET {sets} WHERE id=?",
                     list(d.values()) + [notif_id])


def derniere_notification_le():
    """Sert à l'anti-spam (max 1 notification / 60 s). Les éditions ne comptent
    pas : seul envoye_le est regardé."""
    with get_db() as conn:
        row = conn.execute("SELECT MAX(envoye_le) t FROM ram_notification").fetchone()
        return row["t"] or 0


# ─────────────────────── OBSERVATIONS DE PRIX ───────────────────────
def enregistrer_observation(data):
    d = dict(data)
    d.setdefault("collecte_le", time.time())
    if d.get("part_number"):
        d["pn_normalise"] = normalize_pn(d["part_number"])
    cols = list(d)
    with get_db() as conn:
        cur = conn.execute(f"INSERT OR IGNORE INTO ram_prix_observation ({', '.join(cols)}) "
                           f"VALUES ({', '.join('?' * len(cols))})", [d[c] for c in cols])
        return cur.lastrowid or None


def observations_par_pn(pn_normalise, jours=30):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM ram_prix_observation
            WHERE pn_normalise=? AND COALESCE(vendu_le, collecte_le) > ?
            ORDER BY COALESCE(vendu_le, collecte_le) DESC
        """, (pn_normalise, time.time() - jours * 86400)).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────── PN CANDIDATS / JOURNAL / STATS ───────────────────────
def signaler_pn_inconnu(pn, marque=None, prix=None, url=None, titre=None):
    """Un PN lu mais absent de la base de référence. Le compteur d'occurrences
    dit lesquels valent la peine d'être qualifiés en priorité."""
    norm = normalize_pn(pn)
    if not norm or len(norm) < 6:
        return
    now = time.time()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO ram_pn_candidat (pn_normalise, part_number, marque_devine,
                                         occurrences, prix_min_vu, prix_max_vu,
                                         exemple_url, exemple_titre, vu_le, maj_le)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pn_normalise) DO UPDATE SET
                occurrences = occurrences + 1,
                prix_min_vu = MIN(COALESCE(prix_min_vu, ?), ?),
                prix_max_vu = MAX(COALESCE(prix_max_vu, ?), ?),
                maj_le = ?
        """, (norm, pn, marque, prix, prix, url, titre, now, now,
              prix, prix, prix, prix, now))


def list_pn_candidats(statut="a_qualifier", limit=100):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ram_pn_candidat WHERE statut=? "
                            "ORDER BY occurrences DESC, maj_le DESC LIMIT ?",
                            (statut, limit)).fetchall()
        return [dict(r) for r in rows]


def journaliser(action, annonce=None, motif=None, decide_par="humain", notes=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO ram_journal_decision (annonce_id, action, motif, pre_score,
                score_final, marge_attendue, statut_verif, decide_par, decide_le, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ((annonce or {}).get("id"), action, motif,
              (annonce or {}).get("pre_score"), (annonce or {}).get("score_final"),
              (annonce or {}).get("marge_reelle") or (annonce or {}).get("marge_estimee"),
              (annonce or {}).get("statut_verif"), decide_par, time.time(), notes))


def journal(limit=200):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT j.*, a.titre, a.url, a.prix_total
            FROM ram_journal_decision j LEFT JOIN ram_annonce a ON a.id=j.annonce_id
            ORDER BY j.decide_le DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def maj_scan_stat(source, mot_cle, **compteurs):
    now = time.time()
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO ram_scan_stat (source, mot_cle) VALUES (?, ?)",
                     (source, mot_cle))
        incs = ", ".join(f"{k}={k}+?" for k in compteurs)
        vals = list(compteurs.values())
        sql = "UPDATE ram_scan_stat SET dernier_run=?"
        params = [now]
        if incs:
            sql += ", " + incs
            params += vals
        if compteurs.get("erreurs") and compteurs.get("_err"):
            pass
        sql += " WHERE source=? AND mot_cle=?"
        params += [source, mot_cle]
        conn.execute(sql, params)


def scan_stats():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM ram_scan_stat ORDER BY dernier_run DESC")]


# ─────────────────────── P&L / KPI ───────────────────────
def kpis():
    """Capital engagé, capital dormant, marge réalisée, délai moyen de rotation.
    Le capital dormant (stock non vendu depuis > 30 jours) est l'alerte qui
    compte : c'est lui qui tue la rotation."""
    with get_db() as conn:
        engage = conn.execute("""
            SELECT COALESCE(SUM(prix_revient), 0) v, COUNT(*) n FROM ram_stock
            WHERE statut NOT IN ('vendu', 'retourne')
        """).fetchone()
        dormant = conn.execute("""
            SELECT COALESCE(SUM(prix_revient), 0) v, COUNT(*) n FROM ram_stock
            WHERE statut NOT IN ('vendu', 'retourne') AND achete_le < ?
        """, (time.time() - 30 * 86400,)).fetchone()
        ventes = conn.execute("""
            SELECT COALESCE(SUM(marge_nette), 0) marge, COUNT(*) n,
                   COALESCE(AVG(delai_rotation_jours), 0) rotation,
                   COALESCE(SUM(prix_vente), 0) ca
            FROM ram_stock WHERE statut='vendu'
        """).fetchone()
        feed_24h = conn.execute(
            "SELECT COUNT(*) n FROM ram_annonce WHERE detecte_le > ?",
            (time.time() - 86400,)).fetchone()
        notifs_24h = conn.execute(
            "SELECT COUNT(*) n FROM ram_notification WHERE envoye_le > ?",
            (time.time() - 86400,)).fetchone()

    capital_engage = round(engage["v"], 2)
    capital_dormant = round(dormant["v"], 2)
    part_dormant = round(capital_dormant / capital_engage * 100, 1) if capital_engage else 0.0
    return {
        "capital_engage": capital_engage,
        "articles_en_stock": engage["n"],
        "capital_dormant": capital_dormant,
        "articles_dormants": dormant["n"],
        "part_dormant_pct": part_dormant,
        "alerte_dormant": part_dormant > 40,
        "marge_realisee": round(ventes["marge"], 2),
        "ca_realise": round(ventes["ca"], 2),
        "ventes": ventes["n"],
        "rotation_moyenne_jours": round(ventes["rotation"], 1),
        "annonces_24h": feed_24h["n"],
        "notifications_24h": notifs_24h["n"],
        "references_perimees": len(references_perimees()),
    }


def stats_base():
    with get_db() as conn:
        def n(table, where="1=1", params=()):
            return conn.execute(f"SELECT COUNT(*) c FROM {table} WHERE {where}",
                                params).fetchone()["c"]
        tiers = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) c FROM ram_reference GROUP BY tier")}
        return {
            "references": n("ram_reference"),
            "references_par_tier": tiers,
            "annonces": n("ram_annonce"),
            "annonces_24h": n("ram_annonce", "detecte_le > ?", (time.time() - 86400,)),
            "analyses_vision": n("ram_vision_analyse"),
            "file_vision": etat_file_vision(),
            "stock": n("ram_stock"),
            "kits": n("ram_kit"),
            "appariements_candidats": n("ram_appariement", "statut='candidat'"),
            "notifications": n("ram_notification"),
            "pn_a_qualifier": n("ram_pn_candidat", "statut='a_qualifier'"),
            "observations_prix": n("ram_prix_observation"),
        }


# ─────────────────────── CLI ───────────────────────
def _print_refs(refs):
    entete = (f"{'PART NUMBER':<24} {'MARQUE':<10} {'GAMME':<24} {'CONFIG':<14} "
              f"{'DIE':<14} {'T':<2} {'PRIX':>6} {'LIQ':>4} {'ROT':>4}")
    print(entete)
    print("─" * len(entete))
    for r in refs:
        config = f"{r['nb_modules']}×{r['capacite_module_go']} {r['frequence_mhz']}C{r['cas_latency']}"
        print(f"{r['part_number']:<24} {r['marque']:<10} {r['gamme'][:24]:<24} {config:<14} "
              f"{(r['die_type'] or '—')[:14]:<14} {r['tier']:<2} "
              f"{r['prix_ref_occasion_eur']:>5.0f}€ {r['liquidite']:>4} "
              f"{r['delai_rotation_jours'] or 0:>3}j")


def main():
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "init":
        init_db()
        print(f"✅ Schéma RAM SNIPER initialisé dans {DB_FILE}")
    elif cmd == "seed":
        init_db()
        seed_references(force="--force" in sys.argv)
    elif cmd == "show":
        tier = sys.argv[2] if len(sys.argv) > 2 else None
        refs = list_references(tier=tier, limit=int(os.environ.get("RAM_SHOW_LIMIT", "40")))
        _print_refs(refs)
        print(f"\n{len(refs)} référence(s) affichée(s)")
    elif cmd == "stats":
        for k, v in stats_base().items():
            print(f"  {k:<26} {v}")
        print("\n── KPI ──")
        for k, v in kpis().items():
            print(f"  {k:<26} {v}")
    elif cmd == "calibrage":
        perimees = references_perimees()
        if not perimees:
            print("✅ Aucune référence périmée (toutes recalibrées depuis moins de 14 jours)")
        else:
            print(f"⚠️ {len(perimees)} référence(s) à recalibrer :")
            for r in perimees[:40]:
                print(f"  {r['part_number']:<24} {r['prix_ref_occasion_eur']:>5.0f}€  "
                      f"maj {r['prix_ref_maj_le']} ({r['jours_depuis_calibrage']}j)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
