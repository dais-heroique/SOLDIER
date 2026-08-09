"""
ram_scrapers.py — Collecte Vinted + Leboncoin
═══════════════════════════════════════════════════════════════════════════
Objectif explicite : TENIR 6 MOIS SANS BLACKLIST, pas scraper le plus vite
possible. Concrètement :

  • Requêtes ÉTALÉES, jamais en rafale — un délai aléatoire entre chaque
    requête, et un délai plus long entre deux passages sur le même mot-clé.
  • Backoff exponentiel sur 429/403, avec mise en quarantaine du mot-clé
    fautif plutôt que du scraper entier.
  • Réutilisation des clients existants de SOLDIER (vinted_client.VintedClient,
    module lbc) : le durcissement TLS/cookies y est déjà fait.
  • Toute annonce vue est enregistrée, même rejetée : c'est la matière du
    mode --replay et du réglage du scoring.

Les clients réseau sont importés de façon défensive : le module doit rester
importable (et testable) sur une machine où ni `lbc` ni `curl_cffi` ne sont
installés.
"""

import json
import random
import re
import time

import ram_config
import ram_db
import ram_parser
import ram_scoring

try:
    from vinted_client import VintedClient, VintedError
    _HAS_VINTED = True
except Exception as e:                                  # pragma: no cover
    _HAS_VINTED = False
    _ERR_VINTED = str(e)

    class VintedError(Exception):
        pass

try:
    import lbc
    _HAS_LBC = True
except Exception as e:                                  # pragma: no cover
    _HAS_LBC = False
    _ERR_LBC = str(e)


class Quarantaine:
    """Un mot-clé qui prend un 429 est mis de côté un moment, mais le scraper
    continue sur les autres. Bloquer tout le scraper parce qu'un terme est trop
    sollicité serait se punir soi-même."""

    def __init__(self, paliers):
        self.paliers = list(paliers) or [60, 120, 300, 600]
        self.echecs = {}
        self.jusqua = {}

    def bloque(self, cle):
        return time.time() < self.jusqua.get(cle, 0)

    def echec(self, cle):
        n = self.echecs.get(cle, 0)
        delai = self.paliers[min(n, len(self.paliers) - 1)]
        self.echecs[cle] = n + 1
        self.jusqua[cle] = time.time() + delai
        return delai

    def succes(self, cle):
        self.echecs.pop(cle, None)
        self.jusqua.pop(cle, None)

    def reste(self, cle):
        return max(0, int(self.jusqua.get(cle, 0) - time.time()))


def _pause(intervalle):
    lo, hi = (intervalle if isinstance(intervalle, (list, tuple)) and len(intervalle) == 2
              else (2.0, 4.0))
    time.sleep(random.uniform(float(lo), float(hi)))


# ─────────────────────── TRAITEMENT COMMUN ───────────────────────
def traiter_annonce(brut, source, mot_cle=None, cfg=None):
    """Chaîne complète pour une annonce brute : parsing → frais → pré-score →
    écriture en base.

    Retourne un dict de résultat :
        {annonce, analyse, pre, nouvelle, notifiable, a_analyser}
    ou None si l'annonce n'est pas exploitable du tout.
    """
    cfg = cfg or ram_config.get()

    url = brut.get("url")
    titre = brut.get("titre") or brut.get("subject") or ""
    if not url or not titre:
        return None

    description = brut.get("description") or ""
    photos = brut.get("photos") or ([brut["image"]] if brut.get("image") else [])
    prix = float(brut.get("prix") or brut.get("price") or 0)
    if prix <= 0:
        return None

    analyse = ram_parser.analyser(titre, description, len(photos), cfg)

    port, protection, total = ram_scoring.frais_acquisition(
        source, prix, port_connu=brut.get("frais_port"),
        main_propre=analyse.get("main_propre") or brut.get("main_propre"), cfg=cfg)

    annonce_data = {
        "source": source, "source_id": str(brut.get("source_id") or ""), "url": url,
        "titre": titre, "description": description[:2000], "mot_cle": mot_cle,
        "prix_affiche": prix, "frais_port": port, "frais_protection": protection,
        "main_propre": analyse.get("main_propre"),
        "vendeur_pseudo": brut.get("vendeur_pseudo"),
        "vendeur_note": brut.get("vendeur_note"),
        "vendeur_ventes": brut.get("vendeur_ventes"),
        "localisation": brut.get("localisation"),
        "code_postal": brut.get("code_postal"),
        "departement": brut.get("departement"),
        "photos": photos, "publie_le": brut.get("publie_le"),
        "pn_detecte": analyse.get("pn_detecte"),
        "pn_normalise": analyse.get("pn_normalise"),
        "marque_detectee": analyse.get("marque_detectee"),
        "gamme_detectee": analyse.get("gamme_detectee"),
        "capacite_module_go": analyse.get("capacite_module_go"),
        "nb_modules": analyse.get("nb_modules"),
        "capacite_totale_go": analyse.get("capacite_totale_go"),
        "frequence_mhz": analyse.get("frequence_mhz"),
        "cas_latency": analyse.get("cas_latency"),
        "ref_id": analyse.get("ref_id"), "tier": analyse.get("tier"),
        "est_kit": analyse.get("est_kit"),
        "confiance_texte": analyse.get("confiance_texte"),
        "drapeaux": analyse.get("drapeaux"),
        "brut": brut,
    }

    annonce_id, nouvelle = ram_db.upsert_annonce(annonce_data)
    annonce = ram_db.get_annonce(annonce_id)

    # Une annonce déjà traitée n'est pas re-scorée : ses verdicts sont acquis.
    if not nouvelle:
        return {"annonce": annonce, "analyse": analyse, "pre": None,
                "nouvelle": False, "notifiable": False, "a_analyser": False}

    pre = ram_scoring.pre_score(annonce, analyse, cfg)

    maj = {
        "pre_score": pre["pre_score"], "revente_estimee": pre["revente_estimee"],
        "marge_estimee": pre["marge_estimee"], "marge_pct": pre["marge_pct"],
        "qualite_annonce": pre["qualite_annonce"],
        "score_vendeur": pre["score_vendeur"],
        "score_logistique": pre["score_logistique"],
        "exclusion": pre["exclusion"], "rejet_motif": pre["rejet_motif"],
    }
    if pre["exclusion"]:
        maj["statut"] = "rejete"
        maj["statut_verif"] = "rejete"
    ram_db.maj_annonce(annonce_id, maj)
    annonce.update(maj)

    if pre["exclusion"]:
        ram_db.journaliser("auto_rejet", annonce, motif=pre["rejet_motif"],
                           decide_par="auto")
        return {"annonce": annonce, "analyse": analyse, "pre": pre, "nouvelle": True,
                "notifiable": False, "a_analyser": False}

    seuil_notif = float(cfg.val("scoring.seuil_notification", 65))
    seuil_vision = float(cfg.val("scoring.seuil_vision", 55))
    score = pre["pre_score"]

    return {
        "annonce": annonce, "analyse": analyse, "pre": pre, "nouvelle": True,
        "notifiable": score >= seuil_notif,
        "a_analyser": score >= seuil_vision and bool(photos)
                      and bool(cfg.val("vision.actif", True)),
    }


# ─────────────────────── VINTED ───────────────────────
class ScraperVinted:
    """Priorité 1 : vendeurs peu informés, achat immédiat, meilleur terrain.

    Frais à intégrer systématiquement : protection acheteur ≈ 5 % + 0,70 €,
    port 2-5 €. Un prix affiché Vinted n'est jamais le prix payé.
    """

    source = "vinted"

    def __init__(self, cfg=None, verbose=False):
        self.cfg = cfg or ram_config.get()
        self.verbose = verbose
        self.client = None
        self.quarantaine = Quarantaine(
            self.cfg.val("sources.vinted.backoff_429_s", [60, 120, 300, 600]))
        self._dernier_passage = {}

    def _connecter(self):
        if self.client is None:
            if not _HAS_VINTED:
                raise VintedError(f"client Vinted indisponible : {_ERR_VINTED}")
            self.client = VintedClient(country="FR", verbose=self.verbose)
        return self.client

    def mots_cles(self):
        return list(self.cfg.val("sources.vinted.mots_cles", []) or [])

    def _du_a_passer(self, mot_cle):
        """Le délai par mot-clé (30-45 s) est ce qui étale la charge : sans lui,
        20 mots-clés partiraient en rafale toutes les 30 secondes."""
        lo, hi = self.cfg.val("sources.vinted.delai_par_mot_cle_s", [30, 45])
        attendu = random.uniform(float(lo), float(hi))
        return (time.time() - self._dernier_passage.get(mot_cle, 0)) >= attendu

    def scanner_mot_cle(self, mot_cle):
        """Retourne la liste des résultats de traiter_annonce()."""
        if self.quarantaine.bloque(mot_cle):
            return []
        client = self._connecter()
        self._dernier_passage[mot_cle] = time.time()
        limite = int(self.cfg.val("sources.vinted.max_resultats_par_requete", 20))

        try:
            items = client.search(search_text=mot_cle) or []
        except VintedError as e:
            statut = getattr(e, "status", None)
            if statut in (429, 403, 401):
                delai = self.quarantaine.echec(mot_cle)
                print(f"[vinted] « {mot_cle} » HTTP {statut} → quarantaine {delai}s")
                ram_db.maj_scan_stat(self.source, mot_cle, requetes=1, erreurs=1,
                                     **({"http_429": 1} if statut == 429 else {"http_403": 1}))
            else:
                print(f"[vinted] « {mot_cle} » : {e}")
                ram_db.maj_scan_stat(self.source, mot_cle, requetes=1, erreurs=1)
            return []
        except Exception as e:
            print(f"[vinted] « {mot_cle} » erreur inattendue : {e}")
            ram_db.maj_scan_stat(self.source, mot_cle, requetes=1, erreurs=1)
            return []

        self.quarantaine.succes(mot_cle)
        resultats = []
        nouveaux = 0
        for item in items[:limite]:
            brut = self._normaliser(item)
            r = traiter_annonce(brut, self.source, mot_cle, self.cfg)
            if r:
                resultats.append(r)
                nouveaux += int(r["nouvelle"])
        ram_db.maj_scan_stat(self.source, mot_cle, requetes=1,
                             resultats=len(items), nouveaux=nouveaux)
        return resultats

    @staticmethod
    def _normaliser(item):
        """vinted_client._normalize() rend déjà un dict propre ; on complète
        avec les photos supplémentaires quand l'API les expose."""
        photos = []
        if item.get("image"):
            photos.append(item["image"])
        for p in (item.get("photos") or []):
            url = p.get("url") if isinstance(p, dict) else p
            if url and url not in photos:
                photos.append(url)
        return {
            "url": item.get("url"), "titre": item.get("subject") or item.get("title"),
            "description": item.get("description") or "",
            "prix": item.get("price"), "photos": photos,
            "publie_le": item.get("posted_ts"),
            "source_id": item.get("id"),
            "vendeur_pseudo": item.get("vendeur") or item.get("user_login"),
            "vendeur_note": item.get("vendeur_note"),
            "vendeur_ventes": item.get("vendeur_ventes"),
            "localisation": "Vinted",
        }


# ─────────────────────── LEBONCOIN ───────────────────────
class ScraperLeboncoin:
    """Priorité 2 : vendeurs mieux informés, MAIS retrait en main propre = 0
    frais et 0 risque de casse.

    Le vrai gisement ici n'est pas la barrette seule : ce sont les lots et les
    PC en panne dont on ne veut que la RAM. D'où les mots-clés « lot
    informatique », « pc en panne », « destockage informatique ».
    """

    source = "leboncoin"

    def __init__(self, cfg=None, verbose=False):
        self.cfg = cfg or ram_config.get()
        self.verbose = verbose
        self.client = None
        self.quarantaine = Quarantaine(
            self.cfg.val("sources.leboncoin.backoff_429_s", [120, 300, 900, 1800]))
        self._dernier_passage = {}

    def _connecter(self):
        if self.client is None:
            if not _HAS_LBC:
                raise RuntimeError(f"module lbc indisponible : {_ERR_LBC}")
            self.client = lbc.Client()
        return self.client

    def mots_cles(self):
        return list(self.cfg.val("sources.leboncoin.mots_cles", []) or [])

    def _du_a_passer(self, mot_cle):
        lo, hi = self.cfg.val("sources.leboncoin.delai_par_mot_cle_s", [60, 90])
        attendu = random.uniform(float(lo), float(hi))
        return (time.time() - self._dernier_passage.get(mot_cle, 0)) >= attendu

    def scanner_mot_cle(self, mot_cle):
        if self.quarantaine.bloque(mot_cle):
            return []
        client = self._connecter()
        self._dernier_passage[mot_cle] = time.time()
        limite = int(self.cfg.val("sources.leboncoin.max_resultats_par_requete", 35))

        try:
            res = client.search(text=mot_cle, page=1, limit=limite,
                                sort=lbc.Sort.NEWEST, ad_type=lbc.AdType.OFFER,
                                category=lbc.Category.ELECTRONIQUE_ACCESSOIRES_INFORMATIQUE)
            annonces = res.ads or []
        except Exception as e:
            nom = type(e).__name__
            if "Datadome" in nom or "429" in str(e):
                delai = self.quarantaine.echec(mot_cle)
                print(f"[lbc] « {mot_cle} » anti-bot → quarantaine {delai}s")
                ram_db.maj_scan_stat(self.source, mot_cle, requetes=1, erreurs=1, http_429=1)
            else:
                print(f"[lbc] « {mot_cle} » : {e}")
                ram_db.maj_scan_stat(self.source, mot_cle, requetes=1, erreurs=1)
            return []

        self.quarantaine.succes(mot_cle)
        departements = [str(d) for d in
                        (self.cfg.val("sources.leboncoin.departements", []) or [])]
        resultats = []
        nouveaux = retenus = 0
        for ad in annonces:
            brut = self._normaliser(ad)
            # Filtre géographique : hors zone, le retrait en main propre — le
            # seul vrai avantage de Leboncoin — disparaît.
            if departements and brut.get("departement") and \
                    brut["departement"] not in departements:
                continue
            retenus += 1
            r = traiter_annonce(brut, self.source, mot_cle, self.cfg)
            if r:
                resultats.append(r)
                nouveaux += int(r["nouvelle"])
        ram_db.maj_scan_stat(self.source, mot_cle, requetes=1,
                             resultats=retenus, nouveaux=nouveaux)
        return resultats

    @staticmethod
    def _normaliser(ad):
        photos = []
        for im in (getattr(ad, "images", None) or []):
            url = getattr(im, "url", None) or (im if isinstance(im, str) else None)
            if url:
                photos.append(url)

        code_postal = departement = ville = None
        loc = getattr(ad, "location", None)
        if loc is not None:
            code_postal = str(getattr(loc, "zipcode", "") or "") or None
            ville = getattr(loc, "city", None)
            if code_postal and len(code_postal) >= 2:
                departement = code_postal[:2]

        publie = getattr(ad, "first_publication_date", None)
        publie_ts = None
        if publie:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    publie_ts = time.mktime(time.strptime(str(publie)[:19], fmt))
                    break
                except (ValueError, TypeError):
                    continue

        corps = getattr(ad, "body", "") or ""
        return {
            "url": getattr(ad, "url", None), "titre": getattr(ad, "subject", None),
            "description": corps, "prix": getattr(ad, "price", 0),
            "photos": photos, "publie_le": publie_ts,
            "source_id": getattr(ad, "list_id", None) or getattr(ad, "id", None),
            "localisation": ville, "code_postal": code_postal,
            "departement": departement,
            "main_propre": _lbc_main_propre(getattr(ad, "subject", ""), corps),
        }


_ENVOI_POSSIBLE = re.compile(
    r"\b(envoi|expedition|exp[ée]dition|colissimo|mondial relay|remise en main propre "
    r"ou envoi|livraison)\b", re.I)
_MAIN_PROPRE_SEUL = re.compile(
    r"\b(uniquement (en )?main propre|pas d.envoi|remise en main propre uniquement|"
    r"sur place uniquement|aucun envoi)\b", re.I)


def _lbc_main_propre(titre, corps):
    """Main propre STRICTE seulement. « remise en main propre ou envoi » n'est
    pas du retrait : compter 0 € de port dans ce cas surestimerait la marge."""
    texte = f"{titre} {corps}"
    if _MAIN_PROPRE_SEUL.search(texte):
        return True
    if _ENVOI_POSSIBLE.search(texte):
        return False
    return bool(re.search(r"\bmain propre\b", texte, re.I))


# ─────────────────────── REPLAY ───────────────────────
def rejouer(limite=500, cfg=None, verbose=True):
    """Rejoue les annonces archivées à travers le scoring courant.

    C'est l'outil de calibrage : on modifie un seuil dans le YAML, on rejoue,
    et on voit immédiatement combien d'annonces auraient été notifiées et à
    quel score — sans attendre une semaine de collecte.
    """
    cfg = cfg or ram_config.get()
    with ram_db.get_db() as conn:
        lignes = [dict(r) for r in conn.execute(
            "SELECT * FROM ram_annonce ORDER BY detecte_le DESC LIMIT ?",
            (limite,)).fetchall()]

    stats = {"total": 0, "rejetees": 0, "notifiables": 0, "vision": 0,
             "scores": [], "par_exclusion": {}}
    for annonce in lignes:
        photos = []
        try:
            photos = json.loads(annonce.get("photos") or "[]")
        except (ValueError, TypeError):
            pass
        analyse = ram_parser.analyser(annonce.get("titre"), annonce.get("description"),
                                      len(photos), cfg)
        pre = ram_scoring.pre_score(annonce, analyse, cfg)
        stats["total"] += 1
        if pre["exclusion"]:
            stats["rejetees"] += 1
            stats["par_exclusion"][pre["exclusion"]] = \
                stats["par_exclusion"].get(pre["exclusion"], 0) + 1
            continue
        stats["scores"].append(pre["pre_score"])
        if pre["pre_score"] >= float(cfg.val("scoring.seuil_notification", 65)):
            stats["notifiables"] += 1
        elif pre["pre_score"] >= float(cfg.val("scoring.seuil_vision", 55)):
            stats["vision"] += 1

    if stats["scores"]:
        tries = sorted(stats["scores"])
        stats["score_median"] = tries[len(tries) // 2]
        stats["score_max"] = tries[-1]
        stats["score_moyen"] = round(sum(tries) / len(tries), 1)

    if verbose:
        print(f"── Replay sur {stats['total']} annonce(s) ──")
        print(f"  rejetées      : {stats['rejetees']}")
        for motif, n in sorted(stats["par_exclusion"].items(), key=lambda x: -x[1]):
            print(f"      {motif:<18} {n}")
        print(f"  notifiables   : {stats['notifiables']}")
        print(f"  → vision seule: {stats['vision']}")
        if stats["scores"]:
            print(f"  score médian  : {stats['score_median']} "
                  f"(moyen {stats['score_moyen']}, max {stats['score_max']})")
    return stats


def etat_sources(cfg=None):
    """Disponibilité réelle des clients réseau, pour le dashboard."""
    cfg = cfg or ram_config.get()
    return {
        "vinted": {"actif": bool(cfg.val("sources.vinted.actif", True)),
                   "client": _HAS_VINTED,
                   "erreur": None if _HAS_VINTED else _ERR_VINTED,
                   "mots_cles": len(cfg.val("sources.vinted.mots_cles", []) or [])},
        "leboncoin": {"actif": bool(cfg.val("sources.leboncoin.actif", True)),
                      "client": _HAS_LBC,
                      "erreur": None if _HAS_LBC else _ERR_LBC,
                      "mots_cles": len(cfg.val("sources.leboncoin.mots_cles", []) or []),
                      "departements": cfg.val("sources.leboncoin.departements", [])},
    }


if __name__ == "__main__":
    import sys
    ram_db.init_db()

    if "--replay" in sys.argv:
        rejouer()
        sys.exit(0)

    print("── État des sources ──")
    for nom, etat in etat_sources().items():
        dispo = "✅" if etat["client"] else "❌"
        print(f"  {dispo} {nom:<12} actif={etat['actif']} "
              f"mots-clés={etat['mots_cles']}"
              + (f"  ({etat['erreur']})" if etat["erreur"] else ""))

    print("\n── Test du pipeline sans réseau ──")
    faux = {
        "url": "https://www.vinted.fr/items/test-ram-sniper-1",
        "titre": "Kit RAM DDR4 32Go Corsair Vengeance LPX 3200",
        "description": "CMK32GX4M2E3200C16, 2x16Go, testé memtest, boîte d'origine",
        "prix": 62.0, "photos": ["https://images.vinted.net/a_800x600.jpg"],
        "vendeur_note": 4.8, "vendeur_ventes": 64,
    }
    r = traiter_annonce(faux, "vinted", "ddr4 32go")
    if r:
        a, pre = r["annonce"], r["pre"]
        print(f"  {a['titre']}")
        print(f"  prix total {a['prix_total']}€ (dont {a['frais_protection']}€ protection)")
        if pre:
            print(f"  pré-score {pre['pre_score']} · revente {pre['revente_estimee']}€ "
                  f"· marge {pre['marge_estimee']}€ ({pre['marge_pct']}%)")
        print(f"  notifiable={r['notifiable']}  à analyser={r['a_analyser']}")

    print()
    rejouer(verbose=True)
