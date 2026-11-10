"""
ram_composants.py — Processeurs, cartes graphiques, cartes mères : prix, arnaques, alertes
═══════════════════════════════════════════════════════════════════════════
Le RAM SNIPER ne cherchait que de la DDR4. Ce module applique la même méthode
aux autres composants dont SOLDIER connaît déjà les prix (market_db.py) :

  1. IDENTIFIER le modèle exact dans le titre (« Ryzen 5 5600X » ≠ « 5600 » ≠
     « i5-5600X »), et refuser les titres ambigus (deux modèles = un lot ou un
     PC complet) ;
  2. CONNAÎTRE LE PRIX : prix de référence de market_db.py + market_extra.py,
     corrigé par le prix médian réellement observé sur les annonces vues
     (prix_reference) — un prix écrit en juin ne vaut plus rien en novembre ;
  3. ÉCARTER les arnaques (arnaque.py) et les annonces qui ne sont pas le
     composant (boîte vide, HS, PC complet, portable) ;
  4. NOTIFIER sur Telegram quand le prix total (port et protection compris)
     laisse une vraie marge.

Les requêtes sont étalées : à chaque passage, seules les `requetes_par_source`
recherches les plus « en retard » partent, les modèles chers passant plus
souvent. Volume de requêtes = réglage à surveiller (README §11).
"""

import html
import random
import re
import statistics
import sys
import time
import unicodedata

import arnaque
import market_db
import market_extra
import ram_config
import ram_db
import ram_scoring

try:
    import listing_filter
except Exception:                                       # pragma: no cover
    listing_filter = None


def log(niveau, message, **champs):
    """Log structuré de ram_sniper, trouvé sans le réimporter : lancé par
    `python ram_sniper.py`, le module s'appelle __main__, et un « import
    ram_sniper » en chargerait une seconde copie."""
    for nom in ("ram_sniper", "__main__"):
        mod = sys.modules.get(nom)
        if mod is not None and hasattr(mod, "ARRET") and hasattr(mod, "log"):
            return mod.log(niveau, message, **champs)
    print(f"[{niveau}] {message} {champs}")


DEFAUTS = {
    "actif": True,
    "categories": ["CPU", "GPU", "MOBO"],
    "requetes_par_source": 6,
    # Recherches LARGES (mode continu) : triées « plus récent d'abord », elles
    # voient passer toute nouvelle carte graphique, tout processeur, toute
    # carte mère, quel que soit le modèle — celui-ci est lu dans le titre.
    "requetes_larges": ["rtx", "gtx", "carte graphique", "radeon rx",
                        "ryzen", "intel core", "processeur", "carte mere"],
    "larges_toutes_les_s": {"vinted": [90, 150], "leboncoin": [180, 300]},
    "prix_min_reference_eur": 25,
    "marge_min_eur": 15,
    "marge_min_pct": 12,
    "port_supplementaire_eur": {"GPU": 3.0, "MOBO": 2.0},
    "fenetre_jours": 30,
    "obs_min": 8,
    "melange_observe": 0.5,
    "melange_observe_estime": 0.7,
    "facteur_prix_demande": 0.93,
    "ecart_max_pct": 35,
    "avis_confiance": 30,
    "max_notifs_par_passage": 6,
    "pause_notif_s": 1.5,
    "validite_h": 6,
    "conservation_jours": 60,
    "exclusions_titre": [
        "laptop", "portable", "notebook", "macbook", "imac", "pc gamer",
        "pc gaming", "pc complet", "pc fixe", "unite centrale", "ordinateur",
        "config complete", "configuration complete", "tour complete",
        "mini pc", "bundle", "combo", "kit evolution", "kit upgrade",
        "playstation", "xbox", "ps5", "ps4", "console", "recherche", "cherche",
        "echange contre", "pour piece", "pour pieces", "hs",
    ],
}


def reglages(cfg=None):
    cfg = cfg or ram_config.get()
    r = dict(DEFAUTS)
    r.update(cfg.section("composants") or {})
    return r


# ─────────────────────── CATALOGUE ───────────────────────
class Modele:
    __slots__ = ("categorie", "nom", "fair", "good", "steal", "requetes",
                 "motifs", "estime")

    def __init__(self, categorie, nom, fair, good, steal, requetes, estime=False):
        self.categorie, self.nom = categorie, nom
        self.fair, self.good, self.steal = float(fair), float(good), float(steal)
        self.requetes = list(requetes)
        self.estime = estime
        self.motifs = _motifs(nom, self.requetes)

    @property
    def cle(self):
        return (self.categorie, self.nom)


def jetons(texte):
    """« RTX3060Ti » → ('rtx', '3060', 'ti') : lettres et chiffres séparés, sans
    accents ni ponctuation. Vendeurs et catalogue s'écrivent de dix façons
    (« i5-12400F », « i5 12400 F », « I5 12400f ») ; une fois découpés en
    jetons, ils se comparent."""
    norm = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z]+|\d+", norm.lower()))


def _motif_valide(jt):
    """Un motif doit contenir une lettre ET un chiffre. « 9600 » seul est
    refusé : il identifierait un i5-9600K comme un Ryzen 5 9600 (180 € au lieu
    de 70 €) — la fausse « affaire en or » type."""
    return (len(jt) >= 2 and any(t.isalpha() for t in jt)
            and any(t.isdigit() for t in jt))


def _motifs(nom, requetes):
    vus = []
    for texte in [nom] + list(requetes):
        jt = jetons(texte)
        if _motif_valide(jt) and jt not in vus:
            vus.append(jt)
    return vus


_CATALOGUES = {}


def catalogue(categories=None, prix_min=0):
    """Modèles de market_db.py + market_extra.py pour ces catégories."""
    cles = (tuple(categories or market_db.CATEGORIES), float(prix_min))
    if cles in _CATALOGUES:
        return _CATALOGUES[cles]
    modeles, noms = [], set()
    for cat in cles[0]:
        spec = market_db.CATEGORIES.get(cat)
        if not spec:
            continue
        for nom, d in spec["db"].items():
            noms.add((cat, nom))
            if d["fair"] >= prix_min:
                modeles.append(Modele(cat, nom, d["fair"], d["good"], d["steal"],
                                      d.get("queries") or [nom]))
    for nom, (cat, fair) in market_extra.EXTRA.items():
        if cat in cles[0] and (cat, nom) not in noms and fair >= prix_min:
            modeles.append(Modele(cat, nom, fair,
                                  round(fair * market_extra.RATIO_BON),
                                  round(fair * market_extra.RATIO_AFFAIRE),
                                  [nom], estime=True))
    _CATALOGUES[cles] = modeles
    return modeles


def identifier(titre, modeles):
    """Le modèle exact cité dans le titre, ou None.

    Le motif le plus long l'emporte (« 5600X » avant « 5600 »). Si deux
    modèles différents restent en lice — « 3070 ou 3060 Ti », « 5600X + RTX
    3060 » — le titre est ambigu : on s'abstient plutôt que de deviner."""
    jt = jetons(titre)
    trouves = []
    for m in modeles:
        for motif in m.motifs:
            n = len(motif)
            for i in range(len(jt) - n + 1):
                if jt[i:i + n] == motif:
                    trouves.append((n, i, i + n, m))
    trouves.sort(key=lambda t: -t[0])
    retenus = []
    for n, i, j, m in trouves:
        if any(i >= ri and j <= rj for _, ri, rj, _ in retenus):
            continue                            # contenu dans un motif plus long
        retenus.append((n, i, j, m))
    distincts = {t[3].cle for t in retenus}
    if len(distincts) != 1:
        return None
    return retenus[0][3]


# ─────────────────────── BASE ───────────────────────
def _ligne(url):
    with ram_db.get_db() as conn:
        r = conn.execute("SELECT * FROM ram_composant WHERE url=?", (url,)).fetchone()
        return dict(r) if r else None


def _enregistrer(d):
    champs = ("source", "url", "categorie", "modele", "titre", "prix_affiche",
              "prix_total", "reference", "marge", "niveau", "score_arnaque",
              "vendeur_avis", "statut", "motif", "vu_le")
    d = dict(d, vu_le=time.time())
    with ram_db.get_db() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO ram_composant ({','.join(champs)}) "
            f"VALUES ({','.join('?' * len(champs))})", [d.get(c) for c in champs])


def prix_observes(modele, jours=30):
    """Prix affichés (annonces valides, hors rejets) vus ces `jours` derniers jours."""
    depuis = time.time() - jours * 86400
    with ram_db.get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT prix_affiche FROM ram_composant WHERE modele=? AND vu_le>=? "
            "AND statut IN ('vue','a_notifier','notifiee','expiree') "
            "AND prix_affiche>0", (modele, depuis))]


def purger(cfg=None):
    r = reglages(cfg)
    limite = time.time() - float(r["conservation_jours"]) * 86400
    with ram_db.get_db() as conn:
        return conn.execute("DELETE FROM ram_composant WHERE vu_le<?", (limite,)).rowcount


# ─────────────────────── PRIX DE RÉFÉRENCE ───────────────────────
def prix_reference(m, cfg=None, observes=None):
    """(prix_juste, détail). Prix statique, corrigé par le marché observé.

    Avec au moins `obs_min` annonces vues, la médiane des prix demandés
    (× facteur_prix_demande : on demande plus qu'on n'obtient) est mélangée au
    prix statique, plus fortement pour les estimations de market_extra. Le
    résultat reste dans ±ecart_max_pct du prix statique : quelques annonces
    aberrantes ne peuvent pas faire dériver toute la grille."""
    r = reglages(cfg)
    obs = prix_observes(m.nom, int(r["fenetre_jours"])) if observes is None else list(observes)
    obs = [p for p in obs if 0.3 * m.fair <= p <= 2.5 * m.fair]    # annonces farfelues
    detail = {"statique": m.fair, "n": len(obs), "mediane": None, "estime": m.estime}
    if len(obs) < int(r["obs_min"]):
        return m.fair, detail
    mediane = statistics.median(obs) * float(r["facteur_prix_demande"])
    ecart = float(r["ecart_max_pct"]) / 100.0
    mediane = max(m.fair * (1 - ecart), min(m.fair * (1 + ecart), mediane))
    poids = float(r["melange_observe_estime" if m.estime else "melange_observe"])
    detail["mediane"] = round(mediane, 1)
    return round(m.fair * (1 - poids) + mediane * poids, 1), detail


# ─────────────────────── ÉVALUATION D'UNE ANNONCE ───────────────────────
def _exclusion_titre(titre, r):
    jt = " ".join(jetons(titre))
    for mot in r["exclusions_titre"]:
        if re.search(r"\b" + re.escape(" ".join(jetons(mot))) + r"\b", jt):
            return mot
    return None


def evaluer_annonce(brut, source, cfg=None, verif=None, modeles=None, refs=None):
    """Évalue UNE annonce brute. Retourne un dict
    {statut, modele, motif, ...} ou None si l'annonce est inexploitable
    (titre illisible, modèle non identifié : rien n'est écrit en base).

    statut : 'a_notifier' | 'vue' | 'rejetee' | 'deja_vue' | 'transitoire'
    verif  : fonction(brut) → dict de vendeur_avis.evaluer(), appelée
             UNIQUEMENT pour une affaire potentielle (une requête par vendeur).
    refs   : cache {modele: (prix, détail)} partagé pour tout le passage.
    """
    cfg = cfg or ram_config.get()
    r = reglages(cfg)
    url = brut.get("url")
    titre = brut.get("titre") or brut.get("subject") or ""
    try:
        prix = float(brut.get("prix") or brut.get("price") or 0)
    except (TypeError, ValueError):
        prix = 0.0
    if not url or not titre or prix <= 0:
        return None
    if _ligne(url):
        return {"statut": "deja_vue", "url": url}

    if modeles is None:
        modeles = catalogue(r["categories"], float(r["prix_min_reference_eur"]))
    m = identifier(titre, modeles)
    if m is None:
        return None

    base = {"source": source, "url": url, "categorie": m.categorie,
            "modele": m.nom, "titre": titre[:200], "prix_affiche": prix}

    def clore(statut, motif, **plus):
        d = dict(base, statut=statut, motif=motif, **plus)
        if statut != "transitoire":
            _enregistrer(d)
        return d

    mot = _exclusion_titre(titre, r)
    if mot:
        return clore("rejetee", f"titre : « {mot} » (PC complet, portable, lot…)")
    plancher = market_db.MIN_PRICE.get(m.categorie, 0)
    if prix < plancher:
        return clore("rejetee", f"prix {prix:.0f} € sous le plancher {plancher} € "
                                "(boîte vide, pièce détachée ou arnaque)")

    description = brut.get("description") or ""
    refs = refs if refs is not None else {}
    if m.cle not in refs:
        refs[m.cle] = prix_reference(m, cfg)
    reference, detail = refs[m.cle]
    echelle = reference / m.fair

    if listing_filter is not None:
        a = listing_filter.analyze(titre, description, category=m.categorie,
                                   price=prix, fair=reference)
        if not a.get("keep") and a.get("reason_key") in ("accessory", "bad_condition"):
            return clore("rejetee", a.get("reason"))

    risque = arnaque.evaluer(titre, description, prix, reference,
                             nb_photos=len(brut.get("photos") or []), cfg=cfg)
    if risque["rejeter"]:
        return clore("rejetee", f"risque d'arnaque {risque['score']}/100 : "
                                + ", ".join(risque["raisons"][:3]),
                     score_arnaque=risque["score"])

    port, protection, total = ram_scoring.frais_acquisition(
        source, prix, port_connu=brut.get("frais_port"),
        main_propre=brut.get("main_propre"), cfg=cfg)
    if not brut.get("main_propre"):
        sup = float((r["port_supplementaire_eur"] or {}).get(m.categorie, 0.0))
        total = round(total + sup, 2)
    emballage = float(cfg.val("frais.revente.emballage_eur", 1.2))
    marge = round(reference - total - emballage, 2)
    marge_pct = marge / total * 100 if total > 0 else 0.0

    resultat = dict(base, prix_total=total, reference=reference, marge=marge,
                    score_arnaque=risque["score"], detail_prix=detail)
    affaire = (total <= m.good * echelle and marge >= float(r["marge_min_eur"])
               and marge_pct >= float(r["marge_min_pct"]))
    if not affaire:
        return clore("vue", None, **{k: v for k, v in resultat.items()
                                     if k in ("prix_total", "reference", "marge",
                                              "score_arnaque")})

    # Affaire potentielle : seulement maintenant on dépense une requête de profil.
    avis = None
    if verif is not None:
        v = verif(brut) or {}
        if not v.get("ok", True) and v.get("transitoire"):
            return {"statut": "transitoire", **base, "motif": v.get("motif")}
        avis = v.get("avis")
        if not v.get("ok", True):
            return clore("rejetee", v.get("motif") or "vendeur sans avis",
                         vendeur_avis=avis, score_arnaque=risque["score"])
        risque = arnaque.evaluer(titre, description, prix, reference,
                                 nb_photos=len(brut.get("photos") or []),
                                 vendeur_avis=avis, cfg=cfg)
        if risque["rejeter"]:
            return clore("rejetee", f"risque d'arnaque {risque['score']}/100 : "
                                    + ", ".join(risque["raisons"][:3]),
                         vendeur_avis=avis, score_arnaque=risque["score"])

    # Un prix à moins de 40 % du marché n'est « trop beau » que si le vendeur
    # n'a pas fait ses preuves : les arnaques à l'avis unique sont là.
    ratio = prix / reference if reference else 1.0
    if ratio < float(arnaque.reglages(cfg)["prix_tres_bas_ratio"]) and \
            (avis or 0) < int(r["avis_confiance"]):
        return clore("rejetee",
                     f"prix à {ratio:.0%} du marché avec un vendeur à {avis or 0} avis "
                     f"(< {r['avis_confiance']}) : trop beau pour être vrai",
                     vendeur_avis=avis, score_arnaque=risque["score"])

    niveau = "affaire" if total <= m.steal * echelle else "bonne"
    resultat.update(niveau=niveau, vendeur_avis=avis,
                    score_arnaque=risque["score"],
                    motif=arnaque.drapeau(risque))
    _enregistrer(dict(base, statut="a_notifier",
                      **{k: resultat.get(k) for k in
                         ("prix_total", "reference", "marge", "niveau",
                          "score_arnaque", "vendeur_avis", "motif")}))
    resultat["statut"] = "a_notifier"
    return resultat


# ─────────────────────── SCAN D'UNE SOURCE ───────────────────────
def requetes_du_passage(source, cfg=None, n=None, maintenant=None, modeles=None):
    """Les `n` recherches les plus en retard, pondérées par la valeur du modèle.

    Priorité = (temps écoulé depuis la dernière recherche) × valeur^0,6 : un
    GPU à 600 € revient plus souvent qu'un processeur à 30 €, sans jamais
    affamer les autres. Les recherches jamais faites passent d'abord, au
    hasard, pour ne pas créer de motif régulier côté anti-bot."""
    r = reglages(cfg)
    n = int(r["requetes_par_source"] if n is None else n)
    maintenant = maintenant or time.time()
    if modeles is None:
        modeles = catalogue(r["categories"], float(r["prix_min_reference_eur"]))
    with ram_db.get_db() as conn:
        dernier = {row[0]: row[1] or 0 for row in conn.execute(
            "SELECT mot_cle, dernier_run FROM ram_scan_stat WHERE source=?", (source,))}
    candidates = {}
    for m in modeles:
        q = m.requetes[0]
        if q not in candidates or m.fair > candidates[q].fair:
            candidates[q] = m
    notes = []
    for q, m in candidates.items():
        age = min(maintenant - dernier.get(q, 0), 3 * 86400)
        notes.append((age * (max(m.fair, 25.0) ** 0.6) * random.uniform(0.8, 1.2), q))
    notes.sort(reverse=True)
    return [q for _, q in notes[:n]]


def scanner_requete(scraper, mot, cfg=None, modeles=None, refs=None):
    """UNE recherche « composants » (modèle précis ou recherche large) :
    évalue chaque annonce rendue. Retourne les évaluations nouvelles."""
    cfg = cfg or ram_config.get()
    refs = {} if refs is None else refs
    evaluations = []
    try:
        bruts = scraper.rechercher(mot)
    except Exception as e:
        scraper.echecs_consecutifs = getattr(scraper, "echecs_consecutifs", 0) + 1
        log("ERROR", "recherche composants en échec", source=scraper.source,
            mot_cle=mot, erreur=str(e))
        return evaluations
    if bruts is None:
        return evaluations
    nouveaux = 0
    for brut in bruts:
        try:
            ev = evaluer_annonce(brut, scraper.source, cfg,
                                 verif=scraper._verif_vendeur,
                                 modeles=modeles, refs=refs)
        except Exception as e:
            log("ERROR", "évaluation composant en échec",
                url=brut.get("url"), erreur=str(e))
            continue
        if ev and ev["statut"] not in ("deja_vue",):
            evaluations.append(ev)
            nouveaux += int(ev["statut"] != "transitoire")
    ram_db.maj_scan_stat(scraper.source, mot, requetes=1,
                         resultats=getattr(scraper, "_nb_bruts", len(bruts)),
                         nouveaux=nouveaux)
    return evaluations


def _journal(source, evaluations):
    if evaluations:
        compte = {}
        for ev in evaluations:
            compte[ev["statut"]] = compte.get(ev["statut"], 0) + 1
        log("INFO", "composants", source=source, evaluations=compte)


def scanner_large(scraper, mot, cfg=None):
    """Recherche large de la voie rapide (sans pause : l'appelant cadence)."""
    evaluations = scanner_requete(scraper, mot, cfg)
    _journal(scraper.source, evaluations)
    return evaluations


def scanner_source(scraper, cfg=None, limite=None, arret=None, modeles=None):
    """Un passage « composants » sur une source (ScraperVinted/ScraperLeboncoin).
    Retourne les évaluations. Passe par les mêmes clients, la même bascule de
    sortie réseau et le même disjoncteur que le scan RAM."""
    import ram_scrapers
    cfg = cfg or ram_config.get()
    r = reglages(cfg)
    if not r["actif"]:
        return []
    seuil_disj = int(cfg.val(f"sources.{scraper.source}.disjoncteur_echecs", 4))
    refs, evaluations = {}, []
    for mot in requetes_du_passage(scraper.source, cfg, limite, modeles=modeles):
        if arret is not None and arret.is_set():
            break
        if seuil_disj and getattr(scraper, "echecs_consecutifs", 0) >= seuil_disj:
            log("WARN", "disjoncteur : composants mis de côté pour ce cycle",
                source=scraper.source)
            break
        evaluations += scanner_requete(scraper, mot, cfg, modeles=modeles, refs=refs)
        ram_scrapers._pause(
            cfg.val(f"sources.{scraper.source}.delai_entre_requetes_s", [2.5, 5.0]))
    _journal(scraper.source, evaluations)
    return evaluations


# ─────────────────────── NOTIFICATION ───────────────────────
def message(ligne, estime=False):
    e = html.escape
    titre = {"affaire": "💎 AFFAIRE EN OR", "bonne": "🟢 BONNE AFFAIRE"}.get(
        ligne.get("niveau"), "🟢 BONNE AFFAIRE")
    etiquette = market_db.CATEGORIES.get(ligne["categorie"], {}).get("label", ligne["categorie"])
    prix, total = ligne["prix_affiche"], ligne["prix_total"]
    ref = ligne["reference"]
    out = [f"<b>{titre}</b> — {e(ligne['modele'])} <i>({e(etiquette.lower())})</i>",
           f"💶 <b>{prix:.0f} €</b> (port et frais compris : {total:.0f} €) · "
           f"marché ≈ {ref:.0f} €" + (" <i>(référence estimée)</i>" if estime else ""),
           f"📈 marge ≈ <b>{ligne['marge']:.0f} €</b> ({ligne['marge'] / total * 100:.0f} %)",
           f"🛒 {e(ligne['source'].capitalize())}"
           + (f" · vendeur {ligne['vendeur_avis']} avis"
              if ligne.get("vendeur_avis") is not None else "")]
    if ligne.get("motif"):
        out.append(e(ligne["motif"]))
    out.append(f"📝 {e((ligne.get('titre') or '')[:90])}")
    out.append(ligne["url"])
    return "\n".join(out)


def notifier(cfg=None):
    """Envoie les affaires en attente (les meilleures d'abord, plafonnées).
    Le reste attend le passage suivant, puis expire après `validite_h` : une
    affaire de la veille n'en est plus une. Retourne le nombre envoyé."""
    import ram_telegram
    cfg = cfg or ram_config.get()
    r = reglages(cfg)
    maintenant = time.time()
    with ram_db.get_db() as conn:
        conn.execute("UPDATE ram_composant SET statut='expiree' "
                     "WHERE statut='a_notifier' AND vu_le<?",
                     (maintenant - float(r["validite_h"]) * 3600,))
        lignes = [dict(x) for x in conn.execute(
            "SELECT * FROM ram_composant WHERE statut='a_notifier' "
            "ORDER BY marge DESC LIMIT ?", (int(r["max_notifs_par_passage"]),))]
    if not lignes:
        return 0
    estimes = {m.nom for m in catalogue(r["categories"]) if m.estime}
    envoyes = 0
    for i, ligne in enumerate(lignes):
        try:
            ram_telegram.envoyer_texte(message(ligne, ligne["modele"] in estimes), cfg)
        except Exception as e:
            log("WARN", "notification composant impossible", erreur=str(e))
            break
        with ram_db.get_db() as conn:
            conn.execute("UPDATE ram_composant SET statut='notifiee', notifie_le=? "
                         "WHERE id=?", (time.time(), ligne["id"]))
        envoyes += 1
        if i < len(lignes) - 1:
            time.sleep(float(r["pause_notif_s"]))
    return envoyes


# ─────────────────────── RAPPORT DE PRIX ───────────────────────
def rapport_prix(cfg=None, categorie=None):
    """Pour chaque modèle : prix statique, nombre d'annonces observées, médiane
    observée, prix de référence retenu. Sert à vérifier (et corriger) la grille."""
    r = reglages(cfg)
    cats = [categorie] if categorie else r["categories"]
    lignes = []
    for m in catalogue(cats, float(r["prix_min_reference_eur"])):
        ref, d = prix_reference(m, cfg)
        lignes.append({"categorie": m.categorie, "modele": m.nom, "statique": m.fair,
                       "observees": d["n"], "mediane": d["mediane"], "reference": ref,
                       "ecart_pct": round((ref / m.fair - 1) * 100, 1),
                       "estime": m.estime})
    return lignes


def afficher_rapport(cfg=None, categorie=None):
    lignes = rapport_prix(cfg, categorie)
    print(f"{'catégorie':9} {'modèle':22} {'statique':>8} {'vues':>5} {'médiane':>8} "
          f"{'retenu':>7} {'écart':>7}")
    for x in sorted(lignes, key=lambda x: (x["categorie"], -x["statique"])):
        med = f"{x['mediane']:.0f}" if x["mediane"] is not None else "—"
        print(f"{x['categorie']:9} {x['modele'][:22]:22} {x['statique']:>8.0f} "
              f"{x['observees']:>5} {med:>8} {x['reference']:>7.0f} "
              f"{x['ecart_pct']:>+6.1f}%" + ("  (estimé)" if x["estime"] else ""))
    return lignes
