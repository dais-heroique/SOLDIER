"""
perf_db.py — Base de données PERFORMANCE + REVENDABILITÉ (2026)
═══════════════════════════════════════════════════════════════════════
Pour chaque GPU/CPU: un indice de performance gaming (0-100) calibré sur
les hiérarchies 2026 (Tom's Hardware, TechPowerUp, tier lists), la VRAM,
et une capacité par résolution (1080p / 1440p / 4K en AAA 2026).

Sert à calculer:
  - PERF/PRIX  : indice de perf ÷ prix → combien de "perf par euro"
  - AAA 2026 ? : est-ce jouable en triple-A aujourd'hui, à quelle réso
  - REVENDABILITÉ : score basé sur 5+ paramètres (voir scoring.py)

Échelle perf GPU: 100 = RTX 4090. Un GPU à 50 = ~moitié des perfs 1080p ultra.
Échelle perf CPU: 100 = i9/Ryzen 9 haut de gamme récent pour le gaming.
"""

# ─────────── GPU: perf (0-100), VRAM (Go), tier résolution, gen ───────────
# res: meilleure résolution AAA 2026 jouable confortablement (>=60fps high)
#   "1080p", "1440p", "4K", ou "1080p-low" (obsolète, bas détails only)
GPU_PERF = {
    # RTX 50
    "RTX 5090": {"perf": 130, "vram": 32, "res": "4K",   "rt": 100, "year": 2025},
    "RTX 5080": {"perf": 100, "vram": 16, "res": "4K",   "rt": 88,  "year": 2025},
    "RTX 5070 Ti": {"perf": 88, "vram": 16, "res": "4K", "rt": 80,  "year": 2025},
    "RTX 5070": {"perf": 74, "vram": 12, "res": "1440p", "rt": 70,  "year": 2025},
    "RTX 5060 Ti": {"perf": 58, "vram": 16, "res": "1440p","rt": 58, "year": 2025},
    "RTX 5060": {"perf": 48, "vram": 8, "res": "1080p",  "rt": 48,  "year": 2025},
    # RTX 40
    "RTX 4090": {"perf": 100, "vram": 24, "res": "4K",   "rt": 92,  "year": 2022},
    "RTX 4080 SUPER": {"perf": 88, "vram": 16, "res": "4K","rt": 82, "year": 2024},
    "RTX 4080": {"perf": 85, "vram": 16, "res": "4K",    "rt": 80,  "year": 2022},
    "RTX 4070 Ti SUPER": {"perf": 78, "vram": 16, "res": "1440p","rt": 74,"year": 2024},
    "RTX 4070 Ti": {"perf": 72, "vram": 12, "res": "1440p","rt": 70,"year": 2023},
    "RTX 4070 SUPER": {"perf": 68, "vram": 12, "res": "1440p","rt": 66,"year": 2024},
    "RTX 4070": {"perf": 60, "vram": 12, "res": "1440p", "rt": 58,  "year": 2023},
    "RTX 4060 Ti 16GB": {"perf": 48, "vram": 16, "res": "1440p","rt": 46,"year": 2023},
    "RTX 4060 Ti": {"perf": 46, "vram": 8, "res": "1080p","rt": 44, "year": 2023},
    "RTX 4060": {"perf": 40, "vram": 8, "res": "1080p",  "rt": 38,  "year": 2023},
    # RTX 30
    "RTX 3090 Ti": {"perf": 78, "vram": 24, "res": "4K", "rt": 62,  "year": 2022},
    "RTX 3090": {"perf": 72, "vram": 24, "res": "4K",    "rt": 58,  "year": 2020},
    "RTX 3080 Ti": {"perf": 70, "vram": 12, "res": "1440p","rt": 56,"year": 2021},
    "RTX 3080 12GB": {"perf": 68, "vram": 12, "res": "1440p","rt": 54,"year": 2022},
    "RTX 3080": {"perf": 65, "vram": 10, "res": "1440p", "rt": 52,  "year": 2020},
    "RTX 3070 Ti": {"perf": 55, "vram": 8, "res": "1440p","rt": 44, "year": 2021},
    "RTX 3070": {"perf": 52, "vram": 8, "res": "1440p",  "rt": 42,  "year": 2020},
    "RTX 3060 Ti": {"perf": 47, "vram": 8, "res": "1080p","rt": 38, "year": 2020},
    "RTX 3060 12GB": {"perf": 38, "vram": 12, "res": "1080p","rt": 32,"year": 2021},
    "RTX 3060": {"perf": 37, "vram": 12, "res": "1080p",  "rt": 32,  "year": 2021},
    "RTX 3050": {"perf": 27, "vram": 8, "res": "1080p",  "rt": 22,  "year": 2022},
    # RTX 20
    "RTX 2080 Ti": {"perf": 50, "vram": 11, "res": "1440p","rt": 36,"year": 2018},
    "RTX 2080 SUPER": {"perf": 43, "vram": 8, "res": "1080p","rt": 30,"year": 2019},
    "RTX 2080": {"perf": 41, "vram": 8, "res": "1080p",  "rt": 28,  "year": 2018},
    "RTX 2070 SUPER": {"perf": 38, "vram": 8, "res": "1080p","rt": 26,"year": 2019},
    "RTX 2070": {"perf": 34, "vram": 8, "res": "1080p",  "rt": 24,  "year": 2018},
    "RTX 2060 SUPER": {"perf": 33, "vram": 8, "res": "1080p","rt": 22,"year": 2019},
    "RTX 2060": {"perf": 30, "vram": 6, "res": "1080p",  "rt": 20,  "year": 2019},
    # GTX 16
    "GTX 1660 Ti": {"perf": 28, "vram": 6, "res": "1080p","rt": 0,  "year": 2019},
    "GTX 1660 SUPER": {"perf": 27, "vram": 6, "res": "1080p","rt": 0,"year": 2019},
    "GTX 1660": {"perf": 24, "vram": 6, "res": "1080p",  "rt": 0,   "year": 2019},
    "GTX 1650 SUPER": {"perf": 21, "vram": 4, "res": "1080p-low","rt": 0,"year": 2019},
    "GTX 1650": {"perf": 17, "vram": 4, "res": "1080p-low","rt": 0,"year": 2019},
    # GTX 10
    "GTX 1080 Ti": {"perf": 38, "vram": 11, "res": "1080p","rt": 0, "year": 2017},
    "GTX 1080": {"perf": 30, "vram": 8, "res": "1080p",  "rt": 0,   "year": 2016},
    "GTX 1070 Ti": {"perf": 28, "vram": 8, "res": "1080p","rt": 0,  "year": 2017},
    "GTX 1070": {"perf": 26, "vram": 8, "res": "1080p",  "rt": 0,   "year": 2016},
    "GTX 1060 6GB": {"perf": 18, "vram": 6, "res": "1080p-low","rt": 0,"year": 2016},
    "GTX 1060 3GB": {"perf": 15, "vram": 3, "res": "1080p-low","rt": 0,"year": 2016},
    "GTX 1050 Ti": {"perf": 11, "vram": 4, "res": "1080p-low","rt": 0,"year": 2016},
    "GTX 1050": {"perf": 8, "vram": 2, "res": "1080p-low","rt": 0, "year": 2016},
    # GTX 900
    "GTX 980 Ti": {"perf": 24, "vram": 6, "res": "1080p","rt": 0,   "year": 2015},
    "GTX 980": {"perf": 19, "vram": 4, "res": "1080p-low","rt": 0,  "year": 2014},
    "GTX 970": {"perf": 16, "vram": 4, "res": "1080p-low","rt": 0,  "year": 2014},
    "GTX 960": {"perf": 10, "vram": 2, "res": "1080p-low","rt": 0,  "year": 2015},
    # RX 9000
    "RX 9070 XT": {"perf": 86, "vram": 16, "res": "4K",  "rt": 72,  "year": 2025},
    "RX 9070": {"perf": 76, "vram": 16, "res": "1440p",  "rt": 64,  "year": 2025},
    "RX 9060 XT": {"perf": 52, "vram": 16, "res": "1440p","rt": 46, "year": 2025},
    # RX 7000
    "RX 7900 XTX": {"perf": 90, "vram": 24, "res": "4K", "rt": 66,  "year": 2022},
    "RX 7900 XT": {"perf": 80, "vram": 20, "res": "4K",  "rt": 60,  "year": 2022},
    "RX 7900 GRE": {"perf": 72, "vram": 16, "res": "1440p","rt": 54,"year": 2024},
    "RX 7800 XT": {"perf": 66, "vram": 16, "res": "1440p","rt": 50, "year": 2023},
    "RX 7700 XT": {"perf": 58, "vram": 12, "res": "1440p","rt": 44, "year": 2023},
    "RX 7600 XT": {"perf": 42, "vram": 16, "res": "1080p","rt": 34, "year": 2024},
    "RX 7600": {"perf": 40, "vram": 8, "res": "1080p",   "rt": 32,  "year": 2023},
    # RX 6000
    "RX 6950 XT": {"perf": 72, "vram": 16, "res": "4K",  "rt": 44,  "year": 2022},
    "RX 6900 XT": {"perf": 68, "vram": 16, "res": "1440p","rt": 42, "year": 2020},
    "RX 6800 XT": {"perf": 64, "vram": 16, "res": "1440p","rt": 40, "year": 2020},
    "RX 6800": {"perf": 58, "vram": 16, "res": "1440p",  "rt": 36,  "year": 2020},
    "RX 6750 XT": {"perf": 50, "vram": 12, "res": "1440p","rt": 32, "year": 2022},
    "RX 6700 XT": {"perf": 48, "vram": 12, "res": "1080p","rt": 30, "year": 2021},
    "RX 6700": {"perf": 44, "vram": 10, "res": "1080p",  "rt": 28,  "year": 2021},
    "RX 6650 XT": {"perf": 40, "vram": 8, "res": "1080p", "rt": 26, "year": 2022},
    "RX 6600 XT": {"perf": 38, "vram": 8, "res": "1080p", "rt": 24, "year": 2021},
    "RX 6600": {"perf": 33, "vram": 8, "res": "1080p",   "rt": 20,  "year": 2021},
    "RX 6500 XT": {"perf": 18, "vram": 4, "res": "1080p-low","rt": 0,"year": 2022},
    # RX 5000
    "RX 5700 XT": {"perf": 36, "vram": 8, "res": "1080p", "rt": 0,  "year": 2019},
    "RX 5700": {"perf": 32, "vram": 8, "res": "1080p",   "rt": 0,   "year": 2019},
    "RX 5600 XT": {"perf": 28, "vram": 6, "res": "1080p", "rt": 0,  "year": 2020},
    "RX 5500 XT": {"perf": 20, "vram": 8, "res": "1080p-low","rt": 0,"year": 2019},
    # RX 500
    "RX 590": {"perf": 18, "vram": 8, "res": "1080p-low", "rt": 0,  "year": 2018},
    "RX 580 8GB": {"perf": 17, "vram": 8, "res": "1080p-low","rt": 0,"year": 2017},
    "RX 580 4GB": {"perf": 15, "vram": 4, "res": "1080p-low","rt": 0,"year": 2017},
    "RX 570": {"perf": 14, "vram": 4, "res": "1080p-low", "rt": 0,  "year": 2017},
    "RX 560": {"perf": 8, "vram": 4, "res": "1080p-low",  "rt": 0,  "year": 2017},
    # Intel Arc
    "Arc B580": {"perf": 44, "vram": 12, "res": "1080p",  "rt": 36, "year": 2024},
    "Arc A770": {"perf": 38, "vram": 16, "res": "1080p",  "rt": 30, "year": 2022},
    "Arc A750": {"perf": 34, "vram": 8, "res": "1080p",   "rt": 26, "year": 2022},
}

# ─────────── CPU: perf gaming (0-100), cores, gen year ───────────
CPU_PERF = {
    "Ryzen 9 9950X3D": {"perf": 100, "cores": 16, "year": 2025},
    "Ryzen 9 9900X3D": {"perf": 96, "cores": 12, "year": 2025},
    "Ryzen 7 9800X3D": {"perf": 98, "cores": 8, "year": 2024},
    "Ryzen 9 9950X": {"perf": 90, "cores": 16, "year": 2024},
    "Ryzen 9 9900X": {"perf": 84, "cores": 12, "year": 2024},
    "Ryzen 7 9700X": {"perf": 80, "cores": 8, "year": 2024},
    "Ryzen 5 9600X": {"perf": 74, "cores": 6, "year": 2024},
    "Ryzen 9 7950X3D": {"perf": 94, "cores": 16, "year": 2023},
    "Ryzen 9 7950X": {"perf": 86, "cores": 16, "year": 2022},
    "Ryzen 9 7900X3D": {"perf": 88, "cores": 12, "year": 2023},
    "Ryzen 9 7900X": {"perf": 80, "cores": 12, "year": 2022},
    "Ryzen 7 7800X3D": {"perf": 92, "cores": 8, "year": 2023},
    "Ryzen 7 7700X": {"perf": 76, "cores": 8, "year": 2022},
    "Ryzen 7 7700": {"perf": 74, "cores": 8, "year": 2023},
    "Ryzen 5 7600X": {"perf": 70, "cores": 6, "year": 2022},
    "Ryzen 5 7600": {"perf": 68, "cores": 6, "year": 2023},
    "Ryzen 5 7500F": {"perf": 64, "cores": 6, "year": 2023},
    "Ryzen 9 5950X": {"perf": 74, "cores": 16, "year": 2020},
    "Ryzen 9 5900X": {"perf": 70, "cores": 12, "year": 2020},
    "Ryzen 7 5800X3D": {"perf": 78, "cores": 8, "year": 2022},
    "Ryzen 7 5800X": {"perf": 64, "cores": 8, "year": 2020},
    "Ryzen 7 5700X3D": {"perf": 72, "cores": 8, "year": 2024},
    "Ryzen 7 5700X": {"perf": 60, "cores": 8, "year": 2022},
    "Ryzen 7 5700G": {"perf": 56, "cores": 8, "year": 2021},
    "Ryzen 5 5600X": {"perf": 58, "cores": 6, "year": 2020},
    "Ryzen 5 5600": {"perf": 56, "cores": 6, "year": 2022},
    "Ryzen 5 5600G": {"perf": 50, "cores": 6, "year": 2021},
    "Ryzen 5 5500": {"perf": 44, "cores": 6, "year": 2022},
    "Ryzen 9 3900X": {"perf": 54, "cores": 12, "year": 2019},
    "Ryzen 7 3800X": {"perf": 48, "cores": 8, "year": 2019},
    "Ryzen 7 3700X": {"perf": 46, "cores": 8, "year": 2019},
    "Ryzen 5 3600X": {"perf": 42, "cores": 6, "year": 2019},
    "Ryzen 5 3600": {"perf": 40, "cores": 6, "year": 2019},
    "Ryzen 5 3400G": {"perf": 28, "cores": 4, "year": 2019},
    "Ryzen 7 2700X": {"perf": 34, "cores": 8, "year": 2018},
    "Ryzen 5 2600": {"perf": 28, "cores": 6, "year": 2018},
    "Ryzen 5 1600": {"perf": 24, "cores": 6, "year": 2017},
    "Core Ultra 9 285K": {"perf": 92, "cores": 24, "year": 2024},
    "Core Ultra 7 265K": {"perf": 84, "cores": 20, "year": 2024},
    "Core Ultra 5 245K": {"perf": 74, "cores": 14, "year": 2024},
    "i9-14900K": {"perf": 94, "cores": 24, "year": 2023},
    "i7-14700K": {"perf": 86, "cores": 20, "year": 2023},
    "i5-14600K": {"perf": 76, "cores": 14, "year": 2023},
    "i5-14400F": {"perf": 62, "cores": 10, "year": 2024},
    "i9-13900K": {"perf": 90, "cores": 24, "year": 2022},
    "i7-13700K": {"perf": 82, "cores": 16, "year": 2022},
    "i5-13600K": {"perf": 74, "cores": 14, "year": 2022},
    "i5-13400F": {"perf": 60, "cores": 10, "year": 2023},
    "i9-12900K": {"perf": 80, "cores": 16, "year": 2021},
    "i7-12700K": {"perf": 72, "cores": 12, "year": 2021},
    "i5-12600K": {"perf": 66, "cores": 10, "year": 2021},
    "i5-12400F": {"perf": 54, "cores": 6, "year": 2022},
    "i9-11900K": {"perf": 58, "cores": 8, "year": 2021},
    "i7-11700K": {"perf": 52, "cores": 8, "year": 2021},
    "i5-11600K": {"perf": 46, "cores": 6, "year": 2021},
    "i9-10900K": {"perf": 54, "cores": 10, "year": 2020},
    "i7-10700K": {"perf": 48, "cores": 8, "year": 2020},
    "i5-10600K": {"perf": 42, "cores": 6, "year": 2020},
    "i5-10400F": {"perf": 36, "cores": 6, "year": 2020},
    "i9-9900K": {"perf": 46, "cores": 8, "year": 2018},
    "i7-9700K": {"perf": 42, "cores": 8, "year": 2018},
    "i5-9600K": {"perf": 36, "cores": 6, "year": 2018},
    "i5-9400F": {"perf": 30, "cores": 6, "year": 2019},
    "i7-8700K": {"perf": 40, "cores": 6, "year": 2017},
    "i7-8700": {"perf": 38, "cores": 6, "year": 2017},
    "i5-8600K": {"perf": 32, "cores": 6, "year": 2017},
    "i5-8400": {"perf": 28, "cores": 6, "year": 2017},
    "i7-7700K": {"perf": 30, "cores": 4, "year": 2017},
    "i7-7700": {"perf": 28, "cores": 4, "year": 2017},
    "i5-7600K": {"perf": 24, "cores": 4, "year": 2017},
    "i5-7500": {"perf": 20, "cores": 4, "year": 2017},
    "i7-6700K": {"perf": 27, "cores": 4, "year": 2015},
    "i5-6600K": {"perf": 22, "cores": 4, "year": 2015},
    "i5-6500": {"perf": 18, "cores": 4, "year": 2015},
}

# ─────────── DEMANDE / POPULARITÉ (proxy revendabilité) ───────────
# Score 0-100 = à quel point ce composant se revend vite/facilement en 2026.
# Basé sur: volume de recherche, popularité Steam survey, présence marché occasion.
# Les modèles très demandés (mainstream récents) = haute liquidité.
DEMAND = {
    # GPU forte demande occasion
    "RTX 3060": 92, "RTX 3060 Ti": 90, "RTX 3070": 88, "RTX 3070 Ti": 78,
    "RTX 3080": 82, "RTX 4060": 88, "RTX 4060 Ti": 80, "RTX 4070": 82,
    "RTX 4070 SUPER": 78, "RTX 2060": 80, "RTX 2070 SUPER": 72,
    "GTX 1660 SUPER": 82, "GTX 1660 Ti": 74, "GTX 1080 Ti": 76, "GTX 1070": 74,
    "GTX 1060 6GB": 78, "RX 580 8GB": 80, "RX 6600": 82, "RX 6600 XT": 76,
    "RX 6700 XT": 78, "RX 6650 XT": 72, "RX 5700 XT": 68, "RX 7600": 70,
    "RX 7800 XT": 72, "RTX 4090": 74, "RTX 3050": 66, "RTX 5070": 70,
    # CPU forte demande
    "Ryzen 5 5600": 92, "Ryzen 5 5600X": 90, "Ryzen 7 5800X3D": 88,
    "Ryzen 5 3600": 86, "Ryzen 7 5700X": 82, "Ryzen 7 3700X": 76,
    "Ryzen 5 7600": 74, "Ryzen 7 7800X3D": 84, "i5-12400F": 84,
    "i5-13400F": 78, "i7-12700K": 74, "i5-12600K": 72, "i5-10400F": 74,
    "i7-8700K": 66, "i5-8400": 62, "Ryzen 5 2600": 68,
}
DEMAND_DEFAULT = 55   # filet de sécurité si aucune heuristique n'est calculable


def demand_for(category, model, fair_price, year=None):
    """
    Calcule une demande RÉELLE et DIFFÉRENCIÉE par modèle plutôt qu'une valeur
    fixe identique partout. Deux composantes:
      1. Le prix "fair" du modèle -> les composants milieu de gamme (le plus
         gros volume d'acheteurs) ont la meilleure liquidité; le très haut de
         gamme et le très bas/ancien se revendent moins vite.
      2. La fraîcheur (année) -> une génération encore courante se revend
         plus vite qu'une génération obsolète depuis longtemps.
    Le dict DEMAND (curatorial, ~43 modèles connus pour être particulièrement
    recherchés) reste une SURCOUCHE prioritaire quand elle existe.
    """
    if model in DEMAND:
        return DEMAND[model]

    if category == "GPU" or category == "CPU":
        # courbe en cloche centrée sur la fourchette "milieu de gamme populaire"
        if fair_price < 40:
            base = 42
        elif fair_price < 90:
            base = 58
        elif fair_price < 160:
            base = 72
        elif fair_price < 280:
            base = 85       # zone la plus liquide (le gros du marché occasion)
        elif fair_price < 450:
            base = 78
        elif fair_price < 700:
            base = 66
        elif fair_price < 1200:
            base = 56
        else:
            base = 46        # très haut de gamme: peu d'acheteurs, mais motivés

        if year:
            age = 2026 - year
            # pénalité douce pour l'ancienneté, plafonnée pour ne pas écraser le prix
            base -= min(18, max(0, age - 3) * 2)

        return max(15, min(96, round(base)))

    # autres catégories (RAM, stockage, PSU, boîtiers, périphériques...):
    # moins de données de perf, on reste sur une estimation par tranche de prix
    if fair_price < 20:
        return 50
    elif fair_price < 80:
        return 62
    elif fair_price < 200:
        return 58
    else:
        return 48
