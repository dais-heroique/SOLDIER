"""
test_smoke.py — Smoke test de bout en bout pour SOLDIER
═══════════════════════════════════════════════════════════════════════
Ne touche jamais à soldier.db (le vrai fichier de l'utilisateur): la base
est redirigée vers un fichier temporaire avant tout import du reste de
l'app, et supprimée à la fin.

Couvre le flux principal:
  scan simulé -> listing en base -> envoi pipeline -> achat -> build ->
  vente -> vérification des KPIs
et des cas d'erreur:
  payload invalide (modèle manquant, prix négatif, sale sans cible),
  vision API sans clé/sans image (dégradation silencieuse), doublon d'URL.

Lancer avec: venv/bin/python3 test_smoke.py
Sort avec le code 0 si tout passe, 1 sinon.
"""
import os
import sys
import tempfile
import time

# Redirige la DB AVANT d'importer soldier_db/app, pour ne jamais toucher
# au vrai soldier.db de l'utilisateur pendant le test.
_TMP_DB = tempfile.NamedTemporaryFile(prefix="soldier_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SNIPER_NO_VINTED"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soldier_db  # noqa: E402
soldier_db.DB_FILE = _TMP_DB.name

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} — {detail}")


def section(title):
    print(f"\n=== {title} ===")


def main():
    soldier_db.init_db()

    # ─────────────────────── FLUX PRINCIPAL ───────────────────────
    section("Flux principal: scan -> listing -> pipeline -> achat -> build -> vente -> KPIs")

    fake_deal = {
        "model": "RTX 3070", "category": "GPU", "source": "leboncoin",
        "price": 220, "fair": 280, "margin": 60,
        "subject": "RTX 3070 excellent état, facture", "url": "https://example.invalid/smoke-1",
        "image": "", "ts": time.time(), "posted_ts": time.time(),
    }
    soldier_db.upsert_listing(fake_deal, confidence={"score": 91, "reasons": []})
    listings = soldier_db.list_listings()
    listing = next((l for l in listings if l["url"] == fake_deal["url"]), None)
    check("listing créé depuis un deal simulé", listing is not None)
    check("confidence_score persisté", listing and listing["confidence_score"] == 91)

    purchase_id = soldier_db.send_listing_to_pipeline(listing["id"])
    check("handoff pipeline -> achat", purchase_id is not None)
    listing_after = next(l for l in soldier_db.list_listings(status="envoye_pipeline")
                          if l["id"] == listing["id"])
    check("listing marqué envoyé", listing_after["status"] == "envoye_pipeline")

    purchases = soldier_db.list_purchases()
    check("achat visible dans list_purchases", any(p["id"] == purchase_id for p in purchases))
    p = next(p for p in purchases if p["id"] == purchase_id)
    check("aging (days_in_stock) calculé", "days_in_stock" in p and p["days_in_stock"] >= 0)

    build_id = soldier_db.create_build("Build smoke test", extra_costs=10, target_price=400)
    soldier_db.attach_purchase_to_build(purchase_id, build_id)
    builds = soldier_db.list_builds()
    b = next(bd for bd in builds if bd["id"] == build_id)
    check("achat attaché au build", any(c["id"] == purchase_id for c in b["components"]))
    check("coût du build recalculé (buy_price + extra)", b["total_cost"] == 220 + 10)

    sale_id = soldier_db.create_sale({"build_id": build_id, "sale_price": 320, "platform": "leboncoin", "fees": 8})
    check("vente créée", sale_id is not None)
    build_after = next(bd for bd in soldier_db.list_builds() if bd["id"] == build_id)
    check("statut build auto -> vendu après vente", build_after["status"] == "vendu")
    sales = soldier_db.list_sales()
    s = next(s for s in sales if s["id"] == sale_id)
    check("marge nette calculée correctement", s["net_margin"] == 320 - (220 + 10) - 8)

    kpis = soldier_db.dashboard_kpis()
    check("dashboard_kpis retourne margin_realized cohérent", kpis["margin_realized"] == s["net_margin"])
    check("dashboard_kpis retourne stock_value numérique", isinstance(kpis["stock_value"], (int, float)))

    # ─────────────────────── SOFT-DELETE / CORBEILLE ───────────────────────
    section("Suppression douce (corbeille) et restauration")
    p2_id = soldier_db.create_purchase({"model": "Test soft-delete", "buy_price": 50})
    soldier_db.delete_purchase(p2_id)
    check("achat absent de list_purchases après suppression douce",
          not any(p["id"] == p2_id for p in soldier_db.list_purchases()))
    check("achat présent dans la corbeille",
          any(p["id"] == p2_id for p in soldier_db.list_trash_purchases()))
    soldier_db.restore_purchase(p2_id)
    check("achat restauré depuis la corbeille",
          any(p["id"] == p2_id for p in soldier_db.list_purchases()))
    soldier_db.delete_purchase(p2_id)
    soldier_db.purge_purchase(p2_id)
    check("achat purgé définitivement (plus dans la corbeille)",
          not any(p["id"] == p2_id for p in soldier_db.list_trash_purchases()))

    # ─────────────────────── DÉDOUBLONNAGE ───────────────────────
    section("Dédoublonnage par URL")
    before = len(soldier_db.list_listings(limit=5000))
    soldier_db.upsert_listing(fake_deal, confidence={"score": 91, "reasons": []})
    soldier_db.upsert_listing(fake_deal, confidence={"score": 85, "reasons": ["test"]})
    after = len(soldier_db.list_listings(limit=5000))
    check("même URL scannée 2x de plus ne crée pas de doublon", after == before)

    # ─────────────────────── ANALYTICS ───────────────────────
    section("Analytics")
    analytics = soldier_db.analytics_summary()
    check("analytics_summary a au moins un mois de données", len(analytics["by_month"]) >= 1)
    check("analytics_summary calcule la marge par catégorie", len(analytics["by_category"]) >= 1)

    # ─────────────────────── ROUTES FLASK (validation, cas d'erreur) ───────────────────────
    section("Routes API: validation et cas d'erreur")
    import app as flask_app  # noqa: E402  (import ici: après redirection de DB_FILE)
    client = flask_app.app.test_client()

    r = client.post("/api/soldier/purchases", json={"model": "", "buy_price": 10})
    check("POST achat sans modèle -> 400", r.status_code == 400, f"status={r.status_code}")

    r = client.post("/api/soldier/purchases", json={"model": "X", "buy_price": -5})
    check("POST achat avec prix négatif -> 400", r.status_code == 400, f"status={r.status_code}")

    r = client.post("/api/soldier/purchases", json={"model": "Valide", "buy_price": 42})
    check("POST achat valide -> 200", r.status_code == 200, f"status={r.status_code}")

    r = client.post("/api/soldier/sales", json={"sale_price": 0})
    check("POST vente sans cible ni prix valide -> 400", r.status_code == 400, f"status={r.status_code}")

    r = client.post("/api/soldier/sales", json={"sale_price": 50, "purchase_id": 1, "build_id": 1})
    check("POST vente avec purchase_id ET build_id -> 400 (exclusif)", r.status_code == 400,
          f"status={r.status_code}")

    r = client.get("/api/route/inexistante")
    check("route inconnue -> 404 JSON propre (pas de stacktrace)",
          r.status_code == 404 and r.is_json, f"status={r.status_code} json={r.is_json}")

    r = client.get("/api/soldier/purchases/999999")
    check("méthode non autorisée -> réponse JSON propre (pas de stacktrace)",
          r.status_code == 405 and r.is_json, f"status={r.status_code} json={r.is_json}")

    r = client.post("/api/soldier/pipeline/send", json={})
    check("POST pipeline/send sans listing_id -> 400", r.status_code == 400, f"status={r.status_code}")

    r = client.get("/api/onboarding/status")
    check("GET onboarding/status -> 200", r.status_code == 200)

    r = client.get("/api/soldier/export/purchases.csv")
    check("export CSV achats -> 200 + content-type csv",
          r.status_code == 200 and "csv" in r.content_type, f"status={r.status_code}")

    # ─────────────────────── VISION API DÉFENSIVE ───────────────────────
    section("Vision API: dégradation propre sans clé/sans image")
    import confidence  # noqa: E402
    penalty, reason = confidence.level4_vision_check(image_url="", claimed_model="RTX 3070", api_key=None)
    check("vision sans clé API ni image -> pas de pénalité, pas de crash", penalty == 0 and reason is None)
    penalty2, reason2 = confidence.level4_vision_check(image_url="https://example.invalid/x.jpg",
                                                         claimed_model="RTX 3070", api_key=None)
    check("vision sans clé API -> dégrade proprement (pas de pénalité)", penalty2 == 0 and reason2 is None)
    status = confidence.vision_budget_status()
    check("vision_budget_status() renvoie une structure exploitable",
          set(status.keys()) >= {"enabled", "budget_eur", "spent_eur"})
    p4, r4 = confidence.maybe_run_vision_check(95, "", "RTX 3070")
    check("maybe_run_vision_check désactivé par défaut -> jamais déclenché", p4 == 0 and r4 is None)

    # ─────────────────────── RÉSILIENCE SOURCE MORTE ───────────────────────
    section("Scan résilient: une source qui plante ne bloque pas les autres")

    class BrokenClient:
        def search(self, *a, **kw):
            raise RuntimeError("source down (simulée)")

    ref = {"fair": 280, "good": 240, "steal": 180, "queries": ["rtx 3070"]}
    try:
        deals = flask_app.scan_lbc(BrokenClient(), "GPU", "RTX 3070", ref, {}, [])
        check("scan_lbc absorbe une exception de source sans lever", isinstance(deals, list))
    except Exception as e:
        check("scan_lbc absorbe une exception de source sans lever", False, str(e))

    # ─────────────────────── BACKUP ───────────────────────
    section("Sauvegarde automatique")
    flask_app.BACKUP_DIR = tempfile.mkdtemp(prefix="soldier_backup_test_")
    flask_app.backup_db()
    backups = os.listdir(flask_app.BACKUP_DIR)
    check("backup_db() produit un fichier .db", any(f.endswith(".db") for f in backups), str(backups))

    # ─────────────────────── RÉCAP ───────────────────────
    section("Récapitulatif")
    print(f"\n{len(PASSED)} test(s) passés, {len(FAILED)} échec(s).")
    if FAILED:
        print("\nÉchecs:")
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    print("\nTOUS LES TESTS SONT VERTS.")
    return 0


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        try:
            os.remove(_TMP_DB.name)
        except OSError:
            pass
    sys.exit(exit_code)
