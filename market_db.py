"""
market_db.py — Base de prix de référence (marché occasion FR, juin 2026)
═══════════════════════════════════════════════════════════════════════
255 modèles sur 14 catégories. Chaque modèle:
  - "fair"  : prix occasion juste médian (€) → prix de REVENTE cible
  - "good"  : seuil bon deal (~0.83×fair) → on alerte
  - "steal" : seuil affaire en or (~0.68×fair) → alerte maximale
  - "queries": termes de recherche Leboncoin/Vinted
  - "watts" : (PSU uniquement) puissance pour filtrer ≥550W

Calibré sur recherches réelles Leboncoin/comparateurs juin 2026.
⚠️ RAM (surtout DDR5) flambée fin 2025 (pénurie DRAM) → seuils élevés.
Ajuste librement good/steal. Ajoute des modèles en copiant une ligne.
"""

# ────────────────────────────────────────────────────────────
# 🎮 Cartes graphiques
# ────────────────────────────────────────────────────────────
GPU = {
    "RTX 5090": {"fair": 2100, "good": 1743, "steal": 1428, "queries": ["RTX 5090"]},
    "RTX 5080": {"fair": 1150, "good": 954, "steal": 782, "queries": ["RTX 5080"]},
    "RTX 5070 Ti": {"fair": 800, "good": 664, "steal": 544, "queries": ["RTX 5070 Ti", "5070Ti"]},
    "RTX 5070": {"fair": 580, "good": 481, "steal": 394, "queries": ["RTX 5070"]},
    "RTX 5060 Ti": {"fair": 420, "good": 349, "steal": 286, "queries": ["RTX 5060 Ti", "5060Ti"]},
    "RTX 5060": {"fair": 320, "good": 266, "steal": 218, "queries": ["RTX 5060"]},
    "RTX 4090": {"fair": 1500, "good": 1245, "steal": 1020, "queries": ["RTX 4090"]},
    "RTX 4080 SUPER": {"fair": 850, "good": 706, "steal": 578, "queries": ["RTX 4080 Super", "4080S"]},
    "RTX 4080": {"fair": 780, "good": 647, "steal": 530, "queries": ["RTX 4080"]},
    "RTX 4070 Ti SUPER": {"fair": 650, "good": 540, "steal": 442, "queries": ["RTX 4070 Ti Super", "4070 Ti S"]},
    "RTX 4070 Ti": {"fair": 560, "good": 465, "steal": 381, "queries": ["RTX 4070 Ti"]},
    "RTX 4070 SUPER": {"fair": 480, "good": 398, "steal": 326, "queries": ["RTX 4070 Super", "4070S"]},
    "RTX 4070": {"fair": 420, "good": 349, "steal": 286, "queries": ["RTX 4070"]},
    "RTX 4060 Ti 16GB": {"fair": 330, "good": 274, "steal": 224, "queries": ["RTX 4060 Ti 16"]},
    "RTX 4060 Ti": {"fair": 300, "good": 249, "steal": 204, "queries": ["RTX 4060 Ti", "4060Ti"]},
    "RTX 4060": {"fair": 230, "good": 191, "steal": 156, "queries": ["RTX 4060"]},
    "RTX 3090 Ti": {"fair": 700, "good": 581, "steal": 476, "queries": ["RTX 3090 Ti"]},
    "RTX 3090": {"fair": 600, "good": 498, "steal": 408, "queries": ["RTX 3090"]},
    "RTX 3080 Ti": {"fair": 480, "good": 398, "steal": 326, "queries": ["RTX 3080 Ti"]},
    "RTX 3080 12GB": {"fair": 430, "good": 357, "steal": 292, "queries": ["RTX 3080 12"]},
    "RTX 3080": {"fair": 380, "good": 315, "steal": 258, "queries": ["RTX 3080"]},
    "RTX 3070 Ti": {"fair": 300, "good": 249, "steal": 204, "queries": ["RTX 3070 Ti"]},
    "RTX 3070": {"fair": 270, "good": 224, "steal": 184, "queries": ["RTX 3070"]},
    "RTX 3060 Ti": {"fair": 230, "good": 191, "steal": 156, "queries": ["RTX 3060 Ti", "3060Ti"]},
    "RTX 3060 12GB": {"fair": 200, "good": 166, "steal": 136, "queries": ["RTX 3060 12"]},
    "RTX 3060": {"fair": 195, "good": 162, "steal": 133, "queries": ["RTX 3060"]},
    "RTX 3050": {"fair": 140, "good": 116, "steal": 95, "queries": ["RTX 3050"]},
    "RTX 2080 Ti": {"fair": 280, "good": 232, "steal": 190, "queries": ["RTX 2080 Ti"]},
    "RTX 2080 SUPER": {"fair": 200, "good": 166, "steal": 136, "queries": ["RTX 2080 Super"]},
    "RTX 2080": {"fair": 180, "good": 149, "steal": 122, "queries": ["RTX 2080"]},
    "RTX 2070 SUPER": {"fair": 170, "good": 141, "steal": 116, "queries": ["RTX 2070 Super"]},
    "RTX 2070": {"fair": 150, "good": 124, "steal": 102, "queries": ["RTX 2070"]},
    "RTX 2060 SUPER": {"fair": 140, "good": 116, "steal": 95, "queries": ["RTX 2060 Super"]},
    "RTX 2060": {"fair": 120, "good": 100, "steal": 82, "queries": ["RTX 2060"]},
    "GTX 1660 Ti": {"fair": 110, "good": 91, "steal": 75, "queries": ["GTX 1660 Ti"]},
    "GTX 1660 SUPER": {"fair": 105, "good": 87, "steal": 71, "queries": ["GTX 1660 Super", "1660S"]},
    "GTX 1660": {"fair": 90, "good": 75, "steal": 61, "queries": ["GTX 1660"]},
    "GTX 1650 SUPER": {"fair": 80, "good": 66, "steal": 54, "queries": ["GTX 1650 Super"]},
    "GTX 1650": {"fair": 70, "good": 58, "steal": 48, "queries": ["GTX 1650"]},
    "GTX 1080 Ti": {"fair": 160, "good": 133, "steal": 109, "queries": ["GTX 1080 Ti"]},
    "GTX 1080": {"fair": 120, "good": 100, "steal": 82, "queries": ["GTX 1080"]},
    "GTX 1070 Ti": {"fair": 100, "good": 83, "steal": 68, "queries": ["GTX 1070 Ti"]},
    "GTX 1070": {"fair": 90, "good": 75, "steal": 61, "queries": ["GTX 1070"]},
    "GTX 1060 6GB": {"fair": 60, "good": 50, "steal": 41, "queries": ["GTX 1060 6"]},
    "GTX 1060 3GB": {"fair": 45, "good": 37, "steal": 31, "queries": ["GTX 1060 3"]},
    "GTX 1050 Ti": {"fair": 55, "good": 46, "steal": 37, "queries": ["GTX 1050 Ti"]},
    "GTX 1050": {"fair": 40, "good": 33, "steal": 27, "queries": ["GTX 1050"]},
    "GTX 980 Ti": {"fair": 90, "good": 75, "steal": 61, "queries": ["GTX 980 Ti"]},
    "GTX 980": {"fair": 70, "good": 58, "steal": 48, "queries": ["GTX 980"]},
    "GTX 970": {"fair": 50, "good": 42, "steal": 34, "queries": ["GTX 970"]},
    "GTX 960": {"fair": 35, "good": 29, "steal": 24, "queries": ["GTX 960"]},
    "GTX 780 Ti": {"fair": 40, "good": 33, "steal": 27, "queries": ["GTX 780 Ti"]},
    "GTX 780": {"fair": 30, "good": 25, "steal": 20, "queries": ["GTX 780"]},
    "GTX 770": {"fair": 25, "good": 21, "steal": 17, "queries": ["GTX 770"]},
    "GTX 760": {"fair": 20, "good": 17, "steal": 14, "queries": ["GTX 760"]},
    "GTX 750 Ti": {"fair": 22, "good": 18, "steal": 15, "queries": ["GTX 750 Ti"]},
    "GTX 750": {"fair": 15, "good": 12, "steal": 10, "queries": ["GTX 750"]},
    "GTX 680": {"fair": 25, "good": 21, "steal": 17, "queries": ["GTX 680"]},
    "GTX 670": {"fair": 22, "good": 18, "steal": 15, "queries": ["GTX 670"]},
    "GTX 660": {"fair": 18, "good": 15, "steal": 12, "queries": ["GTX 660"]},
    "GTX 650 Ti": {"fair": 15, "good": 12, "steal": 10, "queries": ["GTX 650 Ti"]},
    "RX 9070 XT": {"fair": 680, "good": 564, "steal": 462, "queries": ["RX 9070 XT", "9070XT"]},
    "RX 9070": {"fair": 580, "good": 481, "steal": 394, "queries": ["RX 9070"]},
    "RX 9060 XT": {"fair": 360, "good": 299, "steal": 245, "queries": ["RX 9060 XT"]},
    "RX 7900 XTX": {"fair": 700, "good": 581, "steal": 476, "queries": ["RX 7900 XTX", "7900XTX"]},
    "RX 7900 XT": {"fair": 560, "good": 465, "steal": 381, "queries": ["RX 7900 XT"]},
    "RX 7900 GRE": {"fair": 480, "good": 398, "steal": 326, "queries": ["RX 7900 GRE"]},
    "RX 7800 XT": {"fair": 420, "good": 349, "steal": 286, "queries": ["RX 7800 XT"]},
    "RX 7700 XT": {"fair": 340, "good": 282, "steal": 231, "queries": ["RX 7700 XT"]},
    "RX 7600 XT": {"fair": 260, "good": 216, "steal": 177, "queries": ["RX 7600 XT"]},
    "RX 7600": {"fair": 220, "good": 183, "steal": 150, "queries": ["RX 7600"]},
    "RX 6950 XT": {"fair": 420, "good": 349, "steal": 286, "queries": ["RX 6950 XT"]},
    "RX 6900 XT": {"fair": 380, "good": 315, "steal": 258, "queries": ["RX 6900 XT"]},
    "RX 6800 XT": {"fair": 350, "good": 290, "steal": 238, "queries": ["RX 6800 XT"]},
    "RX 6800": {"fair": 300, "good": 249, "steal": 204, "queries": ["RX 6800"]},
    "RX 6750 XT": {"fair": 260, "good": 216, "steal": 177, "queries": ["RX 6750 XT"]},
    "RX 6700 XT": {"fair": 230, "good": 191, "steal": 156, "queries": ["RX 6700 XT"]},
    "RX 6700": {"fair": 200, "good": 166, "steal": 136, "queries": ["RX 6700"]},
    "RX 6650 XT": {"fair": 180, "good": 149, "steal": 122, "queries": ["RX 6650 XT"]},
    "RX 6600 XT": {"fair": 170, "good": 141, "steal": 116, "queries": ["RX 6600 XT"]},
    "RX 6600": {"fair": 140, "good": 116, "steal": 95, "queries": ["RX 6600"]},
    "RX 6500 XT": {"fair": 90, "good": 75, "steal": 61, "queries": ["RX 6500 XT"]},
    "RX 5700 XT": {"fair": 150, "good": 124, "steal": 102, "queries": ["RX 5700 XT"]},
    "RX 5700": {"fair": 130, "good": 108, "steal": 88, "queries": ["RX 5700"]},
    "RX 5600 XT": {"fair": 110, "good": 91, "steal": 75, "queries": ["RX 5600 XT"]},
    "RX 5500 XT": {"fair": 80, "good": 66, "steal": 54, "queries": ["RX 5500 XT"]},
    "RX 590": {"fair": 75, "good": 62, "steal": 51, "queries": ["RX 590"]},
    "RX 580 8GB": {"fair": 65, "good": 54, "steal": 44, "queries": ["RX 580 8"]},
    "RX 580 4GB": {"fair": 50, "good": 42, "steal": 34, "queries": ["RX 580 4"]},
    "RX 570": {"fair": 50, "good": 42, "steal": 34, "queries": ["RX 570"]},
    "RX 560": {"fair": 35, "good": 29, "steal": 24, "queries": ["RX 560"]},
    "R9 290": {"fair": 35, "good": 29, "steal": 24, "queries": ["R9 290", "Radeon R9 290"]},
    "R9 280X": {"fair": 28, "good": 23, "steal": 19, "queries": ["R9 280X", "Radeon R9 280X"]},
    "R9 270X": {"fair": 20, "good": 17, "steal": 14, "queries": ["R9 270X", "Radeon R9 270X"]},
    "R7 260X": {"fair": 15, "good": 12, "steal": 10, "queries": ["R7 260X", "Radeon R7 260X"]},
    "HD 7970": {"fair": 25, "good": 21, "steal": 17, "queries": ["HD 7970", "Radeon HD 7970"]},
    "HD 7870": {"fair": 18, "good": 15, "steal": 12, "queries": ["HD 7870", "Radeon HD 7870"]},
    "HD 6870": {"fair": 12, "good": 10, "steal": 8, "queries": ["HD 6870", "Radeon HD 6870"]},
    "Arc B580": {"fair": 280, "good": 232, "steal": 190, "queries": ["Arc B580", "Intel Arc B580"]},
    "Arc A770": {"fair": 220, "good": 183, "steal": 150, "queries": ["Arc A770"]},
    "Arc A750": {"fair": 170, "good": 141, "steal": 116, "queries": ["Arc A750"]},
}

# ────────────────────────────────────────────────────────────
# 🧠 Processeurs
# ────────────────────────────────────────────────────────────
CPU = {
    "Ryzen 9 9950X3D": {"fair": 620, "good": 515, "steal": 422, "queries": ["9950X3D"]},
    "Ryzen 9 9900X3D": {"fair": 480, "good": 398, "steal": 326, "queries": ["9900X3D"]},
    "Ryzen 7 9800X3D": {"fair": 480, "good": 398, "steal": 326, "queries": ["9800X3D"]},
    "Ryzen 9 9950X": {"fair": 480, "good": 398, "steal": 326, "queries": ["Ryzen 9 9950X", "9950X"]},
    "Ryzen 9 9900X": {"fair": 360, "good": 299, "steal": 245, "queries": ["Ryzen 9 9900X", "9900X"]},
    "Ryzen 7 9700X": {"fair": 280, "good": 232, "steal": 190, "queries": ["Ryzen 7 9700X", "9700X"]},
    "Ryzen 5 9600X": {"fair": 200, "good": 166, "steal": 136, "queries": ["Ryzen 5 9600X", "9600X"]},
    "Ryzen 5 9600": {"fair": 180, "good": 149, "steal": 122, "queries": ["Ryzen 5 9600", "9600"]},
    "Ryzen 9 7950X3D": {"fair": 420, "good": 349, "steal": 286, "queries": ["7950X3D"]},
    "Ryzen 9 7950X": {"fair": 340, "good": 282, "steal": 231, "queries": ["Ryzen 9 7950X", "7950X"]},
    "Ryzen 9 7900X3D": {"fair": 320, "good": 266, "steal": 218, "queries": ["7900X3D"]},
    "Ryzen 9 7900X": {"fair": 280, "good": 232, "steal": 190, "queries": ["Ryzen 9 7900X", "7900X"]},
    "Ryzen 7 7800X3D": {"fair": 330, "good": 274, "steal": 224, "queries": ["7800X3D"]},
    "Ryzen 7 7700X": {"fair": 220, "good": 183, "steal": 150, "queries": ["Ryzen 7 7700X", "7700X"]},
    "Ryzen 7 7700": {"fair": 200, "good": 166, "steal": 136, "queries": ["Ryzen 7 7700"]},
    "Ryzen 5 7600X": {"fair": 170, "good": 141, "steal": 116, "queries": ["Ryzen 5 7600X", "7600X"]},
    "Ryzen 5 7600": {"fair": 150, "good": 124, "steal": 102, "queries": ["Ryzen 5 7600"]},
    "Ryzen 5 7500F": {"fair": 130, "good": 108, "steal": 88, "queries": ["Ryzen 5 7500F", "7500F"]},
    "Ryzen 5 8600G": {"fair": 140, "good": 116, "steal": 95, "queries": ["Ryzen 5 8600G", "8600G"]},
    "Ryzen 9 5950X": {"fair": 280, "good": 232, "steal": 190, "queries": ["Ryzen 9 5950X", "5950X"]},
    "Ryzen 9 5900X": {"fair": 200, "good": 166, "steal": 136, "queries": ["Ryzen 9 5900X", "5900X"]},
    "Ryzen 7 5800X3D": {"fair": 260, "good": 216, "steal": 177, "queries": ["5800X3D"]},
    "Ryzen 7 5800X": {"fair": 150, "good": 124, "steal": 102, "queries": ["Ryzen 7 5800X", "5800X"]},
    "Ryzen 7 5700X3D": {"fair": 180, "good": 149, "steal": 122, "queries": ["5700X3D"]},
    "Ryzen 7 5700X": {"fair": 120, "good": 100, "steal": 82, "queries": ["Ryzen 7 5700X", "5700X"]},
    "Ryzen 7 5700G": {"fair": 130, "good": 108, "steal": 88, "queries": ["Ryzen 7 5700G", "5700G"]},
    "Ryzen 5 5600X": {"fair": 100, "good": 83, "steal": 68, "queries": ["Ryzen 5 5600X", "5600X"]},
    "Ryzen 5 5600": {"fair": 90, "good": 75, "steal": 61, "queries": ["Ryzen 5 5600"]},
    "Ryzen 5 5600G": {"fair": 95, "good": 79, "steal": 65, "queries": ["Ryzen 5 5600G", "5600G"]},
    "Ryzen 5 5500": {"fair": 70, "good": 58, "steal": 48, "queries": ["Ryzen 5 5500"]},
    "Ryzen 5 5700G": {"fair": 100, "good": 83, "steal": 68, "queries": ["Ryzen 5 5700G", "5700G"]},
    "Ryzen 5 4600G": {"fair": 75, "good": 62, "steal": 51, "queries": ["Ryzen 5 4600G", "4600G"]},
    "Ryzen 9 3900X": {"fair": 130, "good": 108, "steal": 88, "queries": ["Ryzen 9 3900X", "3900X"]},
    "Ryzen 7 3800X": {"fair": 100, "good": 83, "steal": 68, "queries": ["Ryzen 7 3800X", "3800X"]},
    "Ryzen 7 3700X": {"fair": 90, "good": 75, "steal": 61, "queries": ["Ryzen 7 3700X", "3700X"]},
    "Ryzen 5 3600X": {"fair": 70, "good": 58, "steal": 48, "queries": ["Ryzen 5 3600X", "3600X"]},
    "Ryzen 5 3600": {"fair": 65, "good": 54, "steal": 44, "queries": ["Ryzen 5 3600"]},
    "Ryzen 5 3400G": {"fair": 55, "good": 46, "steal": 37, "queries": ["Ryzen 5 3400G", "3400G"]},
    "Ryzen 7 2700X": {"fair": 70, "good": 58, "steal": 48, "queries": ["Ryzen 7 2700X", "2700X"]},
    "Ryzen 5 2600": {"fair": 40, "good": 33, "steal": 27, "queries": ["Ryzen 5 2600"]},
    "Ryzen 5 2600X": {"fair": 45, "good": 37, "steal": 31, "queries": ["Ryzen 5 2600X", "2600X"]},
    "Ryzen 5 1600": {"fair": 35, "good": 29, "steal": 24, "queries": ["Ryzen 5 1600"]},
    "Ryzen 5 1600X": {"fair": 38, "good": 32, "steal": 26, "queries": ["Ryzen 5 1600X", "1600X"]},
    "Core Ultra 9 285K": {"fair": 480, "good": 398, "steal": 326, "queries": ["Ultra 9 285K", "285K"]},
    "Core Ultra 7 265K": {"fair": 320, "good": 266, "steal": 218, "queries": ["Ultra 7 265K", "265K"]},
    "Core Ultra 5 245K": {"fair": 230, "good": 191, "steal": 156, "queries": ["Ultra 5 245K", "245K"]},
    "i9-14900K": {"fair": 380, "good": 315, "steal": 258, "queries": ["i9-14900K", "14900K"]},
    "i7-14700K": {"fair": 300, "good": 249, "steal": 204, "queries": ["i7-14700K", "14700K"]},
    "i5-14600K": {"fair": 220, "good": 183, "steal": 150, "queries": ["i5-14600K", "14600K"]},
    "i5-14400F": {"fair": 150, "good": 124, "steal": 102, "queries": ["i5-14400F", "14400F"]},
    "i9-13900K": {"fair": 350, "good": 290, "steal": 238, "queries": ["i9-13900K", "13900K"]},
    "i7-13700K": {"fair": 250, "good": 208, "steal": 170, "queries": ["i7-13700K", "13700K"]},
    "i5-13600K": {"fair": 180, "good": 149, "steal": 122, "queries": ["i5-13600K", "13600K"]},
    "i5-13400F": {"fair": 130, "good": 108, "steal": 88, "queries": ["i5-13400F", "13400F"]},
    "i9-12900K": {"fair": 220, "good": 183, "steal": 150, "queries": ["i9-12900K", "12900K"]},
    "i7-12700K": {"fair": 180, "good": 149, "steal": 122, "queries": ["i7-12700K", "12700K"]},
    "i5-12600K": {"fair": 140, "good": 116, "steal": 95, "queries": ["i5-12600K", "12600K"]},
    "i5-12400F": {"fair": 100, "good": 83, "steal": 68, "queries": ["i5-12400F", "12400F"]},
    "i9-11900K": {"fair": 150, "good": 124, "steal": 102, "queries": ["i9-11900K", "11900K"]},
    "i7-11700K": {"fair": 110, "good": 91, "steal": 75, "queries": ["i7-11700K", "11700K"]},
    "i5-11600K": {"fair": 90, "good": 75, "steal": 61, "queries": ["i5-11600K", "11600K"]},
    "i9-10900K": {"fair": 130, "good": 108, "steal": 88, "queries": ["i9-10900K", "10900K"]},
    "i7-10700K": {"fair": 100, "good": 83, "steal": 68, "queries": ["i7-10700K", "10700K"]},
    "i5-10600K": {"fair": 75, "good": 62, "steal": 51, "queries": ["i5-10600K", "10600K"]},
    "i5-10400F": {"fair": 65, "good": 54, "steal": 44, "queries": ["i5-10400F", "10400F"]},
    "i9-9900K": {"fair": 110, "good": 91, "steal": 75, "queries": ["i9-9900K", "9900K"]},
    "i7-9700K": {"fair": 90, "good": 75, "steal": 61, "queries": ["i7-9700K", "9700K"]},
    "i5-9600K": {"fair": 70, "good": 58, "steal": 48, "queries": ["i5-9600K", "9600K"]},
    "i5-9400F": {"fair": 55, "good": 46, "steal": 37, "queries": ["i5-9400F", "9400F"]},
    "i7-8700K": {"fair": 80, "good": 66, "steal": 54, "queries": ["i7-8700K", "8700K"]},
    "i7-8700": {"fair": 70, "good": 58, "steal": 48, "queries": ["i7-8700"]},
    "i5-8600K": {"fair": 60, "good": 50, "steal": 41, "queries": ["i5-8600K", "8600K"]},
    "i5-8400": {"fair": 45, "good": 37, "steal": 31, "queries": ["i5-8400"]},
    "i7-7700K": {"fair": 60, "good": 50, "steal": 41, "queries": ["i7-7700K", "7700K"]},
    "i7-7700": {"fair": 45, "good": 37, "steal": 31, "queries": ["i7-7700"]},
    "i5-7600K": {"fair": 40, "good": 33, "steal": 27, "queries": ["i5-7600K", "7600K"]},
    "i5-7500": {"fair": 30, "good": 25, "steal": 20, "queries": ["i5-7500"]},
    "i7-6700K": {"fair": 50, "good": 42, "steal": 34, "queries": ["i7-6700K", "6700K"]},
    "i5-6600K": {"fair": 35, "good": 29, "steal": 24, "queries": ["i5-6600K", "6600K"]},
    "i5-6500": {"fair": 25, "good": 21, "steal": 17, "queries": ["i5-6500"]},
    "i7-4790K": {"fair": 40, "good": 33, "steal": 27, "queries": ["i7-4790K", "4790K"]},
    "i7-4770K": {"fair": 35, "good": 29, "steal": 24, "queries": ["i7-4770K", "4770K"]},
    "i5-4690K": {"fair": 28, "good": 23, "steal": 19, "queries": ["i5-4690K", "4690K"]},
    "i5-4460": {"fair": 20, "good": 17, "steal": 14, "queries": ["i5-4460"]},
    "i7-3770K": {"fair": 30, "good": 25, "steal": 20, "queries": ["i7-3770K", "3770K"]},
    "i5-3570K": {"fair": 22, "good": 18, "steal": 15, "queries": ["i5-3570K", "3570K"]},
    "i7-2600K": {"fair": 25, "good": 21, "steal": 17, "queries": ["i7-2600K", "2600K"]},
    "i5-2500K": {"fair": 18, "good": 15, "steal": 12, "queries": ["i5-2500K", "2500K"]},
    "i7-860": {"fair": 15, "good": 12, "steal": 10, "queries": ["i7-860"]},
    "i5-750": {"fair": 12, "good": 10, "steal": 8, "queries": ["i5-750"]},
    "FX-9590": {"fair": 25, "good": 21, "steal": 17, "queries": ["FX-9590", "FX 9590"]},
    "FX-8350": {"fair": 20, "good": 17, "steal": 14, "queries": ["FX-8350", "FX 8350"]},
    "FX-8320": {"fair": 17, "good": 14, "steal": 12, "queries": ["FX-8320", "FX 8320"]},
    "FX-6300": {"fair": 12, "good": 10, "steal": 8, "queries": ["FX-6300", "FX 6300"]},
    "Phenom II X6 1090T": {"fair": 15, "good": 12, "steal": 10, "queries": ["Phenom II X6 1090T", "Phenom 1090T"]},
    "Phenom II X4 965": {"fair": 12, "good": 10, "steal": 8, "queries": ["Phenom II X4 965", "Phenom 965"]},
    "A10-7850K": {"fair": 15, "good": 12, "steal": 10, "queries": ["A10-7850K", "A10 7850K"]},
}

# ────────────────────────────────────────────────────────────
# 🔌 Cartes mères
# ────────────────────────────────────────────────────────────
MOBO = {
    # ── AM5 (Ryzen 7000/8000/9000) ──
    "X870E (AM5)": {"fair": 320, "good": 266, "steal": 218, "queries": ["carte mère X870E", "X870E"]},
    "X870 (AM5)": {"fair": 260, "good": 216, "steal": 177, "queries": ["carte mère X870", "X870"]},
    "X670E (AM5)": {"fair": 200, "good": 166, "steal": 136, "queries": ["carte mère X670E", "X670E"]},
    "X670 (AM5)": {"fair": 170, "good": 141, "steal": 116, "queries": ["carte mère X670", "X670"]},
    "B650E (AM5)": {"fair": 150, "good": 124, "steal": 102, "queries": ["carte mère B650E", "B650E"]},
    "B650 (AM5)": {"fair": 120, "good": 100, "steal": 82, "queries": ["carte mère B650", "B650", "B650M", "carte mère B650M"]},
    "A620 (AM5)": {"fair": 80, "good": 66, "steal": 54, "queries": ["carte mère A620", "A620", "A620M", "carte mère A620M"]},
    # ── AM4 (Ryzen 1000-5000) ──
    "X570 (AM4)": {"fair": 110, "good": 91, "steal": 75, "queries": ["carte mère X570", "X570"]},
    "B550 (AM4)": {"fair": 80, "good": 66, "steal": 54, "queries": ["carte mère B550", "B550", "B550M", "carte mère B550M"]},
    "B450 (AM4)": {"fair": 50, "good": 42, "steal": 34, "queries": ["carte mère B450", "B450", "B450M", "carte mère B450M"]},
    "X470 (AM4)": {"fair": 60, "good": 50, "steal": 41, "queries": ["carte mère X470", "X470"]},
    "B350 (AM4)": {"fair": 38, "good": 32, "steal": 26, "queries": ["carte mère B350", "B350", "B350M", "carte mère B350M"]},
    "A320 (AM4)": {"fair": 28, "good": 23, "steal": 19, "queries": ["carte mère A320", "A320", "A320M", "carte mère A320M"]},
    "A520 (AM4)": {"fair": 32, "good": 27, "steal": 22, "queries": ["carte mère A520", "A520", "A520M", "carte mère A520M"]},
    "X370 (AM4)": {"fair": 45, "good": 37, "steal": 31, "queries": ["carte mère X370", "X370"]},
    # ── Intel LGA1851 (Core Ultra 200S) ──
    "Z890 (1851)": {"fair": 260, "good": 216, "steal": 177, "queries": ["carte mère Z890", "Z890"]},
    "B860 (1851)": {"fair": 150, "good": 124, "steal": 102, "queries": ["carte mère B860", "B860", "B860M", "carte mère B860M"]},
    "H810 (1851)": {"fair": 90, "good": 75, "steal": 61, "queries": ["carte mère H810", "H810"]},
    # ── Intel LGA1700 (12e-14e gen) ──
    "Z790 (1700)": {"fair": 160, "good": 133, "steal": 109, "queries": ["carte mère Z790", "Z790"]},
    "B760 (1700)": {"fair": 100, "good": 83, "steal": 68, "queries": ["carte mère B760", "B760", "B760M", "carte mère B760M"]},
    "H770 (1700)": {"fair": 85, "good": 71, "steal": 58, "queries": ["carte mère H770", "H770"]},
    "B660 (1700)": {"fair": 75, "good": 62, "steal": 51, "queries": ["carte mère B660", "B660", "B660M", "carte mère B660M"]},
    "H670 (1700)": {"fair": 70, "good": 58, "steal": 48, "queries": ["carte mère H670", "H670"]},
    "H610 (1700)": {"fair": 55, "good": 46, "steal": 37, "queries": ["carte mère H610", "H610", "H610M", "carte mère H610M"]},
    "Z690 (1700)": {"fair": 120, "good": 100, "steal": 82, "queries": ["carte mère Z690", "Z690"]},
    # ── Intel LGA1200 (10e-11e gen) ──
    "Z590 (1200)": {"fair": 80, "good": 66, "steal": 54, "queries": ["carte mère Z590", "Z590"]},
    "B560 (1200)": {"fair": 55, "good": 46, "steal": 37, "queries": ["carte mère B560", "B560", "B560M", "carte mère B560M"]},
    "H570 (1200)": {"fair": 45, "good": 37, "steal": 31, "queries": ["carte mère H570", "H570"]},
    "H510 (1200)": {"fair": 30, "good": 25, "steal": 20, "queries": ["carte mère H510", "H510", "H510M", "carte mère H510M"]},
    "Z490 (1200)": {"fair": 70, "good": 58, "steal": 48, "queries": ["carte mère Z490", "Z490"]},
    "B460 (1200)": {"fair": 40, "good": 33, "steal": 27, "queries": ["carte mère B460", "B460", "B460M", "carte mère B460M"]},
    "H410 (1200)": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte mère H410", "H410", "H410M", "carte mère H410M"]},
    # ── Intel LGA1151 (6e-9e gen) ──
    "Z390 (1151)": {"fair": 55, "good": 46, "steal": 37, "queries": ["carte mère Z390", "Z390"]},
    "B365 (1151)": {"fair": 32, "good": 27, "steal": 22, "queries": ["carte mère B365", "B365"]},
    "B360 (1151)": {"fair": 35, "good": 29, "steal": 24, "queries": ["carte mère B360", "B360", "B360M", "carte mère B360M"]},
    "H370 (1151)": {"fair": 30, "good": 25, "steal": 20, "queries": ["carte mère H370", "H370"]},
    "Q370 (1151)": {"fair": 35, "good": 29, "steal": 24, "queries": ["carte mère Q370", "Q370"]},
    "H310 (1151)": {"fair": 18, "good": 15, "steal": 12, "queries": ["carte mère H310", "H310", "H310M", "carte mère H310M"]},
    "Z370 (1151)": {"fair": 45, "good": 37, "steal": 31, "queries": ["carte mère Z370", "Z370"]},
    "B250 (1151)": {"fair": 22, "good": 18, "steal": 15, "queries": ["carte mère B250", "B250"]},
    "H270 (1151)": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte mère H270", "H270"]},
    "Z270 (1151)": {"fair": 35, "good": 29, "steal": 24, "queries": ["carte mère Z270", "Z270"]},
    "H110 (1151)": {"fair": 15, "good": 12, "steal": 10, "queries": ["carte mère H110", "H110", "H110M", "carte mère H110M"]},
    # ── Intel LGA1150 (4e-5e gen Haswell) ──
    "Z97 (1150)": {"fair": 40, "good": 33, "steal": 27, "queries": ["carte mère Z97", "Z97"]},
    "H97 (1150)": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte mère H97", "H97"]},
    "B85 (1150)": {"fair": 20, "good": 17, "steal": 14, "queries": ["carte mère B85", "B85"]},
    "H81 (1150)": {"fair": 14, "good": 12, "steal": 9, "queries": ["carte mère H81", "H81"]},
    "Z87 (1150)": {"fair": 28, "good": 23, "steal": 19, "queries": ["carte mère Z87", "Z87"]},
    "H87 (1150)": {"fair": 18, "good": 15, "steal": 12, "queries": ["carte mère H87", "H87"]},
    # ── Intel LGA1155 (2e-3e gen Sandy/Ivy Bridge) ──
    "Z77 (1155)": {"fair": 30, "good": 25, "steal": 20, "queries": ["carte mère Z77", "Z77"]},
    "B75 (1155)": {"fair": 18, "good": 15, "steal": 12, "queries": ["carte mère B75", "B75"]},
    "H77 (1155)": {"fair": 20, "good": 17, "steal": 14, "queries": ["carte mère H77", "H77"]},
    "Z68 (1155)": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte mère Z68", "Z68"]},
    "H61 (1155)": {"fair": 15, "good": 12, "steal": 10, "queries": ["carte mère H61", "H61"]},
    "P67 (1155)": {"fair": 22, "good": 18, "steal": 15, "queries": ["carte mère P67", "P67"]},
    # ── AMD AM3/AM3+ (FX/Phenom) ──
    "990FX (AM3+)": {"fair": 30, "good": 25, "steal": 20, "queries": ["carte mère 990FX", "990FX"]},
    "970 (AM3+)": {"fair": 20, "good": 17, "steal": 14, "queries": ["carte mère 970 AM3", "chipset 970 AM3+"]},
    "990X (AM3+)": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte mère 990X", "990X AM3+"]},
    "760G (AM3)": {"fair": 12, "good": 10, "steal": 8, "queries": ["carte mère 760G", "760G AM3"]},
    # ── AMD FM2/FM2+ (APU A-series) ──
    "FM2+ A88X": {"fair": 20, "good": 17, "steal": 14, "queries": ["carte mère A88X", "FM2+ A88X"]},
    "FM2+ A78": {"fair": 15, "good": 12, "steal": 10, "queries": ["carte mère A78 FM2+", "FM2+ A78"]},
    "FM2 A85X": {"fair": 16, "good": 13, "steal": 11, "queries": ["carte mère A85X", "FM2 A85X"]},
}

# ────────────────────────────────────────────────────────────
# 💾 Mémoire RAM
# ────────────────────────────────────────────────────────────
RAM = {
    "DDR5 64GB (2x32)": {"fair": 320, "good": 266, "steal": 218, "queries": ["DDR5 64", "DDR5 2x32"]},
    "DDR5 32GB (2x16)": {"fair": 160, "good": 133, "steal": 109, "queries": ["DDR5 32", "DDR5 2x16"]},
    "DDR5 16GB (2x8)": {"fair": 90, "good": 75, "steal": 61, "queries": ["DDR5 16", "DDR5 2x8"]},
    "DDR4 64GB (2x32)": {"fair": 140, "good": 116, "steal": 95, "queries": ["DDR4 64", "DDR4 2x32"]},
    "DDR4 32GB (2x16)": {"fair": 75, "good": 62, "steal": 51, "queries": ["DDR4 32", "DDR4 2x16"]},
    "DDR4 16GB (2x8)": {"fair": 42, "good": 35, "steal": 29, "queries": ["DDR4 16", "DDR4 2x8"]},
    "DDR4 16GB (1x16)": {"fair": 38, "good": 32, "steal": 26, "queries": ["DDR4 16GB", "DDR4 16 go"]},
    "DDR4 8GB": {"fair": 20, "good": 17, "steal": 14, "queries": ["DDR4 8GB", "DDR4 8 go"]},
    "DDR3 16GB (2x8)": {"fair": 30, "good": 25, "steal": 20, "queries": ["DDR3 16", "DDR3 2x8"]},
    "DDR3 8GB": {"fair": 15, "good": 12, "steal": 10, "queries": ["DDR3 8GB", "DDR3 8 go"]},
    "DDR2 4GB": {"fair": 10, "good": 8, "steal": 7, "queries": ["DDR2 4GB", "DDR2 4 go"]},
    "DDR2 2GB": {"fair": 6, "good": 5, "steal": 4, "queries": ["DDR2 2GB", "DDR2 2 go"]},
}

# ────────────────────────────────────────────────────────────
# 💿 Stockage
# ────────────────────────────────────────────────────────────
STORAGE = {
    "NVMe Gen4 4To": {"fair": 230, "good": 191, "steal": 156, "queries": ["SSD NVMe 4To", "NVMe 4 To"]},
    "NVMe Gen4 2To": {"fair": 120, "good": 100, "steal": 82, "queries": ["SSD NVMe 2To Gen4", "NVMe 2To"]},
    "NVMe Gen3 2To": {"fair": 95, "good": 79, "steal": 65, "queries": ["SSD NVMe 2To", "NVMe 2 To"]},
    "NVMe 1To": {"fair": 50, "good": 42, "steal": 34, "queries": ["SSD NVMe 1To", "NVMe 1 To"]},
    "NVMe 500GB": {"fair": 28, "good": 23, "steal": 19, "queries": ["SSD NVMe 500", "NVMe 512"]},
    "NVMe 256GB": {"fair": 18, "good": 15, "steal": 12, "queries": ["SSD NVMe 256", "NVMe 250"]},
    "SSD SATA 2To": {"fair": 80, "good": 66, "steal": 54, "queries": ["SSD 2To SATA", "SSD 2 To"]},
    "SSD SATA 1To": {"fair": 40, "good": 33, "steal": 27, "queries": ["SSD 1To SATA", "SSD 1 To"]},
    "SSD SATA 500GB": {"fair": 25, "good": 21, "steal": 17, "queries": ["SSD 500 SATA", "SSD 512 SATA"]},
    "SSD SATA 256GB": {"fair": 15, "good": 12, "steal": 10, "queries": ["SSD 256 SATA", "SSD 250 SATA"]},
    "HDD 4To": {"fair": 60, "good": 50, "steal": 41, "queries": ["disque dur 4To", "HDD 4 To"]},
    "HDD 2To": {"fair": 35, "good": 29, "steal": 24, "queries": ["disque dur 2To", "HDD 2 To"]},
    "HDD 1To": {"fair": 22, "good": 18, "steal": 15, "queries": ["disque dur 1To", "HDD 1 To"]},
}

# ────────────────────────────────────────────────────────────
# ⚡ Alimentations
# ────────────────────────────────────────────────────────────
PSU = {
    # Nom de modèle "Alimentation XXXXW" (pas "PSU XXXXW"): le mot "alimentation"
    # doit rester un token requis à l'identification, sinon seule la puissance
    # ("1200w") suffit à matcher — n'importe quel appareil électroménager de
    # même puissance (fer à repasser, grill...) matchait auparavant.
    "Alimentation 1200W": {"fair": 140, "good": 116, "steal": 95, "watts": 1200, "queries": ["alimentation 1200W"]},
    "Alimentation 1000W": {"fair": 110, "good": 91, "steal": 75, "watts": 1000, "queries": ["alimentation 1000W"]},
    "Alimentation 850W": {"fair": 85, "good": 71, "steal": 58, "watts": 850, "queries": ["alimentation 850W"]},
    "Alimentation 750W": {"fair": 65, "good": 54, "steal": 44, "watts": 750, "queries": ["alimentation 750W"]},
    "Alimentation 650W": {"fair": 50, "good": 42, "steal": 34, "watts": 650, "queries": ["alimentation 650W"]},
    "Alimentation 550W": {"fair": 38, "good": 32, "steal": 26, "watts": 550, "queries": ["alimentation 550W"]},
}

# ────────────────────────────────────────────────────────────
# ❄️ Refroidissement
# ────────────────────────────────────────────────────────────
COOLING = {
    # Prix révisés à la baisse (retour terrain: le marché de l'occasion pour
    # un AIO 420mm tourne réellement autour de 70€, pas 130€ — l'ancien tarif
    # était calé sur du neuf/quasi-neuf, pas sur l'occasion réelle).
    "AIO 420mm": {"fair": 70, "good": 58, "steal": 48, "queries": ["watercooling AIO 420", "AIO 420"]},
    "AIO 360mm": {"fair": 49, "good": 41, "steal": 33, "queries": ["watercooling AIO 360", "AIO 360mm"]},
    "AIO 280mm": {"fair": 41, "good": 34, "steal": 28, "queries": ["watercooling AIO 280", "AIO 280mm"]},
    "AIO 240mm": {"fair": 33, "good": 27, "steal": 22, "queries": ["watercooling AIO 240", "AIO 240mm"]},
    "AIO 120mm": {"fair": 19, "good": 16, "steal": 13, "queries": ["watercooling AIO 120", "AIO 120mm"]},
    "Noctua NH-D15": {"fair": 70, "good": 58, "steal": 48, "queries": ["Noctua NH-D15", "NH-D15"]},
    "Noctua NH-U12": {"fair": 45, "good": 37, "steal": 31, "queries": ["Noctua NH-U12", "NH-U12"]},
    "be quiet Dark Rock": {"fair": 50, "good": 42, "steal": 34, "queries": ["Dark Rock Pro", "be quiet Dark Rock"]},
    "Hyper 212": {"fair": 22, "good": 18, "steal": 15, "queries": ["Hyper 212"]},
}

# ────────────────────────────────────────────────────────────
# 🖥️ Boîtiers — catalogue étendu, un maximum de marques/modèles réels
# ────────────────────────────────────────────────────────────
CASE = {
    # Lian Li
    "Lian Li O11 Dynamic": {"fair": 100, "good": 83, "steal": 68, "queries": ["Lian Li O11 Dynamic"]},
    "Lian Li O11 Dynamic EVO": {"fair": 120, "good": 100, "steal": 82, "queries": ["Lian Li O11 Dynamic EVO", "O11D EVO"]},
    "Lian Li O11 Air Mini": {"fair": 90, "good": 75, "steal": 61, "queries": ["Lian Li O11 Air Mini"]},
    "Lian Li Lancool 216": {"fair": 75, "good": 62, "steal": 51, "queries": ["Lian Li Lancool 216", "Lancool 216"]},
    "Lian Li Lancool 205": {"fair": 55, "good": 46, "steal": 37, "queries": ["Lian Li Lancool 205", "Lancool 205"]},
    "Lian Li Lancool 215": {"fair": 65, "good": 54, "steal": 44, "queries": ["Lian Li Lancool 215", "Lancool 215"]},
    "Lian Li A4-H2O (SFF)": {"fair": 110, "good": 91, "steal": 75, "queries": ["Lian Li A4-H2O", "Lian Li A4"]},
    # Hyte
    "Hyte Y40": {"fair": 130, "good": 108, "steal": 88, "queries": ["Hyte Y40"]},
    "Hyte Y60": {"fair": 150, "good": 125, "steal": 102, "queries": ["Hyte Y60"]},
    "Hyte Y70": {"fair": 140, "good": 116, "steal": 95, "queries": ["Hyte Y70"]},
    # NZXT
    "NZXT H510": {"fair": 45, "good": 37, "steal": 31, "queries": ["NZXT H510"]},
    "NZXT H510 Flow": {"fair": 50, "good": 42, "steal": 34, "queries": ["NZXT H510 Flow"]},
    "NZXT H710": {"fair": 75, "good": 62, "steal": 51, "queries": ["NZXT H710"]},
    "NZXT H7 Flow": {"fair": 85, "good": 71, "steal": 58, "queries": ["NZXT H7 Flow", "NZXT H7"]},
    "NZXT H9 Flow": {"fair": 110, "good": 91, "steal": 75, "queries": ["NZXT H9 Flow"]},
    "NZXT H9 Elite": {"fair": 140, "good": 116, "steal": 95, "queries": ["NZXT H9 Elite"]},
    "NZXT H1": {"fair": 90, "good": 75, "steal": 61, "queries": ["NZXT H1"]},
    "NZXT H6 Flow": {"fair": 80, "good": 66, "steal": 54, "queries": ["NZXT H6 Flow"]},
    # Corsair
    "Corsair 4000D Airflow": {"fair": 70, "good": 58, "steal": 48, "queries": ["Corsair 4000D Airflow", "Corsair 4000D"]},
    "Corsair 5000D Airflow": {"fair": 100, "good": 83, "steal": 68, "queries": ["Corsair 5000D Airflow", "Corsair 5000D"]},
    "Corsair 3000D": {"fair": 55, "good": 46, "steal": 37, "queries": ["Corsair 3000D"]},
    "Corsair iCUE 220T": {"fair": 80, "good": 66, "steal": 54, "queries": ["Corsair iCUE 220T", "Corsair 220T"]},
    "Corsair Carbide 275R": {"fair": 45, "good": 37, "steal": 31, "queries": ["Corsair Carbide 275R", "Corsair 275R"]},
    "Corsair Obsidian 500D": {"fair": 90, "good": 75, "steal": 61, "queries": ["Corsair Obsidian 500D"]},
    "Corsair Obsidian 1000D": {"fair": 250, "good": 208, "steal": 170, "queries": ["Corsair Obsidian 1000D"]},
    # Fractal Design
    "Fractal Design Meshify 2": {"fair": 90, "good": 75, "steal": 61, "queries": ["Fractal Meshify 2"]},
    "Fractal Design Meshify C": {"fair": 55, "good": 46, "steal": 37, "queries": ["Fractal Meshify C"]},
    "Fractal Design North": {"fair": 90, "good": 75, "steal": 61, "queries": ["Fractal Design North", "Fractal North"]},
    "Fractal Design Define 7": {"fair": 110, "good": 91, "steal": 75, "queries": ["Fractal Define 7"]},
    "Fractal Design Pop Air": {"fair": 60, "good": 50, "steal": 41, "queries": ["Fractal Pop Air"]},
    "Fractal Design Torrent": {"fair": 120, "good": 100, "steal": 82, "queries": ["Fractal Design Torrent", "Fractal Torrent"]},
    # be quiet!
    "be quiet Pure Base 500DX": {"fair": 70, "good": 58, "steal": 48, "queries": ["be quiet Pure Base 500DX"]},
    "be quiet Dark Base 900": {"fair": 140, "good": 116, "steal": 95, "queries": ["be quiet Dark Base 900"]},
    "be quiet Dark Base 700": {"fair": 110, "good": 91, "steal": 75, "queries": ["be quiet Dark Base 700"]},
    "be quiet Silent Base 802": {"fair": 100, "good": 83, "steal": 68, "queries": ["be quiet Silent Base 802"]},
    # Cooler Master
    "Cooler Master MasterBox TD500": {"fair": 55, "good": 46, "steal": 37, "queries": ["Cooler Master TD500"]},
    "Cooler Master NR200": {"fair": 65, "good": 54, "steal": 44, "queries": ["Cooler Master NR200"]},
    "Cooler Master H500": {"fair": 65, "good": 54, "steal": 44, "queries": ["Cooler Master H500"]},
    "Cooler Master Cosmos C700M": {"fair": 200, "good": 166, "steal": 136, "queries": ["Cooler Master Cosmos C700M"]},
    "Cooler Master Q300L": {"fair": 35, "good": 29, "steal": 24, "queries": ["Cooler Master Q300L"]},
    # Phanteks
    "Phanteks Eclipse P400A": {"fair": 55, "good": 46, "steal": 37, "queries": ["Phanteks P400A", "Phanteks Eclipse P400A"]},
    "Phanteks Eclipse P500A": {"fair": 75, "good": 62, "steal": 51, "queries": ["Phanteks P500A", "Phanteks Eclipse P500A"]},
    "Phanteks Eclipse P600S": {"fair": 90, "good": 75, "steal": 61, "queries": ["Phanteks P600S"]},
    "Phanteks Enthoo Pro": {"fair": 70, "good": 58, "steal": 48, "queries": ["Phanteks Enthoo Pro"]},
    # Thermaltake
    "Thermaltake Core P3": {"fair": 70, "good": 58, "steal": 48, "queries": ["Thermaltake Core P3"]},
    "Thermaltake View 71": {"fair": 90, "good": 75, "steal": 61, "queries": ["Thermaltake View 71"]},
    "Thermaltake S100": {"fair": 40, "good": 33, "steal": 27, "queries": ["Thermaltake S100"]},
    "Thermaltake Level 20": {"fair": 130, "good": 108, "steal": 88, "queries": ["Thermaltake Level 20"]},
    # Antec
    "Antec P120 Crystal": {"fair": 55, "good": 46, "steal": 37, "queries": ["Antec P120 Crystal"]},
    "Antec DF700 Flux": {"fair": 70, "good": 58, "steal": 48, "queries": ["Antec DF700 Flux"]},
    "Antec NX410": {"fair": 40, "good": 33, "steal": 27, "queries": ["Antec NX410"]},
    # SilverStone
    "SilverStone Fara R1": {"fair": 40, "good": 33, "steal": 27, "queries": ["SilverStone Fara R1"]},
    "SilverStone SG13 (SFF)": {"fair": 35, "good": 29, "steal": 24, "queries": ["SilverStone SG13"]},
    "SilverStone Alta D1": {"fair": 100, "good": 83, "steal": 68, "queries": ["SilverStone Alta D1"]},
    # InWin
    "InWin 303": {"fair": 60, "good": 50, "steal": 41, "queries": ["InWin 303"]},
    "InWin 301": {"fair": 55, "good": 46, "steal": 37, "queries": ["InWin 301"]},
    "InWin A1": {"fair": 90, "good": 75, "steal": 61, "queries": ["InWin A1"]},
    # Asus
    "Asus TUF Gaming GT501": {"fair": 65, "good": 54, "steal": 44, "queries": ["Asus TUF GT501", "TUF Gaming GT501"]},
    "Asus ROG Strix Helios": {"fair": 120, "good": 100, "steal": 82, "queries": ["Asus ROG Strix Helios"]},
    # MSI
    "MSI MAG Forge 100M": {"fair": 40, "good": 33, "steal": 27, "queries": ["MSI MAG Forge 100M"]},
    "MSI MAG Pano 100": {"fair": 75, "good": 62, "steal": 51, "queries": ["MSI MAG Pano 100"]},
    # DeepCool
    "DeepCool CH510": {"fair": 40, "good": 33, "steal": 27, "queries": ["DeepCool CH510"]},
    "DeepCool CK560": {"fair": 55, "good": 46, "steal": 37, "queries": ["DeepCool CK560"]},
    "DeepCool Matrexx 55": {"fair": 45, "good": 37, "steal": 31, "queries": ["DeepCool Matrexx 55"]},
    # Zalman / autres marques
    "Zalman Z9": {"fair": 35, "good": 29, "steal": 24, "queries": ["Zalman Z9"]},
    # Générique / sans marque précise
    "Boîtier ATX RGB façade verre (générique)": {"fair": 30, "good": 25, "steal": 20, "queries": ["boitier ATX RGB verre trempé", "boitier gaming verre trempé"]},
    "Boîtier ATX classique (générique)": {"fair": 20, "good": 17, "steal": 14, "queries": ["boitier ATX gaming", "boitier PC tour ATX"]},
    "Boîtier ITX Mini (générique)": {"fair": 45, "good": 37, "steal": 31, "queries": ["boitier ITX mini", "boitier mini ITX"]},
    "Boîtier Micro-ATX (générique)": {"fair": 25, "good": 21, "steal": 17, "queries": ["boitier micro ATX gaming"]},
}

# ────────────────────────────────────────────────────────────
# 📡 Cartes réseau / WiFi
# ────────────────────────────────────────────────────────────
NETWORK = {
    "Carte WiFi 6E PCIe": {"fair": 35, "good": 29, "steal": 24, "queries": ["carte wifi 6E PCIe", "carte wifi 6E interne"]},
    "Carte WiFi6 PCIe": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte wifi 6 PCIe", "carte wifi ax PCIe"]},
    "Carte WiFi PCIe ancienne (AC/N)": {"fair": 12, "good": 10, "steal": 8, "queries": ["carte wifi PCIe interne", "carte wifi ac PCIe"]},
    "Clé USB WiFi": {"fair": 10, "good": 8, "steal": 7, "queries": ["clé usb wifi", "adaptateur usb wifi"]},
    "Carte réseau 2.5G/10G Ethernet": {"fair": 20, "good": 17, "steal": 14, "queries": ["carte réseau 2.5G", "carte réseau 10G ethernet"]},
    "Carte réseau Gigabit PCIe": {"fair": 10, "good": 8, "steal": 7, "queries": ["carte réseau gigabit PCIe", "carte ethernet PCIe"]},
}

# ────────────────────────────────────────────────────────────
# 🔊 Cartes son
# ────────────────────────────────────────────────────────────
SOUND = {
    "Sound Blaster AE-9/AE-7": {"fair": 90, "good": 75, "steal": 61, "queries": ["Sound Blaster AE-9", "Sound Blaster AE-7"]},
    "Sound Blaster AE-5": {"fair": 55, "good": 46, "steal": 37, "queries": ["Sound Blaster AE-5"]},
    "Sound Blaster Z/ZxR": {"fair": 45, "good": 37, "steal": 31, "queries": ["Sound Blaster Z", "Sound Blaster ZxR"]},
    "Sound Blaster ancienne (Audigy/X-Fi)": {"fair": 20, "good": 17, "steal": 14, "queries": ["Sound Blaster Audigy", "Sound Blaster X-Fi"]},
    "Carte son externe USB": {"fair": 25, "good": 21, "steal": 17, "queries": ["carte son externe USB", "DAC audio USB"]},
}

# ────────────────────────────────────────────────────────────
# 💽 Lecteurs optiques
# ────────────────────────────────────────────────────────────
OPTICAL = {
    "Graveur Blu-ray interne": {"fair": 30, "good": 25, "steal": 20, "queries": ["graveur blu-ray interne", "lecteur blu-ray PC"]},
    "Graveur Blu-ray externe USB": {"fair": 35, "good": 29, "steal": 24, "queries": ["graveur blu-ray externe USB"]},
    "Graveur DVD interne": {"fair": 10, "good": 8, "steal": 7, "queries": ["graveur DVD interne", "lecteur DVD PC"]},
    "Graveur DVD externe USB": {"fair": 15, "good": 12, "steal": 10, "queries": ["graveur DVD externe USB"]},
}

# ────────────────────────────────────────────────────────────
# 🔋 Onduleurs (UPS)
# ────────────────────────────────────────────────────────────
UPS = {
    "Onduleur APC 1000-1500VA": {"fair": 80, "good": 66, "steal": 54, "queries": ["onduleur APC 1000VA", "onduleur APC 1500VA", "APC Back-UPS"]},
    "Onduleur APC 600-850VA": {"fair": 45, "good": 37, "steal": 31, "queries": ["onduleur APC 650VA", "onduleur APC 850VA"]},
    "Onduleur Eaton": {"fair": 70, "good": 58, "steal": 48, "queries": ["onduleur Eaton", "Eaton UPS"]},
    "Onduleur générique": {"fair": 35, "good": 29, "steal": 24, "queries": ["onduleur PC", "UPS onduleur"]},
}

# Regroupement par catégorie (ordre = ordre d'affichage)
CATEGORIES = {
    "GPU": {"label": "Cartes graphiques", "color": "#ff6b35", "db": GPU},
    "CPU": {"label": "Processeurs", "color": "#a8dadc", "db": CPU},
    "MOBO": {"label": "Cartes mères", "color": "#c77dff", "db": MOBO},
    "RAM": {"label": "Mémoire RAM", "color": "#ffd166", "db": RAM},
    "STORAGE": {"label": "Stockage", "color": "#06d6a0", "db": STORAGE},
    "PSU": {"label": "Alimentations", "color": "#118ab2", "db": PSU},
    "COOLING": {"label": "Refroidissement", "color": "#00d4ff", "db": COOLING},
    "CASE": {"label": "Boîtiers", "color": "#ef476f", "db": CASE},
    "NETWORK": {"label": "Réseau/WiFi", "color": "#f4a261", "db": NETWORK},
    "SOUND": {"label": "Cartes son", "color": "#e76f51", "db": SOUND},
    "OPTICAL": {"label": "Lecteurs optiques", "color": "#2a9d8f", "db": OPTICAL},
    "UPS": {"label": "Onduleurs", "color": "#8ac926", "db": UPS},
}

# ─────────────────────────────────────────────────────────────
#  PRIX PLANCHER par catégorie — anti-bruit
# ─────────────────────────────────────────────────────────────
# En dessous de ce prix, une annonce est presque toujours une boîte vide,
# une pièce détachée isolée, ou une arnaque. On l'exclut purement et
# simplement avant même d'appliquer les seuils fair/good/steal.
# GPU à 25€ minimum comme demandé (en dessous = quasi toujours du carton/HS).
MIN_PRICE = {
    "GPU": 25,
    "CPU": 10,
    "MOBO": 10,
    "RAM": 5,
    "STORAGE": 5,
    "PSU": 10,
    "COOLING": 5,
    "CASE": 5,
    "NETWORK": 5,
    "SOUND": 10,
    "OPTICAL": 5,
    "UPS": 15,
}

