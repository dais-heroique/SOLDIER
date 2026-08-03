"""
listing_filter.py — Analyse de pertinence + état + RISQUE D'ARNAQUE d'une annonce
═══════════════════════════════════════════════════════════════════════════════
Résout plusieurs problèmes :

  1. ACCESSOIRE, pas le composant : carton/boîte SANS le composant (ex: "boîte
     de GPU sans la carte"), câble, waterblock, support, annonce "je recherche".

  2. ÉTAT HS / défectueux : détecté et exclu.

  3. RISQUE D'ARNAQUE — nouveau. Le piège classique : une annonce "trop belle
     pour être vraie" (ex: "RTX 4090 neuve jamais ouverte à 500€, raison
     familiale, urgent, livraison uniquement, pas d'échange"). Un prix
     extrêmement bas + du vocabulaire d'urgence/pression + un état "neuf"
     invérifiable, c'est le profil-type de l'arnaque (vol, épave rebrandée,
     photo volée, ou simple entourloupe au paiement).

     On combine plusieurs signaux (phrases suspectes, "neuf" + prix bien trop
     bas pour être crédible, absence de vérification possible) en un score de
     risque, et on ajuste la confiance / on exclut si le risque est trop élevé
     — plutôt que de laisser un prix ridiculement bas remonter comme
     "💎 AFFAIRE EN OR" alors que c'est presque sûrement une arnaque.

Niveau ÉQUILIBRÉ : on EXCLUT le clairement accessoire/HS/arnaque probable, on
GARDE le reste avec un score de confiance (état inconnu = gardé, signalé).
"""

import re

# ── Mots qui indiquent que l'annonce vend un ACCESSOIRE, pas le composant ──
ACCESSORY_PATTERNS = {
    "carton/boîte seule": [
        r"\bboite? vide\b", r"\bboîte vide\b", r"\bcarton vide\b",
        r"\bjuste la (boite|boîte|carton)\b", r"\bemballage seul\b",
        r"\bvide.{0,10}(sans|pour) (carte|gpu)\b", r"\bboite d'origine seule\b",
        r"\bboite seule\b", r"\bboîte seule\b", r"\bcarton seul\b",
        r"\bemballage d'origine seul\b", r"\b(vends?|vente) (la |ma )?boite\b",
        # "GPU sans GPU": la boîte/emballage est présent mais PAS le composant
        r"\bsans (la carte|le gpu|la gpu|le cpu|le processeur)\b",
        r"\b(carte|gpu|cpu|processeur) manquant", r"\bmanque la (carte|carte graphique|gpu)\b",
        r"\bboite? (uniquement|seulement)\b", r"\bjuste l'emballage\b",
        # variantes anglaises (annonces eBay international) — même piège
        r"\bbox only\b", r"\bempty box\b", r"\bwithout (gpu|card|cpu|processor)\b",
        r"\bno (gpu|card|cpu) (included|inside)\b",
    ],
    "câble/adaptateur": [
        r"\bc[âa]ble\b", r"\bcable d'alim", r"\bcâble d'alim", r"\badaptateur\b",
        r"\briser\b", r"\bpci-?e?\s*extender\b", r"\bnappe pci",
        r"\bconnecteur (seul|12vhpwr)\b", r"\b12vhpwr (cable|câble)\b",
        r"\bcable (12vhpwr|pcie|gpu)\b", r"\bcâble (12vhpwr|pcie|gpu)\b",
    ],
    "waterblock/refroidissement seul": [
        r"\bwaterblock\b", r"\bwater block\b", r"\bbackplate\b", r"\bback plate\b",
        r"\bventirad seul\b", r"\bventilateur(s)? seul", r"\bfan(s)? de (gpu|carte)\b",
        r"\bkit de refroidissement (pour|gpu)\b", r"\bbloc water", r"\bblock ek\b",
        # variantes langue étrangère (annonces eBay international) — "dissipateur"
        # seul, sans la carte, est un signal de bourrage identique au français
        r"\bdissipateur (seul|thermique)\b", r"\bdissipatore\b", r"\bdisipador\b",
        r"\bheatsink (only|seul)\b",
    ],
    "support/accessoire": [
        r"\bsupport (gpu|carte|anti-?sag)\b", r"\banti-?sag\b", r"\bstand (gpu|vertical)\b",
        r"\bkit vertical\b", r"\bplaque\b.{0,10}\brgb\b", r"\bautocollant", r"\bsticker",
        r"\bplexi\b", r"\bshroud\b",
    ],
    "antenne (accessoire, pas la carte mère)": [
        # "Antenne WiFi pour ASUS X870E..." mentionne le modèle de carte mère
        # juste pour indiquer la compatibilité — ce n'est pas la carte mère
        # elle-même. Cas très fréquent sur les cartes mères WiFi récentes.
        r"\bantenne(s)? wifi\b", r"\bantenne(s)? (pour|compatible)\b", r"\bwifi antenna\b",
        r"\bantenna (for|kit)\b",
    ],
    "recherche/achat": [
        r"\bje recherche\b", r"\brecherche (une|un|des)\b", r"\bj'achète\b",
        r"\bcherche à acheter\b", r"\bachat\b.{0,6}\brecherche\b",
    ],
    "emballage seul (langue étrangère)": [
        r"\bconfezione( originale)?\b",  # italien: "boîte/emballage (d'origine)"
        r"\bscatola vuota\b", r"\bcaja vacía\b", r"\bcaja vacia\b", r"\bleere verpackung\b",
    ],
    "composant vendu dans un PC complet/portable": [
        # une recherche "RTX 5090" qui remonte un PORTABLE ou un PC COMPLET n'est
        # pas le composant seul à vendre — comparer son prix au tarif d'une carte
        # desktop nue n'a aucun sens (marge fictive énorme, cas classique observé
        # en usage réel). On exclut plutôt que d'afficher un faux "steal".
        r"\bordinateur portable\b", r"\bpc portable\b", r"\blaptop\b", r"\bnotebook\b",
        r"\bportable de jeu\b", r"\bpc gamer complet\b", r"\bpc gaming complet\b",
        r"\bpc complet\b", r"\bunité centrale\b", r"\btour complète\b",
        r"\bconfiguration complète\b", r"\bpc gamer\b", r"\bpc gaming\b",
        r"\bordi gaming\b", r"\bjeux pc\b",
        # "jusqu'à RTX 5070" = annonce de configurateur (plusieurs options de
        # montage), pas un modèle précis en vente à ce prix précis — piège
        # classique des revendeurs qui annoncent le tarif du config de base
        r"\bjusqu'?à\b.{0,20}\b(rtx|gtx|ryzen|core i[3579])\b",
        r"\bup to\b.{0,20}\b(rtx|gtx)\b",
    ],
    "service de réparation, pas un composant à vendre": [
        # "réparation de carte mère/portable" = un réparateur qui propose son
        # service, pas quelqu'un qui vend le composant — même piège que
        # ci-dessus, très fréquent sur les résultats eBay internationaux.
        r"\bréparation de\b", r"\breparation de\b", r"\brepair service\b",
        r"\bmotherboard repair\b",
    ],
    "pièce détachée/accessoire pour un autre produit": [
        # Signal générique et très fréquent, toutes catégories confondues: une
        # annonce qui vend un ÉTUI, une BATTERIE, un FILTRE, des VIS, un
        # PANNEAU ou une CARTE D'EXTENSION "pour" un modèle donné mentionne ce
        # modèle uniquement pour la compatibilité — ce n'est jamais le produit
        # lui-même. Observé en usage réel sur SSD (étui de transport), UPS
        # (batterie de rechange), boîtiers (panneau/filtre/vis de rechange),
        # AIO (vis de fixation seules).
        r"\bétui\b", r"\bhousse\b", r"\bpochette\b", r"\bsleeve\b", r"\bcarrying case\b",
        r"\bbatterie\b.{0,20}\bpour\b", r"\bbatterie (pour|de rechange)\b",
        r"\bbattery for\b", r"\breplacement battery\b",
        r"\bfiltre (à |anti-?)?poussière\b", r"\bpoussière filtre\b", r"\bpoussiere filtre\b",
        r"\bdust filter\b",
        r"\bvis (pour|de fixation)\b", r"\bscrews? for\b", r"\bmounting screws?\b",
        r"\bpanneau (avant|latéral|arrière)\b", r"\bfront panel\b", r"\bside panel\b",
        r"\bgrommets?\b",
        r"\bcarte d'extension\b", r"\bexpansion card\b", r"\bexpansion slot\b",
        r"\bslot d'extension\b", r"\bespansione\b",
        r"\bventilateur pour\b", r"\bfan for\b",
    ],
}

# Patterns supplémentaires appliqués UNIQUEMENT à la catégorie GPU: "GPU
# Cooler" dans un titre signifie quasi toujours que seul le ventirad/shroud
# est vendu — la carte (PCB, mémoire, tout ce qu'il y a "à l'intérieur") n'y
# est pas. Signal fiable observé en usage réel, distinct du cas générique
# "waterblock/refroidissement seul" ci-dessus (qui exige déjà "seul").
GPU_ONLY_ACCESSORY_PATTERNS = {
    "GPU cooler seul (pas la carte)": [
        r"\bgpu cooler\b", r"\bcooler gpu\b", r"\bcarte graphique cooler\b",
        r"\bgraphics card cooler\b",
    ],
}

# Patterns supplémentaires appliqués UNIQUEMENT à la catégorie PSU: une
# alimentation SERVEUR/RACK (Cisco, Dell PowerEdge, HP, Artesyn...) utilise
# des connecteurs propriétaires (pas l'ATX 24 broches standard) — inutilisable
# telle quelle dans un PC de bureau classique. Observé en usage réel: ces
# annonces disent bien "alimentation" donc passent le filtre de pertinence,
# mais ce n'est pas le produit recherché pour un flip PC grand public.
PSU_ONLY_ACCESSORY_PATTERNS = {
    "alimentation serveur/rack (connecteurs non-PC)": [
        r"\bserveur\b", r"\bserver\b", r"\brack\b", r"\b1u\b", r"\b2u\b",
        r"\bpoweredge\b", r"\bproliant\b",
    ],
}

# Patterns supplémentaires appliqués UNIQUEMENT à la catégorie RAM: le nom de
# modèle catalogue ("DDR4 64GB (2x32)") n'a que le type+capacité comme token
# requis — bien trop faible, une carte mère qui décrit sa RAM supportée
# ("compatible DDR4 jusqu'à 64GB") satisfait déjà ces deux tokens sans être
# de la RAM en vente. Cas réel observé: annonces de cartes mères remontant
# dans les résultats RAM.
RAM_ONLY_ACCESSORY_PATTERNS = {
    "carte mère/PC décrivant sa RAM supportée (pas de la RAM en vente)": [
        r"\bcarte mère\b", r"\bmotherboard\b", r"\bcarte-mère\b",
    ],
}

# ── Mots d'état: BON ──
GOOD_CONDITION = [
    "fonctionne parfaitement", "parfait état", "parfait etat", "excellent état",
    "excellent etat", "très bon état", "tres bon etat", "comme neuf", "état neuf",
    "etat neuf", "neuf", "testé", "teste", "testée", "testee", "sous garantie",
    "garantie", "facture", "rien à signaler", "ras", "impeccable", "nickel",
    "aucun problème", "aucun probleme", "fonctionnel", "opérationnel", "operationnel",
    "jamais miné", "jamais mine", "non miné", "peu servi", "peu utilisé",
]

# ── Mots d'état: MAUVAIS (HS / défaut) ──
BAD_CONDITION = [
    "hs", "h.s", "ne fonctionne pas", "ne marche pas", "pour pièce", "pour pieces",
    "pour pièces", "défectueux", "defectueux", "en panne", "panne", "cassé", "casse",
    "ne démarre pas", "ne demarre pas", "ne boot pas", "à réparer", "a reparer",
    "vendu en l'état", "vendu en l etat", "ne s'allume pas", "ne s allume pas",
    "artefact", "artefacts", "écran cassé", "ecran casse", "bloqué au démarrage",
    "problème", "probleme", "défaut", "defaut", "grillé", "grille", "fumée", "fumee",
    "ventilo cassé", "ventilo hs", "bruit anormal", "surchauffe",
    # variantes langue étrangère (annonces eBay international) — même
    # exclusion que "défectueux" en français
    "defectuoso", "defeituoso", "no funciona", "não funciona", "danneggiato",
    "difettoso", "kaputt", "defekt", "broken", "not working", "doesn't work",
    "for parts", "for parts only", "faulty",
]

# ═══════════════════════════════════════════════════════════════════
#  DÉTECTION ANTI-ARNAQUE
# ═══════════════════════════════════════════════════════════════════

# Phrases de pression/urgence — classiques dans les arnaques (poussent à
# décider vite, sans vérifier). Poids = force du signal.
SCAM_PHRASES = {
    r"\burgent": 20,
    r"\bvente rapide\b": 15,
    r"\bavant que je change d'avis\b": 25,
    r"\bavant que je change davis\b": 25,
    r"\bà saisir rapidement\b": 12,
    r"\bpremier arrivé premier servi\b": 8,
    r"\bpas le temps de\b": 10,
}

# Contact/paiement hors plateforme — signal TRÈS fort d'arnaque (l'arnaqueur
# veut sortir du cadre protégé de la plateforme dès que possible).
EXTERNAL_CONTACT_PHRASES = {
    r"\bwhatsapp\b": 65,   # signal fort à lui seul (comme avant: exclusion quasi-automatique)
    r"\btelegram\b": 65,
    r"\buniquement par mail\b": 20,
    r"\bcontactez-moi par\b": 15,
    r"\bcontactez moi par\b": 15,
}

# Demande de paiement/dépôt avant même de voir/tester l'objet — arnaque classique
# ("verse un acompte pour réserver, tu viens ensuite chercher").
DEPOSIT_SCAM_PHRASES = {
    r"\bwestern union\b": 45,
    r"\bmandat cash\b": 45,
    r"\bacompte\b": 20,
    r"\barrhes\b": 20,
    r"\bdépôt de garantie\b": 15,
    r"\bdepot de garantie\b": 15,
    r"\bréserv\w* avec (un )?acompte\b": 30,
    r"\breserv\w* avec (un )?acompte\b": 30,
    r"\bpaypal (famille|friends)\b": 30,
    r"\bvirement (immédiat|immediat) obligatoire\b": 30,
}

# Aveu (volontaire ou non) que la photo n'est pas celle de l'objet réel —
# souvent une photo trouvée sur internet/catalogue pour une arnaque.
STOCK_PHOTO_PHRASES = {
    r"\bphoto (d'illustration|generique|générique)\b": 25,
    r"\bphoto (trouvée|trouvee) sur internet\b": 65,   # aveu explicite = signal très fort
    r"\bimage non contractuelle\b": 20,
    r"\bphoto du net\b": 60,
}

# Prétextes de vente — pas suspects seuls, mais renforcent le score si
# combinés à un prix anormalement bas.
SELLING_PRETEXT_PHRASES = {
    r"\braison familiale\b": 15,
    r"\braison personnelle\b": 15,
    r"\berreur de prix\b": 35,          # signal très fort, quasi toujours une arnaque
    r"\bmauvaise cat[ée]gorisation\b": 30,
    r"\bmauvaise annonce\b": 15,
}

# Absence de vérification possible — combiné à un prix suspect, ça empêche
# l'acheteur de se rendre compte du problème avant de payer.
NO_VERIFICATION_PHRASES = {
    r"\blivraison uniquement\b": 15,
    r"\benvoi uniquement\b": 15,
    r"\bpas de (remise en main propre|rdv|rendez-vous)\b": 12,
    r"\bpas d'échange\b": 8,
    r"\bpas d echange\b": 8,
    r"\bvente définitive\b": 5,
    r"\bvente definitive\b": 5,
    r"\bpaiement (avant|à l'avance)\b": 25,
    r"\bpaiement avant envoi\b": 25,
}

# "Neuf/jamais utilisé" — légitime la plupart du temps, MAIS devient un
# signal fort d'arnaque si le prix est bien trop bas pour un objet neuf.
CLAIMED_NEW_PHRASES = [
    "jamais utilisé", "jamais utilise", "jamais ouvert", "jamais servi",
    "encore sous blister", "jamais sorti de la boite", "jamais sorti de la boîte",
    "jamais déballé", "jamais deballe", "neuf jamais",
]

# Signaux qui RASSURENT (vraie raison, usage assumé, détails concrets) —
# réduisent le score de risque.
GOOD_SIGNAL_PHRASES = [
    "mois d'utilisation", "mois d utilisation", "ans d'utilisation",
    "changement de config", "upgrade vers", "nouvelle config", "je passe à",
    "petite rayure", "légère trace", "legere trace", "trace d'usure",
    "photos supplémentaires", "photos supplementaires", "numéro de série",
    "numero de serie", "n° de série",
]


def _normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def check_duplicate_spam(price, subject, tracker, key, threshold=4):
    """
    Détection de spam/bot : si le MÊME prix + un titre quasi-identique apparaît
    déjà `threshold` fois ou plus pour ce modèle (au sein du tracker fourni,
    typiquement remis à zéro à chaque cycle de scan), c'est probablement du
    spam automatisé plutôt que plusieurs vraies annonces différentes.

    tracker: dict partagé par l'appelant, structure {key: [(price, titre_normalisé), ...]}
    Retourne True si le seuil de répétition est atteint.
    """
    if tracker is None:
        return False
    title_norm = _normalize_title(subject)
    entries = tracker.setdefault(key, [])
    count = sum(1 for p, t in entries if p == price and t == title_norm)
    entries.append((price, title_norm))
    if len(entries) > 200:
        del entries[:100]  # garde-fou mémoire
    return count >= threshold


def _has_word(text, words):
    return any(w in text for w in words)


def _matches_any(text, patterns):
    for p in patterns:
        if re.search(p, text):
            return True
    return False


def _weighted_score(text, phrase_weights):
    score = 0
    matched = []
    for pattern, weight in phrase_weights.items():
        if re.search(pattern, text):
            score += weight
            matched.append(pattern)
    return score, matched


def _assess_scam_risk(text, price, fair):
    """
    Calcule un score de risque d'arnaque 0-100 à partir du texte de l'annonce
    et du rapport prix/valeur réelle. Retourne (score, claimed_new, details).
    """
    score = 0
    reasons = []

    s, _ = _weighted_score(text, SCAM_PHRASES)
    score += s
    if s:
        reasons.append("pression/urgence")

    s, _ = _weighted_score(text, SELLING_PRETEXT_PHRASES)
    score += s
    if s:
        reasons.append("prétexte suspect")

    s, _ = _weighted_score(text, NO_VERIFICATION_PHRASES)
    score += s
    if s:
        reasons.append("aucune vérification possible")

    s, _ = _weighted_score(text, EXTERNAL_CONTACT_PHRASES)
    score += s
    if s:
        reasons.append("contact hors plateforme")

    s, _ = _weighted_score(text, DEPOSIT_SCAM_PHRASES)
    score += s
    if s:
        reasons.append("demande de paiement/dépôt suspecte")

    s, _ = _weighted_score(text, STOCK_PHOTO_PHRASES)
    score += s
    if s:
        reasons.append("photo probablement pas la vraie")

    claimed_new = _has_word(text, CLAIMED_NEW_PHRASES)
    if claimed_new and price > 0 and fair > 0:
        ratio = price / fair
        if ratio < 0.35:
            score += 45   # neuf à moins de 35% du prix juste = quasiment impossible
            reasons.append("« neuf » à un prix impossible")
        elif ratio < 0.5:
            score += 25
            reasons.append("« neuf » à un prix très suspect")

    # prix extrêmement bas même sans revendication de "neuf"
    if price > 0 and fair > 0 and not claimed_new:
        ratio = price / fair
        if ratio < 0.2:
            score += 15
            reasons.append("prix extrêmement bas")

    good_matches = sum(1 for w in GOOD_SIGNAL_PHRASES if w in text)
    score -= good_matches * 12

    score = max(0, min(100, score))
    return score, claimed_new, reasons


def analyze(subject, description, category=None, price=0, fair=0,
            duplicate_tracker=None, duplicate_key=None):
    """
    Analyse une annonce. Retourne un dict:
      {is_accessory, accessory_type, condition, confidence, reason, keep,
       reason_key, reason_params, scam_risk}
    keep=False -> on n'affiche/alerte pas (accessoire clair, HS, ou arnaque
    quasi-certaine). reason_key/reason_params permettent au dashboard de
    traduire le message (le champ "reason" reste la version française).

    duplicate_tracker/duplicate_key (optionnels): si fournis, détecte le spam
    de bots (même prix + titre quasi-identique répété plusieurs fois pour ce
    modèle) et l'ajoute au score de risque d'arnaque.
    """
    text = ((subject or "") + " " + (description or "")).lower()

    # 1) ACCESSOIRE ? (carton, câble, waterblock, support, recherche)
    for label, patterns in ACCESSORY_PATTERNS.items():
        if _matches_any(text, patterns):
            return {
                "is_accessory": True, "accessory_type": label, "condition": "unknown",
                "confidence": 0, "scam_risk": 0,
                "reason": f"Accessoire détecté ({label}) — pas le composant lui-même",
                "reason_key": "accessory", "reason_params": {"type": label},
                "keep": False,
            }

    if category == "GPU":
        for label, patterns in GPU_ONLY_ACCESSORY_PATTERNS.items():
            if _matches_any(text, patterns):
                return {
                    "is_accessory": True, "accessory_type": label, "condition": "unknown",
                    "confidence": 0, "scam_risk": 0,
                    "reason": f"Accessoire détecté ({label}) — pas le composant lui-même",
                    "reason_key": "accessory", "reason_params": {"type": label},
                    "keep": False,
                }

    if category == "PSU":
        for label, patterns in PSU_ONLY_ACCESSORY_PATTERNS.items():
            if _matches_any(text, patterns):
                return {
                    "is_accessory": True, "accessory_type": label, "condition": "unknown",
                    "confidence": 0, "scam_risk": 0,
                    "reason": f"Accessoire détecté ({label}) — pas le composant lui-même",
                    "reason_key": "accessory", "reason_params": {"type": label},
                    "keep": False,
                }

    if category == "RAM":
        for label, patterns in RAM_ONLY_ACCESSORY_PATTERNS.items():
            if _matches_any(text, patterns):
                return {
                    "is_accessory": True, "accessory_type": label, "condition": "unknown",
                    "confidence": 0, "scam_risk": 0,
                    "reason": f"Accessoire détecté ({label}) — pas le composant lui-même",
                    "reason_key": "accessory", "reason_params": {"type": label},
                    "keep": False,
                }

    # 2) ÉTAT MAUVAIS ? (HS / défaut) → exclu
    if _has_word(text, BAD_CONDITION):
        bad_word = next((w for w in BAD_CONDITION if w in text), "defaut")
        return {
            "is_accessory": False, "accessory_type": None, "condition": "bad",
            "confidence": 0, "scam_risk": 0,
            "reason": f"État probablement HS/défectueux (« {bad_word} »)",
            "reason_key": "bad_condition", "reason_params": {"word": bad_word},
            "keep": False,
        }

    # 3) RISQUE D'ARNAQUE — calculé avant de conclure sur l'état
    scam_score, claimed_new, scam_reasons = _assess_scam_risk(text, price, fair)

    if duplicate_tracker is not None and duplicate_key is not None:
        if check_duplicate_spam(price, subject, duplicate_tracker, duplicate_key):
            scam_score = min(100, scam_score + 30)
            scam_reasons.append("prix+titre identiques répétés (spam probable)")

    if scam_score >= 60:
        return {
            "is_accessory": False, "accessory_type": None, "condition": "bad",
            "confidence": 0, "scam_risk": scam_score,
            "reason": f"Risque d'arnaque élevé ({', '.join(scam_reasons)}) — annonce écartée",
            "reason_key": "scam_high", "reason_params": {"score": scam_score},
            "keep": False,
        }

    # 4) ÉTAT BON ? → haute confiance, sauf si risque d'arnaque modéré détecté
    if _has_word(text, GOOD_CONDITION):
        good_word = next((w for w in GOOD_CONDITION if w in text), "bon etat")
        conf = 96 if ("facture" in text or "garantie" in text) else 90

        if scam_score >= 30:
            conf = max(10, conf - scam_score)
            return {
                "is_accessory": False, "accessory_type": None, "condition": "good",
                "confidence": conf, "scam_risk": scam_score,
                "reason": f"Bon état déclaré mais signaux suspects ({', '.join(scam_reasons)}) — vérifie avant d'acheter",
                "reason_key": "scam_moderate", "reason_params": {"score": scam_score, "word": good_word},
                "keep": True,
            }

        return {
            "is_accessory": False, "accessory_type": None, "condition": "good",
            "confidence": conf, "scam_risk": scam_score,
            "reason": f"Bon état confirmé (« {good_word} »)",
            "reason_key": "good_condition", "reason_params": {"word": good_word},
            "keep": True,
        }

    # 5) ÉTAT INCONNU → gardé (équilibré) mais confiance moyenne, ajustée si risque
    desc_len = len((description or "").strip())
    if desc_len < 15:
        conf = 45
        reason = "État non précisé + description très courte — demande photos/test avant achat"
        reason_key = "unknown_short"
    else:
        conf = 60
        reason = "État non explicitement confirmé — à vérifier avant achat"
        reason_key = "unknown"

    if scam_score >= 30:
        conf = max(5, conf - scam_score)
        reason = f"Signaux suspects détectés ({', '.join(scam_reasons)}) — grande prudence recommandée"
        reason_key = "scam_moderate"

    return {
        "is_accessory": False, "accessory_type": None, "condition": "unknown",
        "confidence": conf, "scam_risk": scam_score,
        "reason": reason, "reason_key": reason_key,
        "reason_params": {"score": scam_score} if scam_score >= 30 else {},
        "keep": True,
    }
