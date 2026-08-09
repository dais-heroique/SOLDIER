"""
ram_calibration.py — Recalibrage des prix de référence
═══════════════════════════════════════════════════════════════════════════
Job quotidien. Collecte des ventes RÉELLEMENT CONCLUES (Vinted vendu sur 30
jours, eBay ventes terminées FR/DE), les regroupe par part number, et remplace
le prix de référence par la médiane observée.

Pourquoi la médiane et pas la moyenne : un seul lot bradé ou une seule annonce
de collectionneur déplace une moyenne de 20 %. La médiane encaisse ça sans
broncher, ce qui compte quand l'échantillon fait 4 ventes.

⚠️ RECALIBRAGE HEBDOMADAIRE OBLIGATOIRE. En contexte de pénurie DRAM les prix
montent vite et dans un seul sens. Un prix de référence vieux d'un mois fait
rater des affaires correctes — on croit payer trop cher ce qui est devenu le
prix du marché. Toute référence non recalibrée depuis 14 jours est signalée au
dashboard (v_ram_reference_perimee).

Garde-fou : un recalibrage qui ferait bouger le prix de plus de
`calibrage.variation_max_pct` d'un coup est plafonné et signalé. Un lot de 10
barrettes vendu 15 € pièce ne doit pas écraser une référence à 115 €.
"""

import statistics
import time
from datetime import date

import ram_config
import ram_db
import ram_parser

try:
    from vinted_client import VintedClient, VintedError
    _HAS_VINTED = True
except Exception:                                       # pragma: no cover
    _HAS_VINTED = False

    class VintedError(Exception):
        pass

try:
    from ebay_client import EbayClient, EbayError
    _HAS_EBAY = True
except Exception:                                       # pragma: no cover
    _HAS_EBAY = False

    class EbayError(Exception):
        pass


# ─────────────────────── COLLECTE ───────────────────────
def collecter_vinted_vendus(mots_cles=None, cfg=None, verbose=True):
    """Vinted n'expose pas d'API « articles vendus » exploitable directement.
    On collecte donc les annonces vues et on considère comme vendue toute
    annonce déjà connue qui a disparu du catalogue : c'est le meilleur proxy
    accessible sans compte vendeur, et il est fiable à quelques jours près.

    Retourne le nombre d'observations enregistrées.
    """
    cfg = cfg or ram_config.get()
    if not _HAS_VINTED:
        if verbose:
            print("[calibrage] client Vinted indisponible")
        return 0

    fenetre = int(cfg.val("calibrage.fenetre_jours", 30))
    limite = time.time() - fenetre * 86400
    enregistrees = 0

    with ram_db.get_db() as conn:
        candidates = [dict(r) for r in conn.execute("""
            SELECT * FROM ram_annonce
            WHERE source='vinted' AND encore_en_ligne=1 AND ref_id IS NOT NULL
              AND detecte_le > ? AND vue_le < ?
            ORDER BY vue_le ASC LIMIT 200
        """, (limite, time.time() - 2 * 86400)).fetchall()]

    client = VintedClient(country="FR")
    for annonce in candidates:
        try:
            resultats = client.search(search_text=(annonce.get("pn_detecte")
                                                   or annonce.get("titre", ""))[:60])
            urls = {r.get("url") for r in (resultats or [])}
        except VintedError as e:
            if verbose:
                print(f"[calibrage] Vinted : {e}")
            break
        except Exception:
            continue

        if annonce["url"] in urls:
            ram_db.maj_annonce(annonce["id"], {"vue_le": time.time()})
            continue

        # Disparue du catalogue → considérée vendue au dernier prix affiché.
        ram_db.maj_annonce(annonce["id"], {"encore_en_ligne": 0})
        if ram_db.enregistrer_observation({
                "ref_id": annonce["ref_id"], "part_number": annonce.get("pn_detecte"),
                "source": "vinted_vendu", "prix": annonce["prix_affiche"],
                "frais_port": annonce.get("frais_port"),
                "prix_net_vendeur": annonce["prix_affiche"],
                "capacite_module_go": annonce.get("capacite_module_go"),
                "nb_modules": annonce.get("nb_modules"),
                "frequence_mhz": annonce.get("frequence_mhz"),
                "cas_latency": annonce.get("cas_latency"),
                "url": annonce["url"], "vendu_le": time.time()}):
            enregistrees += 1
        time.sleep(1.5)

    if verbose:
        print(f"[calibrage] {enregistrees} vente(s) Vinted enregistrée(s)")
    return enregistrees


def collecter_ebay_termines(mots_cles=None, cfg=None, verbose=True):
    """Ventes eBay terminées FR/DE. L'Allemagne compte : c'est le marché
    directeur de la DDR4 d'occasion en Europe, et il précède le marché
    français de quelques semaines."""
    cfg = cfg or ram_config.get()
    if not _HAS_EBAY:
        if verbose:
            print("[calibrage] client eBay indisponible (clés API absentes ?)")
        return 0

    mots_cles = mots_cles or ["ddr4 3200 16gb", "ddr4 3600 32gb", "corsair vengeance ddr4",
                              "g.skill ripjaws ddr4", "crucial ballistix ddr4"]
    enregistrees = 0
    try:
        client = EbayClient()
    except Exception as e:
        if verbose:
            print(f"[calibrage] eBay indisponible : {e}")
        return 0

    for mot in mots_cles:
        try:
            items = client.search(mot, limit=50) or []
        except EbayError as e:
            if verbose:
                print(f"[calibrage] eBay « {mot} » : {e}")
            continue
        except Exception:
            continue

        for item in items:
            titre = item.get("subject") or item.get("title") or ""
            prix = float(item.get("price") or 0)
            if prix <= 0:
                continue
            analyse = ram_parser.analyser(titre, item.get("description", ""), 0, cfg)
            if analyse.get("exclusion") or not analyse.get("ref"):
                continue
            if ram_db.enregistrer_observation({
                    "ref_id": analyse["ref"]["id"],
                    "part_number": analyse.get("pn_detecte"),
                    "source": "ebay_termine", "prix": prix,
                    "capacite_module_go": analyse.get("capacite_module_go"),
                    "nb_modules": analyse.get("nb_modules"),
                    "frequence_mhz": analyse.get("frequence_mhz"),
                    "cas_latency": analyse.get("cas_latency"),
                    "url": item.get("url"), "vendu_le": item.get("ts") or time.time()}):
                enregistrees += 1
        time.sleep(2.0)

    if verbose:
        print(f"[calibrage] {enregistrees} vente(s) eBay enregistrée(s)")
    return enregistrees


# ─────────────────────── CALCUL ───────────────────────
def _agreger(prix, methode="mediane"):
    if not prix:
        return None
    if methode == "moyenne_tronquee" and len(prix) >= 5:
        tries = sorted(prix)
        marge = max(1, len(tries) // 10)
        tries = tries[marge:-marge] or tries
        return round(sum(tries) / len(tries), 2)
    return round(statistics.median(prix), 2)


def recalibrer(ref_id=None, cfg=None, verbose=True, appliquer=True):
    """Recalcule les prix de référence à partir des observations.

    `appliquer=False` fait tourner à blanc : utile pour voir ce qui bougerait
    avant de laisser le job écrire en base.

    Retourne la liste des changements proposés/appliqués.
    """
    cfg = cfg or ram_config.get()
    fenetre = int(cfg.val("calibrage.fenetre_jours", 30))
    minimum = int(cfg.val("calibrage.min_observations", 3))
    methode = str(cfg.val("calibrage.methode", "mediane"))
    variation_max = float(cfg.val("calibrage.variation_max_pct", 40)) / 100.0
    depuis = time.time() - fenetre * 86400

    q = """SELECT r.id, r.part_number, r.marque, r.gamme, r.tier,
                  r.prix_ref_occasion_eur, r.prix_ref_maj_le,
                  o.prix, o.nb_modules, r.nb_modules AS ref_nb
           FROM ram_reference r
           JOIN ram_prix_observation o ON o.ref_id = r.id
           WHERE COALESCE(o.vendu_le, o.collecte_le) > ? AND r.actif = 1"""
    params = [depuis]
    if ref_id:
        q += " AND r.id = ?"
        params.append(ref_id)

    groupes = {}
    with ram_db.get_db() as conn:
        for ligne in conn.execute(q, params):
            r = dict(ligne)
            # Une observation portant sur un nombre de barrettes différent de
            # la référence est ramenée au prorata, sinon on compare un kit à
            # une barrette seule.
            prix = float(r["prix"])
            if r["nb_modules"] and r["ref_nb"] and r["nb_modules"] != r["ref_nb"]:
                prix = prix / r["nb_modules"] * r["ref_nb"]
            groupes.setdefault(r["id"], {"ref": r, "prix": []})["prix"].append(prix)

    changements = []
    for rid, groupe in groupes.items():
        ref = groupe["ref"]
        prix = groupe["prix"]
        if len(prix) < minimum:
            continue

        nouveau = _agreger(prix, methode)
        ancien = float(ref["prix_ref_occasion_eur"])
        if nouveau is None or nouveau <= 0:
            continue

        variation = (nouveau - ancien) / ancien if ancien else 0
        plafonne = False
        if abs(variation) > variation_max:
            # Garde-fou : on suit le mouvement sans se laisser emporter par un
            # échantillon aberrant. Le reste sera rattrapé au prochain passage.
            nouveau = round(ancien * (1 + variation_max * (1 if variation > 0 else -1)), 2)
            plafonne = True

        changement = {
            "ref_id": rid, "part_number": ref["part_number"],
            "marque": ref["marque"], "gamme": ref["gamme"], "tier": ref["tier"],
            "ancien": ancien, "nouveau": nouveau,
            "variation_pct": round((nouveau - ancien) / ancien * 100, 1) if ancien else 0,
            "n_observations": len(prix), "plafonne": plafonne,
        }
        if appliquer:
            ram_db.maj_prix_reference(rid, nouveau, "vinted_vendu", len(prix))
        changements.append(changement)

    changements.sort(key=lambda c: abs(c["variation_pct"]), reverse=True)
    if verbose:
        mode = "appliqué" if appliquer else "simulation"
        print(f"── Recalibrage ({mode}) : {len(changements)} référence(s) ──")
        for c in changements[:25]:
            drapeau = " ⚠️ plafonné" if c["plafonne"] else ""
            print(f"  {c['part_number']:<24} {c['ancien']:>6.0f}€ → {c['nouveau']:>6.0f}€ "
                  f"({c['variation_pct']:+.0f}% sur {c['n_observations']} ventes){drapeau}")
        if not changements:
            print("  (aucune référence n'atteint le minimum d'observations)")
    return changements


def alerte_calibrage(cfg=None):
    """Ce que le dashboard affiche : références périmées, et depuis quand."""
    cfg = cfg or ram_config.get()
    jours = int(cfg.val("calibrage.alerte_perime_jours", 14))
    perimees = ram_db.references_perimees(jours)
    total = len(ram_db.list_references(limit=10000))
    return {
        "seuil_jours": jours,
        "perimees": len(perimees),
        "total": total,
        "part_pct": round(len(perimees) / total * 100, 1) if total else 0.0,
        "alerte": len(perimees) > 0,
        "pire": perimees[:10],
    }


def job_quotidien(cfg=None, verbose=True):
    """Enchaîne collecte puis recalibrage. Appelé par le planificateur de
    ram_sniper.py à l'heure configurée (calibrage.heure_job)."""
    cfg = cfg or ram_config.get()
    if not cfg.val("calibrage.actif", True):
        if verbose:
            print("[calibrage] désactivé dans la configuration")
        return {"actif": False}

    debut = time.time()
    n_vinted = collecter_vinted_vendus(cfg=cfg, verbose=verbose)
    n_ebay = collecter_ebay_termines(cfg=cfg, verbose=verbose)
    changements = recalibrer(cfg=cfg, verbose=verbose)
    alerte = alerte_calibrage(cfg)

    resultat = {
        "actif": True, "date": date.today().isoformat(),
        "observations_vinted": n_vinted, "observations_ebay": n_ebay,
        "references_recalibrees": len(changements),
        "references_perimees": alerte["perimees"],
        "duree_s": round(time.time() - debut, 1),
    }
    if verbose:
        print(f"[calibrage] terminé en {resultat['duree_s']}s — "
              f"{resultat['references_recalibrees']} référence(s) mise(s) à jour, "
              f"{alerte['perimees']} encore périmée(s)")
    return resultat


if __name__ == "__main__":
    import sys
    ram_db.init_db()

    if "--simuler" in sys.argv:
        # Injecte des ventes fictives pour montrer le mécanisme sans réseau.
        ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
        print(f"Référence de test : {ref['part_number']} à "
              f"{ref['prix_ref_occasion_eur']:.0f}€\n")
        for i, prix in enumerate((128, 132, 125, 139, 130)):
            ram_db.enregistrer_observation({
                "ref_id": ref["id"], "part_number": ref["part_number"],
                "source": "vinted_vendu", "prix": prix, "nb_modules": 2,
                "url": f"https://vinted.fr/vendu/{time.time()}-{i}",
                "vendu_le": time.time() - i * 86400})
        print("5 ventes injectées entre 125 et 139 €\n")
        recalibrer(ref_id=ref["id"], appliquer=False)
        print()
        recalibrer(ref_id=ref["id"], appliquer=True)
        maj = ram_db.get_reference(ref["id"])
        print(f"\nAprès application : {maj['prix_ref_occasion_eur']}€ "
              f"(source {maj['prix_ref_source']}, {maj['prix_ref_n_ventes']} ventes, "
              f"maj {maj['prix_ref_maj_le']})")
        sys.exit(0)

    alerte = alerte_calibrage()
    print(f"── Calibrage ── seuil {alerte['seuil_jours']} jours")
    print(f"  {alerte['perimees']}/{alerte['total']} référence(s) périmée(s) "
          f"({alerte['part_pct']}%)")
    for r in alerte["pire"]:
        print(f"    {r['part_number']:<24} maj {r['prix_ref_maj_le']} "
              f"({r['jours_depuis_calibrage']}j)")
    if "--run" in sys.argv:
        job_quotidien()
