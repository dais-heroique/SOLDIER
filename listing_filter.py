"""
listing_filter.py — Analyse de pertinence + état d'une annonce
═══════════════════════════════════════════════════════════════════════
Résout le problème: une recherche "RTX 3070" peut remonter :
  - juste le CARTON vide / la boîte
  - un CÂBLE (riser, PCIe, alim), un ADAPTATEUR
  - un WATERBLOCK / backplate / support anti-sag (accessoire, pas la carte)
  - une carte HS / pour pièces / avec artefacts
  - un autocollant, un stand, un ventilateur seul...

Ce module classe chaque annonce et renvoie:
  - is_accessory : True si l'annonce ne vend PAS le composant lui-même
  - condition    : "good" / "unknown" / "bad"
  - confidence   : 0-100 (confiance que c'est le vrai composant en bon état)
  - reason       : explication courte (affichée en flag)

Niveau ÉQUILIBRÉ:
  - on EXCLUT le clairement accessoire/carton/HS
  - on GARDE le reste avec un score de confiance (état inconnu = gardé, signalé)
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
    ],
    "support/accessoire": [
        r"\bsupport (gpu|carte|anti-?sag)\b", r"\banti-?sag\b", r"\bstand (gpu|vertical)\b",
        r"\bkit vertical\b", r"\bplaque\b.{0,10}\brgb\b", r"\bautocollant", r"\bsticker",
        r"\bplexi\b", r"\bshroud\b",
    ],
    "recherche/achat": [
        r"\bje recherche\b", r"\brecherche (une|un|des)\b", r"\bj'achète\b",
        r"\bcherche à acheter\b", r"\bachat\b.{0,6}\brecherche\b",
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
]

# ── Indices que c'est BIEN le composant (renforce la confiance) ──
def _has_word(text, words):
    return any(w in text for w in words)

def _matches_any(text, patterns):
    for p in patterns:
        if re.search(p, text):
            return True
    return False


def analyze(subject, description, category=None):
    """
    Analyse une annonce. Retourne un dict:
      {is_accessory, accessory_type, condition, confidence, reason, keep,
       reason_key, reason_params}
    keep=False -> on n'affiche/alerte pas (accessoire clair ou HS).
    reason_key/reason_params permettent au dashboard de traduire le message
    (le champ "reason" reste la version francaise prete a l'emploi).
    """
    text = ((subject or "") + " " + (description or "")).lower()

    for label, patterns in ACCESSORY_PATTERNS.items():
        if _matches_any(text, patterns):
            return {
                "is_accessory": True,
                "accessory_type": label,
                "condition": "unknown",
                "confidence": 0,
                "reason": f"🚫 Accessoire détecté ({label}) — pas le composant lui-même",
                "reason_key": "accessory",
                "reason_params": {"type": label},
                "keep": False,
            }

    if _has_word(text, BAD_CONDITION):
        bad_word = next((w for w in BAD_CONDITION if w in text), "defaut")
        return {
            "is_accessory": False,
            "accessory_type": None,
            "condition": "bad",
            "confidence": 0,
            "reason": f"🔴 État probablement HS/défectueux (« {bad_word} »)",
            "reason_key": "bad_condition",
            "reason_params": {"word": bad_word},
            "keep": False,
        }

    if _has_word(text, GOOD_CONDITION):
        good_word = next((w for w in GOOD_CONDITION if w in text), "bon etat")
        conf = 90
        if "facture" in text or "garantie" in text:
            conf = 96
        return {
            "is_accessory": False,
            "accessory_type": None,
            "condition": "good",
            "confidence": conf,
            "reason": f"✅ Bon état confirmé (« {good_word} »)",
            "reason_key": "good_condition",
            "reason_params": {"word": good_word},
            "keep": True,
        }

    desc_len = len((description or "").strip())
    if desc_len < 15:
        conf = 45
        reason = "ℹ️ État non précisé + description très courte — demande photos/test avant achat"
        reason_key = "unknown_short"
    else:
        conf = 60
        reason = "ℹ️ État non explicitement confirmé — à vérifier avant achat"
        reason_key = "unknown"

    return {
        "is_accessory": False,
        "accessory_type": None,
        "condition": "unknown",
        "confidence": conf,
        "reason": reason,
        "reason_key": reason_key,
        "reason_params": {},
        "keep": True,
    }
