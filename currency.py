"""
currency.py — Conversion vers l'euro (référence interne de tout le sniper)
═══════════════════════════════════════════════════════════════════════════
Tous les prix de référence (market_db.py) sont calibrés en EUR. Si le pays
actif utilise une autre devise (UK -> GBP, Pologne -> PLN, USA -> USD...),
les annonces reviennent des plateformes dans CETTE devise locale — sans
conversion, comparer un prix en GBP à un seuil en EUR donnerait des résultats
faux (un "bon deal" pourrait être une arnaque, et inversement).

Ce module convertit tout vers l'EUR dès la réception de l'annonce, pour que
le reste du système (scoring, seuils, marge, affichage) reste 100% cohérent
peu importe le pays choisi.

⚠️ Taux approximatifs et STATIQUES (pas de connexion à une API de change en
direct). Suffisant pour classer les annonces (bon/mauvais deal), mais à
rafraîchir de temps en temps si tu veux une précision fine. Modifie
simplement les valeurs ci-dessous.
"""

# Taux approximatifs vers l'EUR (1 unité de la devise = X EUR)
EUR_RATES = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "CAD": 0.68,
    "PLN": 0.23,
    "CZK": 0.040,
    "RON": 0.20,
    "HUF": 0.0025,
    "SEK": 0.088,
    "DKK": 0.134,
    "CHF": 1.04,
    "AUD": 0.61,
    "INR": 0.011,
    "SGD": 0.69,
    "JPY": 0.0061,
    "BRL": 0.17,
    "MXN": 0.049,
}


def to_eur(amount, currency):
    """Convertit un montant depuis `currency` vers l'EUR. Retourne l'original
    si la devise est inconnue (mieux vaut ne pas planter que de mal convertir)."""
    if not amount:
        return amount
    rate = EUR_RATES.get((currency or "EUR").upper())
    if rate is None:
        return amount
    return round(amount * rate, 2)
