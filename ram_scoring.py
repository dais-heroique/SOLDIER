"""
ram_scoring.py — Pré-score textuel + score final après vision
═══════════════════════════════════════════════════════════════════════════
Deux étages, deux usages :

  1. pre_score(annonce)   — instantané, texte seul. Décide de la notification
     immédiate (< 10 s après publication) et de l'entrée en file vision.
        pre_score = marge×0.50 + liquidité×0.25 + qualité×0.15 + vendeur×0.10

  2. score_final(annonce, vision) — après Gemini. Recalcule la marge avec le
     part number réellement lu et intègre la confiance visuelle.
        score = marge×0.40 + liquidité×0.20 + confiance_vision×0.20
              + vendeur×0.10 + logistique×0.10

RÈGLE CARDINALE : le prix scoré est TOUJOURS le prix total d'acquisition
(affiché + port + protection acheteur). Scorer sur le prix affiché revient à
se tromper de 15 à 20 % sur les petits montants — exactement la zone où se
joue la différence entre une affaire et une perte de temps.
"""

import time

import ram_config

# Multiplicateurs appliqués au prix de référence pour estimer la revente réelle.
# Clés = noms dans ram_config.yaml → multiplicateurs.
_MULT_DEFAUT = {
    "kit_assorti_origine": 1.25, "rgb": 1.20, "couleur_blanche": 1.15,
    "low_profile": 1.10, "memtest_prouve": 1.10, "dual_rank_2x16": 1.05,
    "dissipateur_manquant": 0.75, "sans_boite": 0.95, "no_name": 0.50,
}


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _reference_plancher(analyse):
    """Référence synthétique construite sur le prix plancher d'une capacité.

    Sert quand rien ne colle dans la base : une annonce « Barrette RAM 16Go »
    sans marque ni fréquence reste évaluable, à la valeur la plus basse
    crédible. Volontairement pessimiste — on ne veut alerter que sur ce qui est
    manifestement bradé, pas sur une estimation optimiste inventée.
    """
    import ram_db
    capacite = analyse.get("capacite_module_go")
    if not capacite:
        return None
    plancher = ram_db.prix_plancher(capacite, 1)
    if not plancher:
        return None
    return {
        "id": None, "part_number": None, "marque": analyse.get("marque_detectee"),
        "gamme": "estimation plancher", "capacite_module_go": capacite,
        "nb_modules": 1, "capacite_totale_go": capacite,
        "frequence_mhz": analyse.get("frequence_mhz"),
        "cas_latency": analyse.get("cas_latency"), "rank": None, "die_type": None,
        "rgb": 0, "couleur": None, "low_profile": 0, "tier": "C",
        "prix_ref_occasion_eur": plancher, "liquidite": 3,
        "delai_rotation_jours": 20, "synthetique": True,
    }


# ─────────────────────── FRAIS D'ACQUISITION ───────────────────────
def frais_acquisition(source, prix_affiche, port_connu=None, main_propre=False, cfg=None):
    """(frais_port, frais_protection, prix_total).

    Sur Vinted la protection acheteur est incontournable et proportionnelle :
    l'ignorer surestime la marge d'environ 5 % + 0,70 € à chaque annonce.
    Sur Leboncoin, un retrait en main propre annule le port — c'est ce qui
    rend Leboncoin compétitif malgré des vendeurs mieux informés.
    """
    cfg = cfg or ram_config.get()
    section = cfg.val(f"frais.{source}", None) or cfg.val("frais.vinted", {})

    if main_propre:
        port = 0.0
    elif port_connu is not None:
        port = float(port_connu)
    else:
        port = float(section.get("port_defaut", 3.5))

    pct = float(section.get("protection_pct", 0.0)) / 100.0
    fixe = float(section.get("protection_fixe", 0.0))
    protection = round(float(prix_affiche) * pct + fixe, 2) if (pct or fixe) else 0.0

    return round(port, 2), protection, round(float(prix_affiche) + port + protection, 2)


# ─────────────────────── VALEUR DE REVENTE ───────────────────────
def valeur_revente(ref, analyse, vision=None, cfg=None):
    """Prix de revente réaliste = prix de référence, ajusté des ÉCARTS entre
    l'annonce et la référence.

    ⚠️ Point de rigueur : `prix_ref_occasion_eur` est déjà le prix observé de
    CETTE référence dans SA configuration (un kit 2×16 RGB low profile est
    référencé comme tel). Appliquer « +25 % kit assorti », « +20 % RGB » et
    « +10 % low profile » par-dessus reviendrait à compter deux fois ce qui est
    déjà dans le prix — et à surestimer la revente de 40 à 60 %, c'est-à-dire à
    acheter trop cher.

    Un multiplicateur n'est donc appliqué que s'il correspond à un écart réel :
      • soit une caractéristique présente dans l'annonce et ABSENTE de la
        référence retenue (cas d'une référence approchante) ;
      • soit un fait d'état qui n'est jamais dans un prix catalogue
        (MemTest prouvé, dissipateur manquant, sans boîte).

    Retourne (valeur, [explications]) — les explications remontent jusqu'à la
    notification : savoir POURQUOI la revente est estimée à 128 € et pas 115 €
    est ce qui permet de décider en 8 secondes.
    """
    cfg = cfg or ram_config.get()
    mult_cfg = {**_MULT_DEFAUT, **(cfg.section("multiplicateurs") or {})}
    if not ref:
        return None, []

    valeur = float(ref["prix_ref_occasion_eur"])
    details = []
    v = vision or {}
    ref_nb = int(ref["nb_modules"] or 1)
    nb_annonce = int(analyse.get("nb_modules") or ref_nb)

    # ── 1. Mise à l'échelle sur le nombre de barrettes ──
    if nb_annonce != ref_nb:
        valeur = valeur / ref_nb * nb_annonce
        if nb_annonce < ref_nb:
            valeur *= 0.88          # une barrette dépareillée vaut moins que la moitié d'un kit
            details.append(f"{nb_annonce} barrette(s) issue(s) d'un kit de {ref_nb} : "
                           f"prorata −12 %")
        else:
            details.append(f"{nb_annonce} barrettes (référence : kit de {ref_nb})")

        # Prime de kit assorti : légitime UNIQUEMENT ici, quand on valorise
        # plusieurs barrettes à partir d'un prix unitaire. C'est exactement
        # l'arbitrage d'appariement (2 × 35 € → 120 € en kit).
        if nb_annonce > 1 and ref_nb == 1 and analyse.get("est_kit"):
            m = float(mult_cfg["kit_assorti_origine"])
            valeur *= m
            details.append(f"vendable en kit assorti : ×{m:g}")

    # ── 2. Écarts de caractéristiques vs la référence retenue ──
    # (n'arrive qu'avec une référence approchante : sur un PN exact, tout est
    #  déjà dans le prix de référence)
    rgb_vu = v.get("rgb") if v.get("rgb") is not None else analyse.get("rgb")
    if rgb_vu and not ref.get("rgb"):
        m = float(mult_cfg["rgb"])
        valeur *= m
        details.append(f"RGB non présent sur la référence : ×{m:g}")

    couleur_vue = (v.get("couleur") or "").lower()
    blanc_vu = "blanc" in couleur_vue or "white" in couleur_vue or analyse.get("blanc")
    ref_blanche = "blanc" in (ref.get("couleur") or "").lower()
    if blanc_vu and not ref_blanche:
        m = float(mult_cfg["couleur_blanche"])
        valeur *= m
        details.append(f"coloris blanc (builds blancs) : ×{m:g}")

    if v.get("hauteur_estimee") == "low_profile" and not ref.get("low_profile"):
        m = float(mult_cfg["low_profile"])
        valeur *= m
        details.append(f"low profile confirmé à l'image : ×{m:g}")

    if (analyse.get("rank") == "2Rx8" and ref.get("rank") not in ("2Rx8",)
            and ref["capacite_module_go"] == 16 and nb_annonce == 2):
        m = float(mult_cfg["dual_rank_2x16"])
        valeur *= m
        details.append(f"dual rank 2Rx8 (gain Ryzen) : ×{m:g}")

    # Marque no-name alors que la référence retenue ne l'est pas : la référence
    # surévalue forcément l'annonce.
    if analyse.get("no_name") and ref["tier"] != "D":
        m = float(mult_cfg["no_name"])
        valeur *= m
        details.append(f"marque no-name : ×{m:g}, rotation ×3")

    # ── 3. État réel : jamais compris dans un prix de référence ──
    if analyse.get("memtest_prouve"):
        m = float(mult_cfg["memtest_prouve"])
        valeur *= m
        details.append(f"MemTest prouvé (screenshot) : ×{m:g}")

    dissipateur_ko = analyse.get("dissipateur_manquant") or \
        v.get("etat_dissipateur") in ("manquant", "abime", "abîmé", "casse", "cassé")
    if dissipateur_ko:
        m = float(mult_cfg["dissipateur_manquant"])
        valeur *= m
        details.append(f"dissipateur manquant/abîmé : ×{m:g}")

    if analyse.get("sans_boite"):
        m = float(mult_cfg["sans_boite"])
        valeur *= m
        details.append(f"sans boîte : ×{m:g}")

    # ── 4. Frais de revente (emballage, commissions éventuelles) ──
    emballage = float(cfg.val("frais.revente.emballage_eur", 1.20))
    valeur -= emballage

    return round(max(valeur, 0.0), 2), details


# ─────────────────────── COMPOSANTES DE SCORE ───────────────────────
def note_marge(marge_eur, marge_pct, cfg=None):
    """Marge en note /100. Combine le montant absolu et le pourcentage : 60 %
    de marge sur 12 € ne vaut pas 60 % sur 90 €, et l'inverse est vrai aussi
    (30 € de marge sur un article à 400 € immobilise trop de capital)."""
    cfg = cfg or ram_config.get()
    plafond_eur = float(cfg.val("scoring.marge_plafond_eur", 80))
    plafond_pct = float(cfg.val("scoring.marge_pct_plafond", 150))
    if marge_eur is None:
        return 0.0
    note_abs = clamp(marge_eur / plafond_eur * 100)
    note_rel = clamp((marge_pct or 0) / plafond_pct * 100)
    return round(0.6 * note_abs + 0.4 * note_rel, 1)


def note_liquidite(ref, cfg=None):
    """Liquidité 1-5 → /100, corrigée par le délai de rotation. La rotation est
    le KPI décisif : à marge égale, une barrette qui part en 5 jours vaut
    beaucoup mieux qu'une qui dort 30 jours."""
    if not ref:
        return 30.0
    base = (int(ref.get("liquidite") or 3) - 1) / 4 * 100
    rotation = ref.get("delai_rotation_jours")
    if rotation:
        # 7 jours = neutre ; 30 jours = −25 points ; 3 jours = +8
        base += clamp(25 * (7 - float(rotation)) / 23, -25, 8)
    return round(clamp(base), 1)


def note_vendeur(annonce):
    """Note vendeur /100. L'absence d'information n'est pas une mauvaise note :
    beaucoup de bonnes affaires viennent de comptes neufs qui vident un PC."""
    note = 55.0
    etoiles = annonce.get("vendeur_note")
    ventes = annonce.get("vendeur_ventes")
    if etoiles is not None:
        try:
            note = clamp((float(etoiles) / 5.0) * 100)
        except (TypeError, ValueError):
            pass
    if ventes:
        try:
            v = int(ventes)
            # Un historique fourni rassure, mais avec rendement décroissant.
            note += clamp(min(v, 200) / 200 * 15, 0, 15)
        except (TypeError, ValueError):
            pass
    return round(clamp(note), 1)


def ajustement_fraicheur(annonce, cfg=None):
    """(points, explication) — bonus/malus selon l'âge de l'annonce.

    Le raisonnement est celui du marché, pas celui du code : sur Vinted, une
    barrette nettement sous-cotée part en minutes. Une annonce qui coche toutes
    les cases du scoring MAIS qui est en ligne depuis une semaine cache presque
    toujours quelque chose — dissipateur cassé qu'on ne voit pas, vendeur qui ne
    répond jamais, ou tout simplement un prix qui n'est pas si bon que ça et une
    référence de prix à recalibrer.

    C'est un ajustement en points, pas une composante pondérée : il corrige un
    score déjà calculé sans qu'il faille rééquilibrer les poids du YAML.
    """
    cfg = cfg or ram_config.get()
    publie = annonce.get("publie_le")
    if not publie:
        return 0.0, None
    heures = (time.time() - float(publie)) / 3600.0
    if heures < 0.25:
        return 8.0, "publiée il y a moins de 15 min — fenêtre de tir"
    if heures < 1:
        return 5.0, "publiée il y a moins d'une heure"
    if heures < 6:
        return 2.0, None
    if heures < 24:
        return 0.0, None
    if heures < 72:
        return -4.0, None
    if heures < 24 * 7:
        return -8.0, "en ligne depuis plusieurs jours : d'autres l'ont déjà vue"
    return -15.0, ("en ligne depuis plus d'une semaine — si le prix était si bon, "
                   "elle serait partie")


def note_logistique(annonce, cfg=None):
    """Retrait en main propre > envoi. Zéro frais, zéro risque de casse, et on
    voit la marchandise avant de payer."""
    cfg = cfg or ram_config.get()
    if annonce.get("main_propre"):
        return 100.0
    if annonce.get("source") == "leboncoin":
        dept = str(annonce.get("departement") or "")
        if dept in [str(d) for d in cfg.val("sources.leboncoin.departements", [])]:
            return 85.0
        return 45.0
    port = float(annonce.get("frais_port") or 0)
    return round(clamp(100 - port * 12), 1)


# ─────────────────────── MARGE ───────────────────────
def calculer_marge(prix_total, revente):
    if revente is None or prix_total is None:
        return None, None
    marge = round(revente - prix_total, 2)
    pct = round(marge / prix_total * 100, 1) if prix_total > 0 else 0.0
    return marge, pct


def marge_suffisante(marge_eur, marge_pct, cfg=None):
    """La règle est un ET, pas un OU : on rejette seulement si la marge est à
    la fois trop petite en valeur ET en pourcentage. Une grosse marge en euros
    sur un pourcentage faible (kit 64 Go) reste intéressante, et l'inverse
    aussi sur du volume rapide."""
    cfg = cfg or ram_config.get()
    min_eur = float(cfg.val("scoring.marge_min_eur", 20))
    min_pct = float(cfg.val("scoring.marge_min_pct", 45))
    if marge_eur is None:
        return False
    return not (marge_eur < min_eur and (marge_pct or 0) < min_pct)


# ─────────────────────── PRÉ-SCORE ───────────────────────
def pre_score(annonce, analyse, cfg=None):
    """Étape 1 : texte seul. Retourne un dict complet, prêt à être écrit en
    base et à alimenter la notification instantanée."""
    cfg = cfg or ram_config.get()
    poids = cfg.val("scoring.poids_pre_score", {}) or {}

    resultat = {
        "pre_score": 0.0, "revente_estimee": None, "marge_estimee": None,
        "marge_pct": None, "qualite_annonce": analyse.get("qualite_annonce", 0.0),
        "score_vendeur": note_vendeur(annonce), "score_logistique": note_logistique(annonce, cfg),
        "exclusion": analyse.get("exclusion"), "rejet_motif": analyse.get("rejet_motif"),
        "details_revente": [], "notes": {},
    }

    # Exclusion de périmètre : score 0, pas de calcul, pas de notification.
    if analyse.get("exclusion"):
        resultat["rejet_motif"] = analyse.get("rejet_motif")
        return resultat

    if not analyse.get("pertinent"):
        resultat["exclusion"] = "hors_sujet"
        resultat["rejet_motif"] = "aucun signal DDR4 exploitable dans l'annonce"
        return resultat

    ref = analyse.get("ref") or analyse.get("ref_approchee")
    if not ref:
        # Dernier filet : une capacité connue suffit à poser une valeur
        # plancher. Sans elle, l'annonce n'est vraiment pas exploitable.
        ref = _reference_plancher(analyse)
    if not ref:
        resultat["exclusion"] = "non_identifie"
        resultat["rejet_motif"] = ("capacité illisible dans l'annonce : "
                                   "aucune valeur de revente estimable")
        return resultat

    # Cas particulier : le lot de 4 Go n'a de sens qu'à très bas prix unitaire.
    if analyse.get("capacite_module_go") == 4:
        exc = cfg.val("perimetre.exception_4go", {}) or {}
        nb = analyse.get("nb_modules") or 1
        prix_unitaire = float(annonce.get("prix_total", 0)) / max(nb, 1)
        if prix_unitaire > float(exc.get("prix_max_unitaire", 1.5)):
            resultat["exclusion"] = "capacite"
            resultat["rejet_motif"] = (f"lot 4 Go à {prix_unitaire:.2f} €/barrette > "
                                       f"{exc.get('prix_max_unitaire', 1.5)} € : sans intérêt")
            return resultat

    revente, details = valeur_revente(ref, analyse, None, cfg)
    marge, pct = calculer_marge(annonce.get("prix_total"), revente)

    resultat.update({"revente_estimee": revente, "marge_estimee": marge,
                     "marge_pct": pct, "details_revente": details})

    if not marge_suffisante(marge, pct, cfg):
        resultat["exclusion"] = "marge"
        resultat["rejet_motif"] = (
            f"marge {marge:.0f} € / {pct:.0f} % sous le plancher "
            f"({cfg.val('scoring.marge_min_eur')} € et {cfg.val('scoring.marge_min_pct')} %)"
            if marge is not None else "marge incalculable")
        return resultat

    notes = {
        "marge": note_marge(marge, pct, cfg),
        "liquidite": note_liquidite(ref, cfg),
        "qualite_annonce": float(analyse.get("qualite_annonce", 0.0)),
        "vendeur": resultat["score_vendeur"],
    }
    score = sum(notes[k] * float(poids.get(k, 0)) for k in notes)

    # Une identification textuelle faible ne doit pas produire un score de
    # certitude : on plafonne tant que Gemini n'a pas confirmé.
    confiance = float(analyse.get("confiance_texte") or 0)
    if confiance < 0.5:
        score = min(score, 72.0)

    # Présomption de DDR3 non levée : l'annonce reste visible mais recule dans
    # la file. C'est le compromis choisi — signaler plutôt qu'éliminer, quitte
    # à ce que l'utilisateur tranche sur les photos.
    suspicions = analyse.get("suspicions_ddr3") or []
    if suspicions:
        penalite = float(cfg.val("perimetre.pieges_ddr3.penalite_score", 20))
        score -= penalite
        resultat.setdefault("drapeaux", []).extend(suspicions)

    points_age, note_age = ajustement_fraicheur(annonce, cfg)
    score += points_age
    if note_age:
        resultat.setdefault("drapeaux", []).append(note_age)
    resultat["ajustement_fraicheur"] = points_age

    resultat["notes"] = {k: round(v, 1) for k, v in notes.items()}
    resultat["pre_score"] = round(clamp(score), 1)
    return resultat


# ─────────────────────── SCORE FINAL ───────────────────────
def score_final(annonce, analyse, vision, cfg=None):
    """Étape 2 : après Gemini. `vision` est le dict normalisé de ram_vision.

    Retourne notamment `statut_verif` :
      confirme    — identification cohérente et score ≥ seuil_confirme
      probable    — cohérent mais photo moyenne (confiance 0,5-0,75)
      a_verifier  — photo illisible → message pré-rédigé au vendeur
      rejete      — incohérence détectée (DDR3, SO-DIMM, ECC, faux sticker)
    """
    cfg = cfg or ram_config.get()
    poids = cfg.val("scoring.poids_score_final", {}) or {}
    v = vision or {}

    resultat = {
        "score_final": 0.0, "statut_verif": "a_verifier", "marge_reelle": None,
        "marge_reelle_pct": None, "revente_estimee": None, "rejet_motif": None,
        "drapeaux": list(v.get("drapeaux") or []), "details_revente": [], "notes": {},
    }

    # ── Rejets durs issus de l'image ──
    rejets = []
    if v.get("est_sodimm"):
        rejets.append("SO-DIMM identifiée à l'image (barrette courte)")
    if v.get("est_ecc"):
        rejets.append("ECC identifiée à l'image (9 ou 18 puces)")
    if v.get("est_registered"):
        rejets.append("RDIMM : puce de registre centrale visible")
    gen = (v.get("generation_suspectee") or "").upper()
    if gen and gen not in ("DDR4", "INCONNU", "INCONNUE", ""):
        rejets.append(f"encoche en position {gen} : ce n'est pas de la DDR4")
    if v.get("est_ddr4_desktop") is False:
        rejets.append("le modèle ne reconnaît pas une DDR4 desktop")
    if v.get("sticker_authentique") is False:
        rejets.append("sticker suspect : relabellisation probable")

    if rejets:
        resultat["statut_verif"] = "rejete"
        resultat["rejet_motif"] = " · ".join(rejets)
        resultat["drapeaux"] = rejets + resultat["drapeaux"]
        return resultat

    # ── Photo illisible : on ne tranche pas, on demande une photo ──
    confiance = v.get("confiance")
    confiance = float(confiance) if confiance is not None else 0.0
    if v.get("photo_lisible") is False or v.get("statut") in ("parse_erreur", "echec"):
        resultat["statut_verif"] = "a_verifier"
        resultat["rejet_motif"] = "photo illisible : sticker non déchiffrable"
        return resultat

    # ── Réidentification avec le part number réellement lu ──
    import ram_db
    ref = None
    if v.get("part_number_lu"):
        ref = ram_db.find_reference_by_pn(v["part_number_lu"])
        if ref is None:
            ram_db.signaler_pn_inconnu(v["part_number_lu"], marque=v.get("marque"),
                                       prix=annonce.get("prix_total"),
                                       url=annonce.get("url"), titre=annonce.get("titre"))
    ref = ref or analyse.get("ref") or analyse.get("ref_approchee")
    if not ref:
        resultat["statut_verif"] = "a_verifier"
        resultat["rejet_motif"] = "part number illisible et aucune référence approchante"
        return resultat

    # Le nombre de barrettes VU prime sur le nombre annoncé : c'est là que se
    # trouvent les "kit 2x16" qui n'ont qu'une seule barrette en photo.
    analyse_corrigee = dict(analyse)
    if v.get("nb_barrettes_visibles"):
        analyse_corrigee["nb_modules"] = int(v["nb_barrettes_visibles"])
        analyse_corrigee["est_kit"] = int(v["nb_barrettes_visibles"]) > 1
        if analyse.get("nb_modules") and \
                int(v["nb_barrettes_visibles"]) < int(analyse["nb_modules"]):
            resultat["drapeaux"].append(
                f"annonce : {analyse['nb_modules']} barrettes, "
                f"photo : {v['nb_barrettes_visibles']} → vérifier avant d'acheter")

    revente, details = valeur_revente(ref, analyse_corrigee, v, cfg)
    marge, pct = calculer_marge(annonce.get("prix_total"), revente)
    resultat.update({"marge_reelle": marge, "marge_reelle_pct": pct,
                     "revente_estimee": revente, "details_revente": details,
                     "ref": ref})

    if not marge_suffisante(marge, pct, cfg):
        resultat["statut_verif"] = "rejete"
        resultat["rejet_motif"] = (f"marge réelle {marge:.0f} € / {pct:.0f} % "
                                   f"sous le plancher après identification exacte")
        return resultat

    notes = {
        "marge": note_marge(marge, pct, cfg),
        "liquidite": note_liquidite(ref, cfg),
        "confiance_vision": round(clamp(confiance * 100), 1),
        "vendeur": note_vendeur(annonce),
        "logistique": note_logistique(annonce, cfg),
    }
    score = round(clamp(sum(notes[k] * float(poids.get(k, 0)) for k in notes)), 1)

    # Contacts oxydés / brûlés : ça se revend, mais moins cher et plus lentement.
    if v.get("etat_contacts") in ("oxyde", "oxydé", "raye", "rayé", "brule", "brûlé"):
        score -= 8
        resultat["drapeaux"].append(f"contacts : {v['etat_contacts']}")

    seuil_confirme = float(cfg.val("scoring.seuil_confirme", 75))
    c_min_confirme = float(cfg.val("scoring.confiance_confirme_min", 0.75))
    c_min_probable = float(cfg.val("scoring.confiance_probable_min", 0.50))

    if confiance >= c_min_confirme and score >= seuil_confirme:
        resultat["statut_verif"] = "confirme"
    elif confiance >= c_min_probable:
        resultat["statut_verif"] = "probable"
    else:
        resultat["statut_verif"] = "a_verifier"

    resultat["notes"] = {k: round(v_, 1) for k, v_ in notes.items()}
    resultat["score_final"] = round(clamp(score), 1)
    return resultat


if __name__ == "__main__":
    import ram_parser

    annonce = {"source": "vinted", "prix_affiche": 45.0, "vendeur_note": 4.9,
               "vendeur_ventes": 127}
    port, protection, total = frais_acquisition("vinted", 45.0, port_connu=4.0)
    annonce.update({"frais_port": port, "frais_protection": protection, "prix_total": total})

    analyse = ram_parser.analyser(
        "Ram ddr4 32go corsair",
        "Kit 2x16 3200mhz CMK32GX4M2E3200C16 en très bon état, boîte d'origine", 3)
    pre = pre_score(annonce, analyse)

    print(f"Prix {annonce['prix_affiche']:.0f}€ + {port:.0f}€ port + {protection:.2f}€ "
          f"= {total:.2f}€")
    print(f"Revente estimée {pre['revente_estimee']}€ → marge {pre['marge_estimee']}€ "
          f"({pre['marge_pct']}%)")
    for d in pre["details_revente"]:
        print(f"   · {d}")
    print(f"PRÉ-SCORE {pre['pre_score']}  détail={pre['notes']}")

    vision = {"est_ddr4_desktop": True, "generation_suspectee": "DDR4", "est_sodimm": False,
              "est_ecc": False, "est_registered": False,
              "part_number_lu": "CMK32GX4M2E3200C16", "marque": "Corsair",
              "nb_barrettes_visibles": 2, "nb_puces_par_face": 8, "sticker_authentique": True,
              "etat_contacts": "propre", "etat_dissipateur": "bon", "rgb": False,
              "couleur": "noir", "hauteur_estimee": "low_profile", "photo_lisible": True,
              "drapeaux": [], "confiance": 0.88, "statut": "ok"}
    fin = score_final(annonce, analyse, vision)
    print(f"\nSCORE FINAL {fin['score_final']} — {fin['statut_verif'].upper()}")
    print(f"Revente {fin['revente_estimee']}€ → marge nette {fin['marge_reelle']}€ "
          f"({fin['marge_reelle_pct']}%)  détail={fin['notes']}")
