"""
ram_reference_data.py — Base de références DDR4 UDIMM desktop (socle du scoring)
═══════════════════════════════════════════════════════════════════════════════
89 références réelles, part numbers constructeur exacts, couvrant les 5 tiers.
Chargée en base par `ram_db.seed_references()`.

⚠️ LES PRIX SONT UN POINT DE DÉPART, PAS UNE VÉRITÉ.
Ils sont calibrés sur le marché occasion FR mi-2026 (contexte de pénurie DRAM :
la DDR4 est en fin de vie côté production, les prix montent et ne redescendent
pas). `ram_calibration.py` les remplace par des médianes de ventes réelles dès
le premier passage. Une référence non recalibrée depuis 14 jours est signalée
au dashboard (vue `v_ram_reference_perimee`).

── Comment lire une ligne ──
    R("CMK32GX4M2E3200C16", "Corsair", "Vengeance LPX", 16, 2, 3200, 16,
      tier="A", prix=115, liq=5, rot=6, ...)
       │        │          │            │  │   │     │
       PN exact marque     gamme        │  │   freq  CL
                            capacité par barrette
                                        nb de barrettes du kit

── Champs optionnels ──
    die      : 'Samsung B-die' / 'Micron E-die' / … (argument de vente fort)
    rank     : '1Rx8' / '2Rx8'
    rgb      : True/False       couleur : 'noir' / 'blanc' / …
    haut     : hauteur en mm    (low_profile calculé automatiquement < 34 mm)
    volt     : tension XMP
    alias    : formulations réellement tapées par les vendeurs (matching texte)
    pn_ok    : False = PN reconstruit par déduction, à confirmer au calibrage
    notes    : ce qu'il faut savoir avant d'acheter

── Tiers ──
    S : premium overclockeur (B-die / E-die), forte marge, rotation moyenne
    A : le pain quotidien — 2×16 en 3200/3600, meilleur rapport marge/temps
    B : volume — 2×8, 2×16 lents, 32 Go unitaires
    C : lot ou bas prix seulement — 4×8, OEM nue, 2666/2400
    D : no-name — refus sauf lot quasi gratuit, warning en notification
"""

# Hauteur au-delà de laquelle une barrette ne passe plus sous un gros ventirad.
# Seuil INCLUSIF : le Vengeance LPX fait exactement 34 mm et c'est la barrette
# low profile de référence du marché — l'exclure par un « < 34 » strict priverait
# du bonus la gamme la plus vendue.
LOW_PROFILE_MM = 34.0


def R(part_number, marque, gamme, capacite_module, nb_modules, frequence, cl,
      tier, prix, liq, rot, die=None, rank=None, rgb=False, couleur="noir",
      haut=None, volt=1.35, alias=None, pn_ok=True, notes=None):
    """Construit une référence. `capacite_totale` et `low_profile` sont dérivés
    pour qu'ils ne puissent pas diverger d'une saisie à l'autre."""
    return {
        "part_number": part_number,
        "marque": marque,
        "gamme": gamme,
        "alias": alias or [],
        "capacite_module_go": capacite_module,
        "nb_modules": nb_modules,
        "capacite_totale_go": capacite_module * nb_modules,
        "frequence_mhz": frequence,
        "cas_latency": cl,
        "voltage": volt,
        "rank": rank,
        "die_type": die,
        "rgb": bool(rgb),
        "couleur": couleur,
        "hauteur_mm": haut,
        "low_profile": bool(haut is not None and haut <= LOW_PROFILE_MM),
        "tier": tier,
        "prix_ref_occasion_eur": float(prix),
        "liquidite": liq,
        "delai_rotation_jours": rot,
        "pn_verifie": bool(pn_ok),
        "notes": notes,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  TIER S — Samsung B-die & Micron E-die
#  Le CL14 en 3200 est la signature B-die. Toujours confirmer au part number :
#  le nom marketing ("Vengeance", "Ripjaws") ne dit rien sur les puces.
# ═══════════════════════════════════════════════════════════════════════════
TIER_S = [
    R("F4-3200C14D-16GTZ", "G.Skill", "Trident Z", 8, 2, 3200, 14,
      tier="S", prix=95, liq=3, rot=14, die="Samsung B-die", rank="1Rx8",
      couleur="noir/argent", haut=44, volt=1.35,
      alias=["trident z 3200 cl14", "tridentz 3200c14", "gskill 3200 cl14"],
      notes="B-die de référence. Public overclockeur : vend plus cher mais plus lentement."),

    R("F4-3200C14D-32GTZ", "G.Skill", "Trident Z", 16, 2, 3200, 14,
      tier="S", prix=165, liq=3, rot=18, die="Samsung B-die", rank="2Rx8",
      couleur="noir/argent", haut=44,
      alias=["trident z 32go cl14", "trident z 2x16 3200 cl14"],
      notes="2×16 B-die dual rank : la combinaison la plus recherchée sur AM4."),

    R("F4-3200C14D-16GTZR", "G.Skill", "Trident Z RGB", 8, 2, 3200, 14,
      tier="S", prix=110, liq=3, rot=14, die="Samsung B-die", rank="1Rx8",
      rgb=True, couleur="noir", haut=44,
      alias=["trident z rgb 3200 cl14", "tridentz rgb 16go"]),

    R("F4-3200C14D-32GTZR", "G.Skill", "Trident Z RGB", 16, 2, 3200, 14,
      tier="S", prix=190, liq=2, rot=21, die="Samsung B-die", rank="2Rx8",
      rgb=True, couleur="noir", haut=44,
      notes="Rare en occasion FR. Le tarif tient si les LED sont fonctionnelles."),

    R("F4-3600C14D-32GTZN", "G.Skill", "Trident Z Neo", 16, 2, 3600, 14,
      tier="S", prix=190, liq=3, rot=18, die="Samsung B-die", rank="2Rx8",
      rgb=True, couleur="noir/argent", haut=44, volt=1.45,
      alias=["trident z neo 3600 cl14", "tz neo 32go cl14"],
      notes="Le graal Ryzen : 3600 CL14 en FCLK 1:1. Argument de vente à mettre en titre."),

    R("F4-3600C16D-32GTZN", "G.Skill", "Trident Z Neo", 16, 2, 3600, 16,
      tier="S", prix=150, liq=4, rot=10, die="Samsung B-die", rank="2Rx8",
      rgb=True, couleur="noir/argent", haut=44,
      alias=["trident z neo 3600", "tz neo 32 go"],
      notes="Optimum Ryzen 5000 (FCLK 1800, 1:1). Rotation rapide malgré le prix."),

    R("F4-4000C18D-32GTZN", "G.Skill", "Trident Z Neo", 16, 2, 4000, 18,
      tier="S", prix=165, liq=2, rot=25, die="Samsung B-die", rank="2Rx8",
      rgb=True, couleur="noir/argent", haut=44, volt=1.40,
      notes="4000 MHz : intéressant surtout Intel. Sur Ryzen le 1:1 décroche, à ne pas survendre."),

    R("F4-3600C14D-16GTRG", "G.Skill", "Trident Z Royal", 8, 2, 3600, 14,
      tier="S", prix=135, liq=2, rot=25, die="Samsung B-die", rank="1Rx8",
      rgb=True, couleur="or", haut=44, volt=1.45,
      alias=["trident z royal gold", "tz royal 3600"],
      notes="Dissipateur doré : très fragile aux rayures. Photographier de près avant achat."),

    R("F4-3600C14D-16GTRS", "G.Skill", "Trident Z Royal", 8, 2, 3600, 14,
      tier="S", prix=130, liq=2, rot=25, die="Samsung B-die", rank="1Rx8",
      rgb=True, couleur="argent", haut=44, volt=1.45,
      alias=["trident z royal silver"]),

    R("F4-3200C14D-16GVK", "G.Skill", "Ripjaws V", 8, 2, 3200, 14,
      tier="S", prix=88, liq=3, rot=14, die="Samsung B-die", rank="1Rx8",
      couleur="noir", haut=42,
      alias=["ripjaws v 3200 cl14", "ripjaws 3200c14"],
      notes="B-die sans le prix du dissipateur Trident. Excellente marge à l'achat."),

    R("F4-3200C14D-32GVK", "G.Skill", "Ripjaws V", 16, 2, 3200, 14,
      tier="S", prix=160, liq=3, rot=18, die="Samsung B-die", rank="2Rx8",
      couleur="noir", haut=42),

    R("CMK16GX4M2B3200C14", "Corsair", "Vengeance LPX", 8, 2, 3200, 14,
      tier="S", prix=90, liq=3, rot=14, die="Samsung B-die", rank="1Rx8",
      couleur="noir", haut=34,
      alias=["vengeance lpx 3200 cl14", "corsair lpx cl14"],
      notes="Le seul LPX en B-die. Souvent vendu au prix d'un CL16 par méconnaissance."),

    R("BL2K16G36C16U4B", "Crucial", "Ballistix", 16, 2, 3600, 16,
      tier="S", prix=150, liq=4, rot=12, die="Micron E-die", rank="2Rx8",
      couleur="noir", haut=39, volt=1.35,
      alias=["ballistix 3600 cl16", "crucial ballistix 32go 3600"],
      notes="Gamme arrêtée en 2022 : valeur collector, la cote monte lentement mais sûrement."),

    R("BL2K8G36C16U4B", "Crucial", "Ballistix", 8, 2, 3600, 16,
      tier="S", prix=82, liq=4, rot=12, die="Micron E-die", rank="1Rx8",
      couleur="noir", haut=39),

    R("BL2K16G36C16U4BL", "Crucial", "Ballistix RGB", 16, 2, 3600, 16,
      tier="S", prix=158, liq=3, rot=12, die="Micron E-die", rank="2Rx8",
      rgb=True, couleur="noir", haut=39,
      alias=["ballistix rgb 3600", "ballistix rgb 32go"]),

    R("BLM2K16G40C18U4B", "Crucial", "Ballistix MAX", 16, 2, 4000, 18,
      tier="S", prix=175, liq=2, rot=25, die="Micron E-die", rank="2Rx8",
      couleur="noir", haut=39, volt=1.35,
      notes="Ballistix MAX : haut de gamme arrêté, peu d'acheteurs mais prix ferme."),

    R("BLM2K8G44C19U4B", "Crucial", "Ballistix MAX", 8, 2, 4400, 19,
      tier="S", prix=105, liq=2, rot=28, die="Micron E-die", rank="1Rx8",
      couleur="noir", haut=39, volt=1.40),

    R("PVS416G440C9K", "Patriot", "Viper Steel", 8, 2, 4400, 19,
      tier="S", prix=100, liq=2, rot=25, die="Samsung B-die", rank="1Rx8",
      couleur="gris", haut=45, volt=1.45,
      alias=["viper steel 4400", "patriot 4400 cl19"],
      notes="B-die binné haut. Codage Patriot : C9 = CL19, C8 = CL18, C6 = CL16."),

    R("TF10D416G3600HC14CDC01", "TeamGroup", "T-Force Xtreem ARGB", 8, 2, 3600, 14,
      tier="S", prix=120, liq=2, rot=28, die="Samsung B-die", rank="1Rx8",
      rgb=True, couleur="blanc", haut=48, volt=1.45,
      alias=["xtreem argb 3600", "t-force xtreem blanc"],
      notes="Blanc + ARGB + B-die : cumule 3 multiplicateurs. Marché FR étroit mais prix haut."),

    R("CMT32GX4M2Z3600C18", "Corsair", "Dominator Platinum RGB", 16, 2, 3600, 18,
      tier="S", prix=185, liq=3, rot=15, rank="2Rx8", rgb=True, couleur="noir",
      haut=55, alias=["dominator platinum rgb 3600", "dominator 32go rgb"],
      notes="Attention : les Dominator Platinum 1866 sont de la DDR3. Vérifier la fréquence."),

    R("CMT16GX4M2C3200C16", "Corsair", "Dominator Platinum RGB", 8, 2, 3200, 16,
      tier="S", prix=125, liq=3, rot=15, rank="1Rx8", rgb=True, couleur="noir", haut=55),

    R("CMD16GX4M2B3200C16", "Corsair", "Dominator Platinum", 8, 2, 3200, 16,
      tier="S", prix=105, liq=2, rot=20, rank="1Rx8", couleur="noir", haut=55,
      notes="Sans RGB : moins demandé que le CMT malgré une qualité identique."),
]


# ═══════════════════════════════════════════════════════════════════════════
#  TIER A — LE PAIN QUOTIDIEN
#  2×16 en 3200 CL16 et 3600 CL16-18. Meilleur rapport marge / temps passé.
#  Le 3600 CL16-18 est l'optimum Ryzen 5000 (FCLK 1800 en 1:1) : à écrire
#  systématiquement dans l'annonce de revente, ça déclenche l'achat.
# ═══════════════════════════════════════════════════════════════════════════
TIER_A = [
    R("CMK32GX4M2E3200C16", "Corsair", "Vengeance LPX", 16, 2, 3200, 16,
      tier="A", prix=115, liq=5, rot=6, rank="2Rx8", couleur="noir", haut=34,
      alias=["vengeance lpx 32go 3200", "corsair 2x16 3200", "lpx 32 go"],
      notes="La référence la plus liquide du marché FR. Le 'E' est une révision Corsair, PAS de l'ECC."),

    R("CMK32GX4M2Z3200C16", "Corsair", "Vengeance LPX", 16, 2, 3200, 16,
      tier="A", prix=115, liq=5, rot=6, rank="2Rx8", couleur="noir", haut=34,
      alias=["lpx 32go amd ryzen"],
      notes="Révision 'Z' : profil AMD Ryzen validé. Argument de vente sur AM4."),

    R("CMK32GX4M2B3200C16", "Corsair", "Vengeance LPX", 16, 2, 3200, 16,
      tier="A", prix=112, liq=5, rot=7, rank="2Rx8", couleur="noir", haut=34),

    R("CMK32GX4M2D3600C18", "Corsair", "Vengeance LPX", 16, 2, 3600, 18,
      tier="A", prix=122, liq=5, rot=6, rank="2Rx8", couleur="noir", haut=34,
      alias=["lpx 3600 32go", "corsair 3600 cl18 32"],
      notes="FCLK 1:1 sur Ryzen 5000. Se revend en 3-4 jours à prix correct."),

    R("CMK32GX4M2Z3600C18", "Corsair", "Vengeance LPX", 16, 2, 3600, 18,
      tier="A", prix=122, liq=5, rot=6, rank="2Rx8", couleur="noir", haut=34),

    R("CMK32GX4M2D3000C16", "Corsair", "Vengeance LPX", 16, 2, 3000, 16,
      tier="A", prix=98, liq=4, rot=9, rank="2Rx8", couleur="noir", haut=34),

    R("CMK64GX4M2E3200C16", "Corsair", "Vengeance LPX", 32, 2, 3200, 16,
      tier="A", prix=235, liq=3, rot=14, rank="2Rx8", couleur="noir", haut=34,
      alias=["lpx 64go", "corsair 2x32 3200"],
      notes="Valeur unitaire élevée, concurrence faible. Immobilise du capital : viser < 150€ à l'achat."),

    R("CMW32GX4M2E3200C16", "Corsair", "Vengeance RGB Pro", 16, 2, 3200, 16,
      tier="A", prix=135, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=51,
      alias=["vengeance rgb pro 32go", "corsair rgb pro 3200"],
      notes="Haut de barrette 51 mm : incompatible avec beaucoup de ventirads. À préciser en annonce."),

    R("CMW32GX4M2C3200C16", "Corsair", "Vengeance RGB Pro", 16, 2, 3200, 16,
      tier="A", prix=135, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=51),

    R("CMW32GX4M2Z3600C18", "Corsair", "Vengeance RGB Pro", 16, 2, 3600, 18,
      tier="A", prix=145, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=51,
      alias=["rgb pro 3600 32go"]),

    R("CMW32GX4M2D3600C18", "Corsair", "Vengeance RGB Pro", 16, 2, 3600, 18,
      tier="A", prix=145, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=51),

    R("CMH32GX4M2Z3600C18", "Corsair", "Vengeance RGB Pro SL", 16, 2, 3600, 18,
      tier="A", prix=145, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=44,
      alias=["rgb pro sl 3600", "vengeance sl 32go"],
      notes="Version SL : 44 mm au lieu de 51. Compatible ventirad, argument de vente réel."),

    R("CMH32GX4M2E3200C16", "Corsair", "Vengeance RGB Pro SL", 16, 2, 3200, 16,
      tier="A", prix=138, liq=4, rot=8, rank="2Rx8", rgb=True, couleur="noir", haut=44),

    R("CMH32GX4M2Z3600C18W", "Corsair", "Vengeance RGB Pro SL", 16, 2, 3600, 18,
      tier="A", prix=165, liq=3, rot=10, rank="2Rx8", rgb=True, couleur="blanc", haut=44,
      alias=["vengeance rgb pro sl blanc", "corsair blanc 32go"],
      notes="Blanc : +15% et rotation plus rapide, les builds blancs cherchent des kits assortis."),

    R("F4-3200C16D-32GVK", "G.Skill", "Ripjaws V", 16, 2, 3200, 16,
      tier="A", prix=112, liq=5, rot=7, rank="2Rx8", couleur="noir", haut=42,
      alias=["ripjaws v 32go 3200", "gskill 2x16 3200"]),

    R("F4-3600C16D-32GVKC", "G.Skill", "Ripjaws V", 16, 2, 3600, 16,
      tier="A", prix=128, liq=5, rot=7, rank="2Rx8", couleur="noir", haut=42,
      alias=["ripjaws v 3600 cl16 32go"],
      notes="3600 CL16 : meilleur compromis Ryzen du tier A."),

    R("F4-3600C18D-32GVKC", "G.Skill", "Ripjaws V", 16, 2, 3600, 18,
      tier="A", prix=118, liq=4, rot=8, rank="2Rx8", couleur="noir", haut=42),

    R("F4-3600C18D-32GTZN", "G.Skill", "Trident Z Neo", 16, 2, 3600, 18,
      tier="A", prix=138, liq=4, rot=9, rank="2Rx8", rgb=True,
      couleur="noir/argent", haut=44),

    R("F4-3200C16D-32GTZN", "G.Skill", "Trident Z Neo", 16, 2, 3200, 16,
      tier="A", prix=132, liq=4, rot=9, rank="2Rx8", rgb=True,
      couleur="noir/argent", haut=44),

    R("F4-3200C16D-64GVK", "G.Skill", "Ripjaws V", 32, 2, 3200, 16,
      tier="A", prix=230, liq=3, rot=14, rank="2Rx8", couleur="noir", haut=42,
      alias=["ripjaws 64go", "gskill 2x32"]),

    R("KF432C16BB1K2/32", "Kingston", "FURY Beast", 16, 2, 3200, 16,
      tier="A", prix=108, liq=5, rot=7, rank="2Rx8", couleur="noir", haut=34,
      alias=["fury beast 32go 3200", "kingston fury 2x16"],
      notes="Low profile 34 mm. Très demandé en boîtier compact."),

    R("KF436C18BBAK2/32", "Kingston", "FURY Beast RGB", 16, 2, 3600, 18,
      tier="A", prix=132, liq=4, rot=9, rank="2Rx8", rgb=True, couleur="noir", haut=43,
      alias=["fury beast rgb 32go"]),

    R("KF432C16BBAK2/32", "Kingston", "FURY Beast RGB", 16, 2, 3200, 16,
      tier="A", prix=125, liq=4, rot=9, rank="2Rx8", rgb=True, couleur="noir", haut=43),

    R("KF436C16RB1K2/32", "Kingston", "FURY Renegade", 16, 2, 3600, 16,
      tier="A", prix=135, liq=4, rot=9, rank="2Rx8", couleur="noir", haut=40,
      alias=["fury renegade 3600 32go"]),

    R("KF436C16RBAK2/32", "Kingston", "FURY Renegade RGB", 16, 2, 3600, 16,
      tier="A", prix=150, liq=3, rot=12, rank="2Rx8", rgb=True, couleur="noir", haut=44),

    R("HX432C16FB3K2/32", "HyperX", "Fury", 16, 2, 3200, 16,
      tier="A", prix=105, liq=5, rot=7, rank="2Rx8", couleur="noir", haut=34,
      alias=["hyperx fury 32go 3200", "hyperx 2x16"],
      notes="⚠️ HyperX Fury existe aussi en DDR3 1600. Toujours vérifier la fréquence annoncée."),

    R("BL2K16G32C16U4B", "Crucial", "Ballistix", 16, 2, 3200, 16,
      tier="A", prix=112, liq=5, rot=7, die="Micron E-die", rank="2Rx8",
      couleur="noir", haut=39, alias=["ballistix 32go 3200", "crucial ballistix 2x16"]),

    R("BL2K16G32C16U4W", "Crucial", "Ballistix", 16, 2, 3200, 16,
      tier="A", prix=128, liq=4, rot=9, die="Micron E-die", rank="2Rx8",
      couleur="blanc", haut=39, alias=["ballistix blanc 32go"],
      notes="Version blanche : +15%, recherchée pour les builds blancs."),

    R("BL2K16G32C16U4R", "Crucial", "Ballistix", 16, 2, 3200, 16,
      tier="A", prix=115, liq=4, rot=9, die="Micron E-die", rank="2Rx8",
      couleur="rouge", haut=39),

    R("PVS432G360C8K", "Patriot", "Viper Steel", 16, 2, 3600, 18,
      tier="A", prix=120, liq=4, rot=9, rank="2Rx8", couleur="gris", haut=44,
      alias=["viper steel 32go 3600"]),

    R("PVS432G320C6K", "Patriot", "Viper Steel", 16, 2, 3200, 16,
      tier="A", prix=110, liq=4, rot=9, rank="2Rx8", couleur="gris", haut=44),

    R("PVB432G360C8K", "Patriot", "Viper 4 Blackout", 16, 2, 3600, 18,
      tier="A", prix=118, liq=4, rot=10, rank="2Rx8", couleur="noir", haut=41,
      alias=["viper blackout 32go"]),

    R("TLZGD432G3200HC16CDC01", "TeamGroup", "T-Force Vulcan Z", 16, 2, 3200, 16,
      tier="A", prix=95, liq=4, rot=10, rank="2Rx8", couleur="gris", haut=32,
      alias=["vulcan z 32go", "team group vulcan"],
      notes="Low profile 32 mm. Marque moins cotée en FR : acheter bas, vendre vite."),
]


# ═══════════════════════════════════════════════════════════════════════════
#  TIER B — VOLUME
#  2×8 en 3200/3600, 2×16 lents, 32 Go unitaires. Marge unitaire plus faible
#  mais flux constant : c'est ce qui fait tourner le capital.
# ═══════════════════════════════════════════════════════════════════════════
TIER_B = [
    R("CMK16GX4M2B3200C16", "Corsair", "Vengeance LPX", 8, 2, 3200, 16,
      tier="B", prix=60, liq=5, rot=7, rank="1Rx8", couleur="noir", haut=34,
      alias=["lpx 16go 3200", "corsair 2x8 3200"]),

    R("CMK16GX4M2Z3200C16", "Corsair", "Vengeance LPX", 8, 2, 3200, 16,
      tier="B", prix=60, liq=5, rot=7, rank="1Rx8", couleur="noir", haut=34),

    R("CMK16GX4M2D3600C18", "Corsair", "Vengeance LPX", 8, 2, 3600, 18,
      tier="B", prix=66, liq=4, rot=8, rank="1Rx8", couleur="noir", haut=34),

    R("CMK16GX4M2B3000C15", "Corsair", "Vengeance LPX", 8, 2, 3000, 15,
      tier="B", prix=52, liq=4, rot=9, rank="1Rx8", couleur="noir", haut=34),

    R("CMK16GX4M2A2666C16", "Corsair", "Vengeance LPX", 8, 2, 2666, 16,
      tier="B", prix=45, liq=4, rot=11, rank="1Rx8", couleur="noir", haut=34, volt=1.20),

    R("CMW16GX4M2C3200C16", "Corsair", "Vengeance RGB Pro", 8, 2, 3200, 16,
      tier="B", prix=72, liq=4, rot=9, rank="1Rx8", rgb=True, couleur="noir", haut=51),

    R("CMW16GX4M2Z3600C18", "Corsair", "Vengeance RGB Pro", 8, 2, 3600, 18,
      tier="B", prix=78, liq=4, rot=9, rank="1Rx8", rgb=True, couleur="noir", haut=51),

    R("CMK32GX4M1E3200C16", "Corsair", "Vengeance LPX", 32, 1, 3200, 16,
      tier="B", prix=105, liq=3, rot=12, rank="2Rx8", couleur="noir", haut=34,
      alias=["lpx 32go barrette seule", "corsair 1x32"],
      notes="Barrette 32 Go unitaire : marché étroit mais concurrence quasi nulle. Bon candidat appariement."),

    R("F4-3200C16D-16GVKB", "G.Skill", "Ripjaws V", 8, 2, 3200, 16,
      tier="B", prix=58, liq=5, rot=7, rank="1Rx8", couleur="noir", haut=42,
      alias=["ripjaws v 16go 3200"]),

    R("F4-3600C18D-16GVK", "G.Skill", "Ripjaws V", 8, 2, 3600, 18,
      tier="B", prix=64, liq=4, rot=8, rank="1Rx8", couleur="noir", haut=42),

    R("F4-3600C16D-16GTZNC", "G.Skill", "Trident Z Neo", 8, 2, 3600, 16,
      tier="B", prix=85, liq=4, rot=10, rank="1Rx8", rgb=True,
      couleur="noir/argent", haut=44),

    R("F4-3200C16S-32GVK", "G.Skill", "Ripjaws V", 32, 1, 3200, 16,
      tier="B", prix=100, liq=3, rot=13, rank="2Rx8", couleur="noir", haut=42,
      alias=["ripjaws 32go seule"],
      notes="Le 'S' du PN = single module (vs 'D' pour un kit de 2). Discriminant fiable."),

    R("F4-3200C16S-16GVK", "G.Skill", "Ripjaws V", 16, 1, 3200, 16,
      tier="B", prix=52, liq=4, rot=10, rank="2Rx8", couleur="noir", haut=42,
      notes="Cible prioritaire du radar d'appariement : 2 barrettes identiques = kit 32 Go."),

    R("KF432C16BBK2/16", "Kingston", "FURY Beast", 8, 2, 3200, 16,
      tier="B", prix=56, liq=5, rot=7, rank="1Rx8", couleur="noir", haut=34),

    R("KF432C16BB/32", "Kingston", "FURY Beast", 32, 1, 3200, 16,
      tier="B", prix=105, liq=3, rot=12, rank="2Rx8", couleur="noir", haut=34,
      alias=["fury beast 32go seule"]),

    R("KF432C16BB/16", "Kingston", "FURY Beast", 16, 1, 3200, 16,
      tier="B", prix=52, liq=4, rot=10, rank="2Rx8", couleur="noir", haut=34,
      notes="Barrette 16 Go unitaire, très fréquente en sortie de PC préassemblé. Appariement facile."),

    R("HX432C16FB3K2/16", "HyperX", "Fury", 8, 2, 3200, 16,
      tier="B", prix=54, liq=5, rot=7, rank="1Rx8", couleur="noir", haut=34),

    R("HX432C16FB3/16", "HyperX", "Fury", 16, 1, 3200, 16,
      tier="B", prix=50, liq=4, rot=10, rank="2Rx8", couleur="noir", haut=34),

    R("HX436C17FB3K2/16", "HyperX", "Fury", 8, 2, 3600, 17,
      tier="B", prix=62, liq=4, rot=9, rank="1Rx8", couleur="noir", haut=34),

    R("HX432C16PB3K2/16", "HyperX", "Predator", 8, 2, 3200, 16,
      tier="B", prix=62, liq=4, rot=10, rank="1Rx8", couleur="noir", haut=42,
      notes="⚠️ Predator existe en DDR3. Vérifier la fréquence et le socle mentionné."),

    R("BL2K8G32C16U4B", "Crucial", "Ballistix", 8, 2, 3200, 16,
      tier="B", prix=58, liq=5, rot=7, die="Micron E-die", rank="1Rx8",
      couleur="noir", haut=39),

    R("BL2K8G32C16U4W", "Crucial", "Ballistix", 8, 2, 3200, 16,
      tier="B", prix=66, liq=4, rot=9, die="Micron E-die", rank="1Rx8",
      couleur="blanc", haut=39),

    R("PVS416G320C6K", "Patriot", "Viper Steel", 8, 2, 3200, 16,
      tier="B", prix=55, liq=4, rot=9, rank="1Rx8", couleur="gris", haut=44),

    R("PVB416G360C8K", "Patriot", "Viper 4 Blackout", 8, 2, 3600, 18,
      tier="B", prix=60, liq=4, rot=9, rank="1Rx8", couleur="noir", haut=41),

    R("TLZGD416G3200HC16CDC01", "TeamGroup", "T-Force Vulcan Z", 8, 2, 3200, 16,
      tier="B", prix=48, liq=4, rot=10, rank="1Rx8", couleur="gris", haut=32),

    R("TDZAD416G3200HC16CDC01", "TeamGroup", "T-Force Dark Za", 8, 2, 3200, 16,
      tier="B", prix=48, liq=3, rot=12, rank="1Rx8", couleur="gris", haut=33,
      pn_ok=False, notes="PN à confirmer au premier calibrage (gamme peu vue en FR)."),

    R("CT32G4DFD832A", "Crucial", "CT (standard)", 32, 1, 3200, 22,
      tier="B", prix=100, liq=3, rot=14, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["crucial 32go 3200", "ct32g4dfd"],
      notes="Barrette nue 32 Go. 'DFD' = non-ECC dual rank. Le 'W' (CT..W..) serait de l'ECC → rejet."),
]


# ═══════════════════════════════════════════════════════════════════════════
#  TIER C — LOT OU TRÈS BAS PRIX SEULEMENT
#  4×8 (occupe les 4 slots, mauvais pour Ryzen), OEM nue, 2666/2400.
#  Ça se vend, mais lentement : ne jamais immobiliser de capital dessus.
# ═══════════════════════════════════════════════════════════════════════════
TIER_C = [
    R("CMK32GX4M4B3200C16", "Corsair", "Vengeance LPX", 8, 4, 3200, 16,
      tier="C", prix=100, liq=2, rot=22, rank="1Rx8", couleur="noir", haut=34,
      alias=["lpx 4x8 3200"],
      notes="4×8 : occupe les 4 slots, IMC Ryzen peine au-delà de 3200. Se vend mal en kit, mieux dépareillé."),

    R("F4-3200C16Q-32GVK", "G.Skill", "Ripjaws V", 8, 4, 3200, 16,
      tier="C", prix=98, liq=2, rot=22, rank="1Rx8", couleur="noir", haut=42,
      notes="'Q' = quad kit. Souvent plus rentable revendu en 2 paires qu'en kit de 4."),

    R("F4-2666C19D-16GVS", "G.Skill", "Ripjaws V", 8, 2, 2666, 19,
      tier="C", prix=40, liq=3, rot=15, rank="1Rx8", couleur="noir", haut=42, volt=1.20),

    R("KVR32N22D8/16", "Kingston", "ValueRAM", 16, 1, 3200, 22,
      tier="C", prix=42, liq=3, rot=14, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["kingston valueram 16go 3200", "kvr32n22"],
      notes="'N' = non-ECC unbuffered. Un 'E' à cette position (KVR32E22…) = ECC → rejet."),

    R("KVR32N22S8/8", "Kingston", "ValueRAM", 8, 1, 3200, 22,
      tier="C", prix=20, liq=3, rot=15, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("KVR26N19S8/8", "Kingston", "ValueRAM", 8, 1, 2666, 19,
      tier="C", prix=17, liq=3, rot=18, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("KVR26N19D8/16", "Kingston", "ValueRAM", 16, 1, 2666, 19,
      tier="C", prix=34, liq=3, rot=16, rank="2Rx8", couleur="vert", haut=31, volt=1.20),

    R("CT16G4DFRA32A", "Crucial", "CT (standard)", 16, 1, 3200, 22,
      tier="C", prix=42, liq=3, rot=14, rank="1Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["crucial 16go 3200 nue"]),

    R("CT16G4DFD832A", "Crucial", "CT (standard)", 16, 1, 3200, 22,
      tier="C", prix=45, liq=3, rot=14, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      notes="Dual rank : léger gain perf sur Ryzen, à mentionner en annonce."),

    R("CT8G4DFS832A", "Crucial", "CT (standard)", 8, 1, 3200, 22,
      tier="C", prix=20, liq=3, rot=16, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("HMA82GU6DJR8N-XN", "SK Hynix", "OEM nue", 16, 1, 3200, 22,
      tier="C", prix=40, liq=2, rot=20, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["hynix 16go 3200", "hma82gu6"],
      notes="'U6' = UDIMM non-ECC. 'U7' (HMA82GU7…) = ECC → rejet immédiat."),

    R("HMA81GU6DJR8N-XN", "SK Hynix", "OEM nue", 8, 1, 3200, 22,
      tier="C", prix=19, liq=2, rot=22, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("HMA82GU6CJR8N-VK", "SK Hynix", "OEM nue", 16, 1, 2666, 19,
      tier="C", prix=34, liq=2, rot=22, rank="2Rx8", couleur="vert", haut=31, volt=1.20),

    R("HMA81GU6CJR8N-VK", "SK Hynix", "OEM nue", 8, 1, 2666, 19,
      tier="C", prix=16, liq=2, rot=25, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("M378A2G43AB3-CWE", "Samsung", "OEM nue", 16, 1, 3200, 22,
      tier="C", prix=40, liq=2, rot=20, rank="1Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["samsung 16go 3200", "m378a2g43"],
      notes="'M378' = UDIMM non-ECC. M391 = ECC UDIMM, M393 = RDIMM → tous deux à rejeter."),

    R("M378A2K43CB1-CTD", "Samsung", "OEM nue", 16, 1, 2666, 19,
      tier="C", prix=33, liq=2, rot=22, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      notes="Malgré le motif 'A2K43', c'est bien du NON-ECC : c'est le préfixe M378 qui tranche."),

    R("M378A1K43CB2-CTD", "Samsung", "OEM nue", 8, 1, 2666, 19,
      tier="C", prix=16, liq=2, rot=25, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("M378A1K43DB2-CVF", "Samsung", "OEM nue", 8, 1, 2933, 21,
      tier="C", prix=18, liq=2, rot=24, rank="1Rx8", couleur="vert", haut=31, volt=1.20),

    R("MTA16ATF2G64AZ-3G2", "Micron", "OEM nue", 16, 1, 3200, 22,
      tier="C", prix=38, liq=2, rot=22, rank="2Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["micron 16go 3200", "mta16atf2g64"],
      notes="'AZ' = non-ECC. Un 'AZ' remplacé par 'HZ'/'G7' indique de l'ECC → rejet."),

    R("MTA8ATF1G64AZ-3G2", "Micron", "OEM nue", 8, 1, 3200, 22,
      tier="C", prix=18, liq=2, rot=24, rank="1Rx8", couleur="vert", haut=31, volt=1.20,
      alias=["mta8atf1g64az"]),
]


# ═══════════════════════════════════════════════════════════════════════════
#  TIER D — NO-NAME : REFUS SAUF LOT QUASI GRATUIT
#  Puces déclassées ou relabellisées, XMP instable, revente très difficile.
#  Ces marques n'impriment pas de part number stable : la clé est donc
#  synthétique (NONAME:MARQUE-FREQ-CAPACITÉ) et le matching se fait sur la
#  marque + les specs, pas sur un PN. Warning explicite en notification.
# ═══════════════════════════════════════════════════════════════════════════
TIER_D = [
    R("NONAME:QIYIDA-3200-2X8", "Qiyida", "no-name", 8, 2, 3200, 16,
      tier="D", prix=25, liq=1, rot=50, couleur="noir", haut=40, pn_ok=False,
      alias=["qiyida ddr4", "qiyida 3200"],
      notes="Puces déclassées, XMP souvent instable. Revente FR très difficile."),

    R("NONAME:QIYIDA-3200-1X16", "Qiyida", "no-name", 16, 1, 3200, 16,
      tier="D", prix=20, liq=1, rot=50, couleur="noir", haut=40, pn_ok=False),

    R("NONAME:KINGSPEC-3200-1X16", "Kingspec", "no-name", 16, 1, 3200, 16,
      tier="D", prix=20, liq=1, rot=50, couleur="noir", haut=35, pn_ok=False,
      alias=["kingspec ddr4"]),

    R("NONAME:JUHOR-3200-2X8", "JUHOR", "no-name", 8, 2, 3200, 16,
      tier="D", prix=24, liq=1, rot=50, couleur="noir", haut=40, pn_ok=False,
      alias=["juhor ddr4"]),

    R("NONAME:ASGARD-3200-1X16", "Asgard", "no-name", 16, 1, 3200, 16,
      tier="D", prix=22, liq=1, rot=45, couleur="noir", haut=38, pn_ok=False,
      alias=["asgard ddr4", "asgard loki"]),

    R("NONAME:GLOWAY-3200-2X8", "Gloway", "no-name", 8, 2, 3200, 16,
      tier="D", prix=24, liq=1, rot=50, couleur="noir", haut=40, pn_ok=False,
      alias=["gloway ddr4"]),

    R("NONAME:KIMTIGO-3200-1X8", "Kimtigo", "no-name", 8, 1, 3200, 16,
      tier="D", prix=12, liq=1, rot=55, couleur="noir", haut=32, pn_ok=False,
      alias=["kimtigo ddr4"]),

    R("NONAME:SNOAMOO-3200-2X8", "Snoamoo", "no-name", 8, 2, 3200, 16,
      tier="D", prix=22, liq=1, rot=55, couleur="noir", haut=40, pn_ok=False,
      alias=["snoamoo ddr4"]),

    R("NONAME:ZIFEI-3200-1X16", "Zifei", "no-name", 16, 1, 3200, 16,
      tier="D", prix=20, liq=1, rot=50, couleur="noir", haut=33, pn_ok=False,
      alias=["zifei ddr4"]),

    R("NONAME:NETAC-3200-2X8", "Netac", "no-name", 8, 2, 3200, 16,
      tier="D", prix=28, liq=1, rot=45, couleur="noir", haut=42, pn_ok=False,
      alias=["netac shadow", "netac ddr4"],
      notes="Netac hors gamme premium. Un peu mieux distribué que les autres no-name."),

    # ── Exception 4 Go : uniquement en lot de 10+ à moins de 1,50 € pièce ──
    R("NONAME:LOT-4GO-DDR4", "générique", "lot 4 Go", 4, 1, 2400, 17,
      tier="D", prix=6, liq=1, rot=60, couleur="vert", haut=31, volt=1.20,
      pn_ok=False, alias=["lot ram 4go ddr4", "barrettes 4 go ddr4"],
      notes="SEULE exception 4 Go autorisée : lot de 10+ à ≤ 1,50 €/pièce, revente en lot uniquement."),
]


# ═══════════════════════════════════════════════════════════════════════════
REFERENCES = TIER_S + TIER_A + TIER_B + TIER_C + TIER_D


def par_tier():
    """{tier: [références]} — utilisé par le dashboard et les stats de seed."""
    out = {}
    for r in REFERENCES:
        out.setdefault(r["tier"], []).append(r)
    return out


def verifier_coherence():
    """Garde-fou exécuté au seed et par les tests : détecte les incohérences de
    saisie avant qu'elles ne polluent le scoring. Retourne une liste d'erreurs."""
    erreurs = []
    vus = set()
    for r in REFERENCES:
        pn = r["part_number"]
        if pn in vus:
            erreurs.append(f"{pn}: part number en double")
        vus.add(pn)

        if r["capacite_totale_go"] != r["capacite_module_go"] * r["nb_modules"]:
            erreurs.append(f"{pn}: capacité totale incohérente")
        if r["frequence_mhz"] < 2133:
            erreurs.append(f"{pn}: fréquence {r['frequence_mhz']} < 2133 (hors périmètre DDR4)")
        if r["capacite_module_go"] == 4 and r["tier"] != "D":
            erreurs.append(f"{pn}: une barrette 4 Go ne peut être qu'en tier D (lot)")
        if r["tier"] not in ("S", "A", "B", "C", "D"):
            erreurs.append(f"{pn}: tier invalide {r['tier']}")
        if not 1 <= r["liquidite"] <= 5:
            erreurs.append(f"{pn}: liquidité hors 1-5")
        if r["prix_ref_occasion_eur"] <= 0:
            erreurs.append(f"{pn}: prix de référence nul ou négatif")
        # Un CL14 en 3200 sans die renseigné = presque toujours du B-die oublié.
        if r["frequence_mhz"] == 3200 and r["cas_latency"] == 14 and not r["die_type"]:
            erreurs.append(f"{pn}: 3200 CL14 sans die_type (B-die probable, à renseigner)")
    return erreurs


if __name__ == "__main__":
    from collections import Counter
    print(f"{len(REFERENCES)} références")
    for tier, refs in sorted(par_tier().items()):
        print(f"  tier {tier}: {len(refs):>3}")
    print("\nMarques:", dict(Counter(r["marque"] for r in REFERENCES).most_common()))
    errs = verifier_coherence()
    print(f"\nCohérence: {'OK' if not errs else str(len(errs)) + ' erreur(s)'}")
    for e in errs:
        print("  ✗", e)
