"""
soldier_db.py — Base SQLite unifiée pour SOLDIER
═══════════════════════════════════════════════════════════════════════════
Fusionne PC-Sniper (scanner de deals) et SOLDER (gestion achats/builds/ventes)
en une seule base de données. Remplace le stockage localStorage/artifact de
l'ancien SOLDER — plus de ressaisie manuelle, le scanner alimente directement
le pipeline d'achat.

Tables :
  listings  — deals détectés par le scanner (equivalent des "deals" actuels)
  purchases — objets achetés (issus d'un listing ou saisis à la main)
  builds    — assemblages de plusieurs purchases en PC complet
  sales     — ventes (d'un achat seul ou d'un build)
  settings  — clé/valeur générique (remplace une partie de sniper_settings.json
              pour ce qui doit vivre en DB plutôt qu'en JSON de config)

Toutes les tables ont des clés étrangères en mode "nullable" pour ne jamais
bloquer une saisie manuelle (un achat n'a pas forcément de listing d'origine,
une vente n'a pas forcément de build).
"""

import sqlite3
import os
import time
import json
from contextlib import contextmanager

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soldier.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model           TEXT NOT NULL,
    category        TEXT NOT NULL,
    marketplace     TEXT NOT NULL,
    price           REAL NOT NULL,
    market_price    REAL,
    estimated_margin REAL,
    scam_score      INTEGER DEFAULT 0,
    confidence_score INTEGER DEFAULT 100,
    confidence_reasons TEXT DEFAULT '[]',
    title           TEXT,
    url             TEXT UNIQUE,
    image_url       TEXT,
    status          TEXT DEFAULT 'nouveau',
    detected_at     REAL NOT NULL,
    posted_at       REAL
);

CREATE TABLE IF NOT EXISTS purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    model           TEXT NOT NULL,
    category        TEXT,
    buy_price       REAL NOT NULL,
    shipping_cost   REAL DEFAULT 0,
    buyer_protection_fee REAL DEFAULT 0,
    source          TEXT,
    condition_note  TEXT,
    status          TEXT DEFAULT 'en_route',
    build_id        INTEGER REFERENCES builds(id) ON DELETE SET NULL,
    purchase_date   REAL NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS builds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    total_cost      REAL DEFAULT 0,
    extra_costs     REAL DEFAULT 0,
    target_price    REAL,
    status          TEXT DEFAULT 'en_cours',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id     INTEGER REFERENCES purchases(id) ON DELETE CASCADE,
    build_id        INTEGER REFERENCES builds(id) ON DELETE CASCADE,
    sale_price      REAL NOT NULL,
    platform        TEXT,
    fees            REAL DEFAULT 0,
    net_margin      REAL,
    sale_date       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_detected ON listings(detected_at);
CREATE INDEX IF NOT EXISTS idx_purchases_status ON purchases(status);
CREATE INDEX IF NOT EXISTS idx_purchases_build ON purchases(build_id);
"""

# Colonnes ajoutées après la première version du schéma. ALTER TABLE ADD COLUMN
# est idempotent ici via try/except (SQLite n'a pas de "ADD COLUMN IF NOT EXISTS"
# avant 3.35 côté DDL générique) — sûr à rejouer à chaque démarrage, ne touche
# jamais aux données existantes.
MIGRATIONS = [
    "ALTER TABLE purchases ADD COLUMN deleted_at REAL",
    "ALTER TABLE purchases ADD COLUMN tags TEXT DEFAULT ''",
    "ALTER TABLE purchases ADD COLUMN image_url TEXT",
    "ALTER TABLE builds ADD COLUMN deleted_at REAL",
    "ALTER TABLE builds ADD COLUMN tags TEXT DEFAULT ''",
    "ALTER TABLE builds ADD COLUMN notes TEXT",
]


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # colonne déjà présente


# ─────────────────────── KV SETTINGS (onboarding, préférences, budget vision) ───────────────────────
def get_kv(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]


def set_kv(key, value):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO kv_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, json.dumps(value, ensure_ascii=False)))


def is_onboarded():
    return bool(get_kv("onboarded", False))


def set_onboarded(value=True):
    set_kv("onboarded", bool(value))


# ─────────────────────── LISTINGS (scanner -> DB) ───────────────────────
def upsert_listing(deal, confidence=None):
    """Insère ou met à jour un listing à partir d'un deal du scanner (dict
    au format déjà utilisé par app.py). `confidence` (optionnel): résultat de
    confidence.assess() — {score, reasons}."""
    conf_score = confidence["score"] if confidence else 100
    conf_reasons = json.dumps(confidence["reasons"], ensure_ascii=False) if confidence else "[]"
    with get_db() as conn:
        conn.execute("""
            INSERT INTO listings (model, category, marketplace, price, market_price,
                                  estimated_margin, scam_score, confidence_score,
                                  confidence_reasons, title, url, image_url, status,
                                  detected_at, posted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'nouveau', ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                price=excluded.price, market_price=excluded.market_price,
                estimated_margin=excluded.estimated_margin,
                confidence_score=excluded.confidence_score,
                confidence_reasons=excluded.confidence_reasons
        """, (
            deal.get("model"), deal.get("category"), deal.get("source"),
            deal.get("price"), deal.get("fair"), deal.get("margin"),
            0, conf_score, conf_reasons,
            deal.get("subject"), deal.get("url"), deal.get("image"),
            deal.get("ts", time.time()), deal.get("posted_ts"),
        ))


def list_listings(status=None, min_confidence=0, limit=200):
    q = "SELECT * FROM listings WHERE confidence_score >= ?"
    params = [min_confidence]
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY detected_at DESC LIMIT ?"
    params.append(limit)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def send_listing_to_pipeline(listing_id):
    """Le handoff scanner -> pipeline en un clic: crée un achat à partir d'un
    listing, sans ressaisie manuelle, et marque le listing comme envoyé."""
    with get_db() as conn:
        listing = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not listing:
            return None
        cur = conn.execute("""
            INSERT INTO purchases (listing_id, model, category, buy_price, source,
                                   status, purchase_date, image_url)
            VALUES (?, ?, ?, ?, ?, 'en_route', ?, ?)
        """, (listing["id"], listing["model"], listing["category"], listing["price"],
              listing["marketplace"], time.time(), listing["image_url"]))
        conn.execute("UPDATE listings SET status='envoye_pipeline' WHERE id=?", (listing_id,))
        return cur.lastrowid


# ─────────────────────── PURCHASES ───────────────────────
def create_purchase(data):
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO purchases (listing_id, model, category, buy_price, shipping_cost,
                                   buyer_protection_fee, source, condition_note, status,
                                   purchase_date, notes, tags, image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("listing_id"), data["model"], data.get("category"),
            data["buy_price"], data.get("shipping_cost", 0),
            data.get("buyer_protection_fee", 0), data.get("source"),
            data.get("condition_note"), data.get("status", "en_route"),
            data.get("purchase_date", time.time()), data.get("notes"),
            data.get("tags", ""), data.get("image_url"),
        ))
        return cur.lastrowid


def update_purchase(purchase_id, data):
    fields = []
    values = []
    for key in ("model", "category", "buy_price", "shipping_cost", "buyer_protection_fee",
                "source", "condition_note", "status", "build_id", "notes", "tags", "image_url"):
        if key in data:
            fields.append(f"{key} = ?")
            values.append(data[key])
    if not fields:
        return
    values.append(purchase_id)
    with get_db() as conn:
        conn.execute(f"UPDATE purchases SET {', '.join(fields)} WHERE id = ?", values)


def list_purchases(status=None, include_deleted=False):
    q = "SELECT * FROM purchases WHERE 1=1"
    params = []
    if not include_deleted:
        q += " AND deleted_at IS NULL"
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY purchase_date DESC"
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    now = time.time()
    for r in rows:
        r["days_in_stock"] = int((now - (r.get("purchase_date") or now)) / 86400)
    return rows


def delete_purchase(purchase_id):
    """Suppression douce: l'achat part en corbeille (deleted_at posé), jamais
    supprimé en dur — restaurable via restore_purchase()."""
    with get_db() as conn:
        conn.execute("UPDATE purchases SET deleted_at = ? WHERE id = ?", (time.time(), purchase_id))


def restore_purchase(purchase_id):
    with get_db() as conn:
        conn.execute("UPDATE purchases SET deleted_at = NULL WHERE id = ?", (purchase_id,))


def purge_purchase(purchase_id):
    """Suppression définitive et irréversible — seulement depuis la corbeille."""
    with get_db() as conn:
        conn.execute("DELETE FROM purchases WHERE id = ? AND deleted_at IS NOT NULL", (purchase_id,))


def list_trash_purchases():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM purchases WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall()]


# ─────────────────────── BUILDS ───────────────────────
def create_build(name, extra_costs=0, target_price=None, tags="", notes=None):
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO builds (name, extra_costs, target_price, status, created_at, tags, notes)
            VALUES (?, ?, ?, 'en_cours', ?, ?, ?)
        """, (name, extra_costs, target_price, time.time(), tags, notes))
        return cur.lastrowid


def attach_purchase_to_build(purchase_id, build_id):
    with get_db() as conn:
        conn.execute("UPDATE purchases SET build_id = ?, status='en_build' WHERE id = ?",
                     (build_id, purchase_id))
        recompute_build_cost(build_id, conn)


def detach_purchase_from_build(purchase_id):
    with get_db() as conn:
        row = conn.execute("SELECT build_id FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        build_id = row["build_id"] if row else None
        conn.execute("UPDATE purchases SET build_id = NULL, status='recu' WHERE id = ?",
                     (purchase_id,))
        if build_id:
            recompute_build_cost(build_id, conn)


def recompute_build_cost(build_id, conn=None):
    """Recalcule le coût total d'un build = somme des achats liés + coûts additionnels."""
    def _do(c):
        row = c.execute("""
            SELECT COALESCE(SUM(buy_price + shipping_cost + buyer_protection_fee), 0) AS total
            FROM purchases WHERE build_id = ?
        """, (build_id,)).fetchone()
        extra = c.execute("SELECT extra_costs FROM builds WHERE id=?", (build_id,)).fetchone()
        extra_costs = extra["extra_costs"] if extra else 0
        c.execute("UPDATE builds SET total_cost = ? WHERE id = ?",
                  (row["total"] + extra_costs, build_id))
    if conn is not None:
        _do(conn)
    else:
        with get_db() as c:
            _do(c)


def list_builds(include_deleted=False):
    q = "SELECT * FROM builds"
    if not include_deleted:
        q += " WHERE deleted_at IS NULL"
    q += " ORDER BY created_at DESC"
    with get_db() as conn:
        builds = [dict(r) for r in conn.execute(q).fetchall()]
        for b in builds:
            comps = conn.execute("SELECT * FROM purchases WHERE build_id=? AND deleted_at IS NULL",
                                  (b["id"],)).fetchall()
            b["components"] = [dict(c) for c in comps]
        return builds


def delete_build(build_id):
    """Suppression douce: le build part en corbeille, ses composants repassent
    en stock (retirés du build mais jamais supprimés)."""
    with get_db() as conn:
        conn.execute("UPDATE purchases SET build_id=NULL, status='recu' WHERE build_id=?", (build_id,))
        conn.execute("UPDATE builds SET deleted_at = ? WHERE id=?", (time.time(), build_id))


def restore_build(build_id):
    with get_db() as conn:
        conn.execute("UPDATE builds SET deleted_at = NULL WHERE id = ?", (build_id,))


def purge_build(build_id):
    with get_db() as conn:
        conn.execute("DELETE FROM builds WHERE id = ? AND deleted_at IS NOT NULL", (build_id,))


def list_trash_builds():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM builds WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall()]


# ─────────────────────── SALES ───────────────────────
def create_sale(data):
    """data: purchase_id OU build_id (exclusif), sale_price, platform, fees."""
    cost = 0
    with get_db() as conn:
        if data.get("purchase_id"):
            p = conn.execute("SELECT * FROM purchases WHERE id=?", (data["purchase_id"],)).fetchone()
            cost = (p["buy_price"] or 0) + (p["shipping_cost"] or 0) + (p["buyer_protection_fee"] or 0)
            conn.execute("UPDATE purchases SET status='vendu' WHERE id=?", (data["purchase_id"],))
        elif data.get("build_id"):
            b = conn.execute("SELECT * FROM builds WHERE id=?", (data["build_id"],)).fetchone()
            cost = b["total_cost"] or 0
            conn.execute("UPDATE builds SET status='vendu' WHERE id=?", (data["build_id"],))

        net_margin = data["sale_price"] - cost - data.get("fees", 0)
        cur = conn.execute("""
            INSERT INTO sales (purchase_id, build_id, sale_price, platform, fees,
                               net_margin, sale_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.get("purchase_id"), data.get("build_id"), data["sale_price"],
              data.get("platform"), data.get("fees", 0), net_margin,
              data.get("sale_date", time.time())))
        return cur.lastrowid


def list_sales():
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sales ORDER BY sale_date DESC").fetchall()]


# ─────────────────────── DASHBOARD / KPIs ───────────────────────
def dashboard_kpis():
    with get_db() as conn:
        cash_engaged = conn.execute("""
            SELECT COALESCE(SUM(buy_price + shipping_cost + buyer_protection_fee), 0) AS v
            FROM purchases WHERE status != 'vendu' AND deleted_at IS NULL
        """).fetchone()["v"]

        margin_realized = conn.execute(
            "SELECT COALESCE(SUM(net_margin), 0) AS v FROM sales").fetchone()["v"]

        total_invested = conn.execute("""
            SELECT COALESCE(SUM(sale_price - net_margin), 0) AS v FROM sales
        """).fetchone()["v"]
        roi = (margin_realized / total_invested * 100) if total_invested > 0 else 0

        stock_value = conn.execute("""
            SELECT COALESCE(SUM(buy_price + shipping_cost + buyer_protection_fee), 0) AS v
            FROM purchases WHERE status != 'vendu' AND build_id IS NULL AND deleted_at IS NULL
        """).fetchone()["v"]
        stock_value += conn.execute("""
            SELECT COALESCE(SUM(total_cost), 0) AS v FROM builds
            WHERE status != 'vendu' AND deleted_at IS NULL
        """).fetchone()["v"]

        cutoff = time.time() - 86400
        deals_today = conn.execute("""
            SELECT COUNT(*) AS v FROM listings
            WHERE detected_at >= ? AND confidence_score >= 70
        """, (cutoff,)).fetchone()["v"]

        return {
            "cash_engaged": round(cash_engaged, 2),
            "margin_realized": round(margin_realized, 2),
            "roi_percent": round(roi, 1),
            "stock_value": round(stock_value, 2),
            "deals_today": deals_today,
        }


# ─────────────────────── ANALYTICS ───────────────────────
def analytics_summary():
    """Agrégats pour la page Analytics: revenu/marge par mois, marge par
    catégorie, cash-flow cumulé. Tout calculé à partir des ventes déjà
    enregistrées — pas de dépendance lourde, juste du SQL + Python."""
    with get_db() as conn:
        sales = [dict(r) for r in conn.execute(
            "SELECT * FROM sales ORDER BY sale_date ASC").fetchall()]
        purchases_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM purchases").fetchall()}
        builds_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM builds").fetchall()}

    by_month = {}
    by_category = {}
    cash_flow = []
    cumulative = 0
    for s in sales:
        month = time.strftime("%Y-%m", time.localtime(s["sale_date"]))
        m = by_month.setdefault(month, {"month": month, "revenue": 0, "margin": 0, "count": 0})
        m["revenue"] += s["sale_price"] or 0
        m["margin"] += s["net_margin"] or 0
        m["count"] += 1

        category = None
        if s.get("purchase_id") and s["purchase_id"] in purchases_by_id:
            category = purchases_by_id[s["purchase_id"]].get("category")
        elif s.get("build_id"):
            category = "Build"
        category = category or "Autre"
        c = by_category.setdefault(category, {"category": category, "margin": 0, "count": 0})
        c["margin"] += s["net_margin"] or 0
        c["count"] += 1

        cumulative += s["net_margin"] or 0
        cash_flow.append({"date": s["sale_date"], "cumulative_margin": round(cumulative, 2)})

    return {
        "by_month": sorted(by_month.values(), key=lambda r: r["month"]),
        "by_category": sorted(by_category.values(), key=lambda r: -r["margin"]),
        "cash_flow": cash_flow,
        "total_sales": len(sales),
    }


def load_demo_data():
    """Jeu de données de démo pour l'onboarding (option "voir l'app remplie").
    Purement illustratif, jamais mélangé avec de vraies données de scan."""
    now = time.time()
    day = 86400
    with get_db() as conn:
        b1 = conn.execute("""
            INSERT INTO builds (name, extra_costs, target_price, status, created_at)
            VALUES ('PC Gaming Ryzen 5600 / RTX 3060 (démo)', 15, 650, 'en_cours', ?)
        """, (now - 6 * day,)).lastrowid

        p1 = conn.execute("""
            INSERT INTO purchases (model, category, buy_price, shipping_cost, source, status,
                                   build_id, purchase_date, notes, tags)
            VALUES ('RTX 3060', 'GPU', 190, 0, 'leboncoin', 'en_build', ?, ?, 'Testée, bon état', 'demo')
        """, (b1, now - 6 * day)).lastrowid
        conn.execute("""
            INSERT INTO purchases (model, category, buy_price, shipping_cost, source, status,
                                   build_id, purchase_date, notes, tags)
            VALUES ('Ryzen 5 5600X', 'CPU', 110, 5, 'vinted', 'en_build', ?, ?, '', 'demo')
        """, (b1, now - 5 * day))
        recompute_build_cost(b1, conn)

        p3 = conn.execute("""
            INSERT INTO purchases (model, category, buy_price, shipping_cost, source, status,
                                   purchase_date, notes, tags)
            VALUES ('SSD NVMe 1To', 'STORAGE', 45, 0, 'leboncoin', 'recu', ?, '', 'demo')
        """, (now - 2 * day,)).lastrowid

        p4 = conn.execute("""
            INSERT INTO purchases (model, category, buy_price, shipping_cost, source, status,
                                   purchase_date, notes, tags)
            VALUES ('DDR4 32Go', 'RAM', 55, 0, 'ebay', 'vendu', ?, '', 'demo')
        """, (now - 20 * day,)).lastrowid
        cost4 = 55
        margin4 = 85 - cost4 - 3
        conn.execute("""
            INSERT INTO sales (purchase_id, sale_price, platform, fees, net_margin, sale_date)
            VALUES (?, 85, 'leboncoin', 3, ?, ?)
        """, (p4, margin4, now - 12 * day))

        conn.execute("""
            INSERT OR IGNORE INTO listings (model, category, marketplace, price, market_price,
                                  estimated_margin, scam_score, confidence_score, confidence_reasons,
                                  title, url, image_url, status, detected_at)
            VALUES ('RTX 4070', 'GPU', 'leboncoin', 320, 480, 160, 0, 92, '[]',
                    'RTX 4070 excellent état, facture', 'https://example.invalid/demo-listing-1',
                    '', 'nouveau', ?)
        """, (now - 3600,))

    return {"ok": True}


def migrate_from_solder_export(export_json):
    """
    Importe un export de l'ancien SOLDER (JSON: {"purchases":[...], "builds":[...]})
    vers la nouvelle base SQLite. Route d'import prévue pour ne pas perdre les
    données existantes lors de la bascule. Tolérant aux champs manquants.
    """
    data = json.loads(export_json) if isinstance(export_json, str) else export_json
    old_purchases = data.get("purchases", [])
    old_builds = data.get("builds", [])

    id_map = {}  # ancien id (str/uuid) -> nouveau id SQLite
    with get_db() as conn:
        for b in old_builds:
            cur = conn.execute("""
                INSERT INTO builds (name, extra_costs, target_price, status, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (b.get("name", "Build importé"), b.get("extraCosts", 0), b.get("targetPrice"),
                  "vendu" if b.get("status") == "sold" else "en_cours",
                  b.get("createdAt", time.time())))
            id_map[("build", b.get("id"))] = cur.lastrowid

        for p in old_purchases:
            build_id = id_map.get(("build", p.get("buildId")))
            cur = conn.execute("""
                INSERT INTO purchases (model, category, buy_price, shipping_cost,
                                       buyer_protection_fee, source, condition_note,
                                       status, build_id, purchase_date, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p.get("name", p.get("model", "Article importé")), p.get("category"),
                p.get("price", 0), p.get("shipping", 0), p.get("buyerProtectionFee", 0),
                p.get("platform"), p.get("condition"),
                "vendu" if p.get("status") == "sold" else ("en_build" if build_id else "recu"),
                build_id, p.get("date", time.time()), p.get("notes"),
            ))
            new_id = cur.lastrowid
            id_map[("purchase", p.get("id"))] = new_id
            sale = p.get("sale")
            if sale:
                cost = (p.get("price", 0) + p.get("shipping", 0) + p.get("buyerProtectionFee", 0))
                net_margin = sale.get("price", 0) - cost - sale.get("fees", 0)
                conn.execute("""
                    INSERT INTO sales (purchase_id, sale_price, platform, fees, net_margin, sale_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (new_id, sale.get("price", 0), sale.get("platform"),
                      sale.get("fees", 0), net_margin, sale.get("date", time.time())))

        for build_id in set(v for k, v in id_map.items() if k[0] == "build"):
            recompute_build_cost(build_id, conn)

    return {"purchases_imported": len(old_purchases), "builds_imported": len(old_builds)}
