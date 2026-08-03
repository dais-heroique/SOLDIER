"""
confidence.py — Score de confiance anti-bourrage de mots-clés (SOLDIER)
═══════════════════════════════════════════════════════════════════════════
Problème réel : des vendeurs bourrent le titre/description de plusieurs noms
de modèles ("RTX 4090 4080 4070 i9 compatible avec...") pour apparaître dans
plus de recherches. Le scanner croit alors avoir trouvé une 4090 à 200€ alors
que c'est un boîtier vide ou une carte bas de gamme qui ne fait que MENTIONNER
la 4090 en passant (comme accessoire compatible, comparaison, etc.).

Ce module calcule un `confidence_score` (0-100) DISTINCT du scam_score
existant (listing_filter.py) — le scam_score détecte les arnaques (paiement
externe, urgence...), celui-ci détecte spécifiquement le bourrage de
mots-clés qui fausse l'identification du produit.

Quatre niveaux, du moins cher au plus coûteux :
  1. Cohérence prix/marché — un prix bien trop bas pour le modèle annoncé
     est suspect (déjà en partie couvert par le scam_score, durci ici).
  2. Densité de mots-clés — combien de modèles GPU/CPU DISTINCTS sont
     mentionnés dans le texte ; au-delà de 2-3, c'est un signal de bourrage.
  3. Position du modèle — dans le titre = confiance haute ; seulement noyé
     dans une description bourrée = confiance basse.
  4. Vérification visuelle (optionnelle, gated) — envoie la photo à l'API
     Anthropic (vision) pour confirmer que le produit visible correspond au
     modèle annoncé. Coûte des tokens : n'est lancé QUE sur les annonces
     ayant déjà passé les niveaux 1-3 (candidats sérieux), jamais sur tout
     le flux brut.
"""

import re
import os

# Liste de tous les noms de modèles GPU/CPU connus (pour détecter la densité
# de mots-clés distincts mentionnés dans une annonce)
try:
    from perf_db import GPU_PERF, CPU_PERF
    ALL_MODEL_NAMES = list(GPU_PERF.keys()) + list(CPU_PERF.keys())
except Exception:
    ALL_MODEL_NAMES = []


def _normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _count_distinct_models_mentioned(text, exclude_model=None):
    """Compte combien de modèles GPU/CPU DIFFÉRENTS (hors celui annoncé) sont
    mentionnés dans le texte — signal de bourrage si trop nombreux."""
    flat = _normalize(text)
    excl = _normalize(exclude_model) if exclude_model else None
    found = set()
    for name in ALL_MODEL_NAMES:
        n = _normalize(name)
        if len(n) < 4:
            continue
        if excl and n == excl:
            continue
        if n in flat:
            found.add(name)
    return len(found), found


def level1_price_coherence(price, fair_price, model):
    """Prix bien trop bas pour le modèle annoncé = suspect. Retourne
    (pénalité 0-40, raison ou None)."""
    if not price or not fair_price or fair_price <= 0:
        return 0, None
    ratio = price / fair_price
    if ratio < 0.15:
        return 40, f"prix à {ratio*100:.0f}% du marché pour ce modèle — quasiment impossible"
    if ratio < 0.25:
        return 20, f"prix à {ratio*100:.0f}% du marché — très suspect"
    return 0, None


def level2_keyword_density(subject, description, model):
    """Trop de modèles distincts mentionnés = signal de bourrage. Retourne
    (pénalité 0-40, raison ou None)."""
    text = (subject or "") + " " + (description or "")
    count, found = _count_distinct_models_mentioned(text, exclude_model=model)
    if count >= 4:
        return 40, f"{count} autres modèles mentionnés dans l'annonce (bourrage probable)"
    if count >= 2:
        return 20, f"{count} autres modèles mentionnés dans l'annonce"
    return 0, None


def level3_match_position(subject, description, model_tokens_flat):
    """Le modèle doit apparaître dans le TITRE, pas seulement noyé dans une
    longue description. Retourne (pénalité 0-25, raison ou None)."""
    subj_flat = _normalize(subject)
    if all(tok in subj_flat for tok in model_tokens_flat if isinstance(tok, str)):
        return 0, None  # présent dans le titre -> confiance haute, pas de pénalité
    desc_len = len((description or "").strip())
    if desc_len > 300:
        return 25, "modèle absent du titre, seulement noyé dans une longue description"
    return 10, "modèle absent du titre (présent seulement en description)"


def assess(subject, description, price, fair_price, model, model_tokens_flat=None):
    """
    Calcule le confidence_score (0-100, 100 = aucun signal de bourrage) et
    la liste des raisons. Niveaux 1-3 uniquement (déterministe, gratuit).
    """
    score = 100
    reasons = []

    p1, r1 = level1_price_coherence(price, fair_price, model)
    if r1:
        score -= p1
        reasons.append(r1)

    p2, r2 = level2_keyword_density(subject, description, model)
    if r2:
        score -= p2
        reasons.append(r2)

    if model_tokens_flat:
        p3, r3 = level3_match_position(subject, description, model_tokens_flat)
        if r3:
            score -= p3
            reasons.append(r3)

    score = max(0, min(100, score))
    return {"score": score, "reasons": reasons}


# ═══════════════════════════════════════════════════════════════════
#  NIVEAU 4 — Vérification visuelle (optionnelle, gated, coûte des tokens)
# ═══════════════════════════════════════════════════════════════════
VISION_CONFIDENCE_THRESHOLD = 60  # ne lance le niveau 4 que si niveaux 1-3 >= ce seuil
VISION_MODEL = "claude-sonnet-5"


def should_run_vision_check(level123_score):
    """Filtre gratuit d'abord: le niveau 4 (payant en tokens) n'est déclenché
    QUE sur les candidats ayant déjà passé les niveaux 1-3 avec un score
    correct — jamais sur tout le flux brut."""
    return level123_score >= VISION_CONFIDENCE_THRESHOLD


# Estimation prudente du coût d'un appel niveau 4 (image + réponse courte).
# Pas de facturation exacte disponible ici — sert uniquement à ne JAMAIS
# dépasser le plafond mensuel choisi par l'utilisateur dans l'onboarding/réglages.
VISION_COST_PER_CALL_EUR = 0.01


def vision_budget_status():
    """Lit l'état du budget vision (activé ?, plafond mensuel, dépense du mois
    en cours) depuis la base SOLDIER. Import local pour éviter tout risque de
    cycle d'import avec soldier_db (qui n'importe pas confidence)."""
    import time as _time
    import soldier_db
    enabled = bool(soldier_db.get_kv("vision_enabled", False))
    budget_eur = float(soldier_db.get_kv("vision_budget_eur_monthly", 0) or 0)
    month_key = "vision_spend_" + _time.strftime("%Y-%m", _time.localtime())
    spent = float(soldier_db.get_kv(month_key, 0) or 0)
    return {"enabled": enabled, "budget_eur": budget_eur, "spent_eur": spent, "month_key": month_key}


def maybe_run_vision_check(level123_score, image_url, claimed_model, api_key=None):
    """Point d'entrée défensif complet pour le niveau 4: vérifie le toggle
    utilisateur, le plafond de budget mensuel (jamais dépassé), le filtre de
    seuil gratuit, ET dégrade proprement (retourne (0, None), score inchangé)
    sur toute condition manquante ou erreur — l'annonce garde alors son score
    des niveaux 1-3."""
    if not should_run_vision_check(level123_score):
        return 0, None
    status = vision_budget_status()
    if not status["enabled"]:
        return 0, None
    if status["budget_eur"] <= 0 or status["spent_eur"] + VISION_COST_PER_CALL_EUR > status["budget_eur"]:
        return 0, None
    penalty, reason = level4_vision_check(image_url, claimed_model, api_key=api_key)
    try:
        import soldier_db
        soldier_db.set_kv(status["month_key"], status["spent_eur"] + VISION_COST_PER_CALL_EUR)
    except Exception:
        pass
    return penalty, reason


def level4_vision_check(image_url, claimed_model, api_key=None):
    """
    Envoie la photo à l'API Anthropic (vision) pour vérifier que le produit
    visible correspond au modèle annoncé. Retourne (pénalité 0-100, raison)
    ou (0, None) si la vérification n'a pas pu être faite (pas de clé API,
    pas d'image, erreur réseau — on ne pénalise jamais sur une incertitude
    technique, seulement sur une VRAIE incohérence détectée).

    Nécessite ANTHROPIC_API_KEY (variable d'environnement ou argument).
    Optionnel — désactivé par défaut, à activer dans les réglages.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not image_url:
        return 0, None

    try:
        import anthropic
    except ImportError:
        return 0, None

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=15.0)
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "url", "url": image_url}},
                    {"type": "text", "text": (
                        f"Cette photo montre-t-elle bien un(e) {claimed_model} ? "
                        f"Réponds uniquement par OUI, NON, ou INCERTAIN, suivi "
                        f"d'une très courte justification (moins de 15 mots)."
                    )},
                ],
            }],
        )
        answer = response.content[0].text.strip().upper()
        if answer.startswith("NON"):
            return 70, f"vérification visuelle: {response.content[0].text.strip()}"
        return 0, None
    except Exception:
        return 0, None  # erreur réseau/API: on ne pénalise pas sur une incertitude technique
