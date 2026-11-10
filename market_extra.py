"""
market_extra.py — Modèles absents de market_db.py (variantes très courantes)
═══════════════════════════════════════════════════════════════════════════
market_db.py couvre les modèles « phares » ; le marché de l'occasion est fait
de variantes qu'il ignore : Intel sans « K » (i5-12400, i7-12700…), versions
« KF » (sans graphique intégré), AMD « GT » / « XT » / « G » récents.

⚠️  CES PRIX SONT DES ESTIMATIONS, pas des relevés. Ils sont dérivés du modèle
voisin de market_db.py (ex. un « KF » vaut ~5 % de moins que le « K », un
modèle avec graphique intégré ~7-10 % de plus que son « F »). Je n'ai pas pu
les comparer à des annonces réelles. Le bot les traite donc avec prudence :
  • seuils plus stricts (« bon » à 80 % et « affaire » à 65 % du prix, au lieu
    de 83 % et 68 %) ;
  • le prix OBSERVÉ sur les annonces réellement vues prend le pas très vite
    (voir ram_composants.prix_reference) ;
  • la notification indique « prix de référence estimé ».

Format : nom → (catégorie, prix_juste_€). Ajouter un modèle = une ligne.
"""

# (catégorie, fair)
EXTRA = {
    # ── Intel 12e génération ──
    "i3-12100F": ("CPU", 70), "i3-12100": ("CPU", 80),
    "i5-12400": ("CPU", 110), "i5-12600": ("CPU", 130),
    "i5-12600KF": ("CPU", 130), "i7-12700": ("CPU", 165),
    "i7-12700F": ("CPU", 150), "i7-12700KF": ("CPU", 170),
    "i9-12900KF": ("CPU", 205),
    # ── Intel 13e génération ──
    "i3-13100F": ("CPU", 85), "i5-13400": ("CPU", 145),
    "i5-13500": ("CPU", 175), "i5-13600KF": ("CPU", 170),
    "i7-13700F": ("CPU", 225), "i7-13700KF": ("CPU", 240),
    "i9-13900KF": ("CPU", 335),
    # ── Intel 14e génération ──
    "i5-14400": ("CPU", 160), "i5-14500": ("CPU", 190),
    "i5-14600KF": ("CPU", 210), "i7-14700F": ("CPU", 260),
    "i7-14700KF": ("CPU", 285), "i9-14900KF": ("CPU", 360),
    # ── Intel 10e / 11e / 9e génération ──
    "i5-11400F": ("CPU", 85), "i5-11400": ("CPU", 95),
    "i5-11600KF": ("CPU", 85), "i7-11700KF": ("CPU", 105),
    "i3-10100F": ("CPU", 45), "i5-10400": ("CPU", 70),
    "i5-10600KF": ("CPU", 70), "i7-10700KF": ("CPU", 95),
    "i9-10900KF": ("CPU", 125), "i5-9600KF": ("CPU", 65),
    "i7-9700KF": ("CPU", 85), "i9-9900KF": ("CPU", 105),
    # ── AMD récents ──
    "Ryzen 5 5600GT": ("CPU", 105), "Ryzen 5 5500GT": ("CPU", 85),
    "Ryzen 5 5600XT": ("CPU", 100), "Ryzen 7 5800XT": ("CPU", 190),
    "Ryzen 9 5900XT": ("CPU", 250), "Ryzen 5 4500": ("CPU", 65),
    "Ryzen 5 8400F": ("CPU", 105), "Ryzen 5 8500G": ("CPU", 120),
    "Ryzen 7 8700G": ("CPU", 230), "Ryzen 5 3500X": ("CPU", 60),
    "Ryzen 3 3300X": ("CPU", 65), "Ryzen 3 3100": ("CPU", 45),
    "Ryzen 5 2400G": ("CPU", 40), "Ryzen 7 1700": ("CPU", 30),
    "Ryzen 7 1800X": ("CPU", 40),
    # ── Cartes graphiques ──
    "RTX 5050": ("GPU", 230), "RTX 3050 6GB": ("GPU", 115),
    "RTX 2060 12GB": ("GPU", 150), "GTX 1630": ("GPU", 65),
    "RX 6400": ("GPU", 65), "Radeon VII": ("GPU", 200),
    "Arc A580": ("GPU", 130), "Arc A380": ("GPU", 70),
}

# Seuils plus stricts pour les estimations (market_db : 0.83 / 0.68).
RATIO_BON = 0.80
RATIO_AFFAIRE = 0.65
