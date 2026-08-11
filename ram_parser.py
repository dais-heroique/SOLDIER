"""
ram_parser.py — Identification textuelle d'une annonce DDR4
═══════════════════════════════════════════════════════════════════════════
Transforme un titre + une description en caractéristiques exploitables :
part number, capacité, nombre de barrettes, fréquence, CAS, et surtout
EXCLUSIONS (SO-DIMM, ECC, DDR3/DDR5, 4 Go) avant tout calcul de score.

── Sur la détection ECC : pourquoi pas une simple recherche de sous-chaîne ──
La règle « un E ou un W chez Crucial/Kingston = ECC » est vraie, mais
uniquement À UNE POSITION PRÉCISE du part number :
    KVR32E22D8/16   → E après la fréquence  = ECC
    KVR32N22D8/16   → N                     = non-ECC
    CT16G4WFD8266   → W avant FD            = ECC
    CT16G4DFD832A   → D                     = non-ECC
Cherchée en sous-chaîne, cette règle rejette CMK32GX4M2**E**3200C16 — le
Corsair Vengeance LPX 2×16, c'est-à-dire la référence la plus liquide du
marché. Ici, chaque constructeur a donc son motif positionnel (ECC_MOTIFS),
et les PN Corsair/G.Skill ne sont jamais concernés.

De même « A2K43 chez Samsung = ECC » est faux : M378A2K43CB1-CTD est du
non-ECC parfaitement ordinaire. Chez Samsung c'est le PRÉFIXE qui tranche :
    M378 = UDIMM non-ECC   M391 = UDIMM ECC   M393 = RDIMM   M471 = SO-DIMM
"""

import re
import unicodedata

import ram_config
import ram_db

# ─────────────────────── NORMALISATION TEXTE ───────────────────────
_ESPACES = re.compile(r"\s+")


def normaliser(texte):
    """Minuscules, sans accents, ponctuation réduite à des espaces. Permet de
    chercher 'mémoire vive' et 'MEMOIRE-VIVE' avec le même motif."""
    if not texte:
        return ""
    t = unicodedata.normalize("NFD", str(texte))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").lower()
    t = re.sub(r"[^a-z0-9./+-]+", " ", t)
    return _ESPACES.sub(" ", t).strip()


def contient_terme(texte_norm, terme):
    """Recherche en mot entier : évite que 'ecc' matche dans 'occasion' ou
    'seconde', et que 'ddr3' matche dans 'ddr3200' (faute de frappe fréquente
    pour DDR4 3200)."""
    t = normaliser(terme)
    if not t:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", texte_norm) is not None


# ─────────────────────── PART NUMBERS ───────────────────────
# Un motif par constructeur : plus fiable qu'un motif générique, et permet de
# déduire la marque du seul PN.
PN_MOTIFS = [
    # Corsair : CMK32GX4M2E3200C16, CMW/CMH/CMT/CMD/CMU/CMR
    ("Corsair", re.compile(r"\bCM[KWHTDUR]\d{1,3}GX4M\d[A-Z]?\d{3,4}C\d{2}[A-Z]?\b", re.I)),
    # G.Skill : F4-3200C14D-16GTZ / F4-3600C16Q-64GTZN
    ("G.Skill", re.compile(r"\bF4[-\s]?\d{4}C\d{2}[DSQTK][-\s]?\d{1,3}G[A-Z]{2,5}\b", re.I)),
    # Kingston FURY / ValueRAM / Server Premier. Les codes de fréquence n'ont
    # pas la même longueur selon la gamme : KF432 (3 chiffres) vs KVR32 (2).
    ("Kingston", re.compile(r"\b(?:KF\d{3}|KVR\d{2}|KSM\d{2})[A-Z]?\d{2}"
                            r"[A-Z0-9]{1,8}?(?:K\d)?/\d{1,3}[A-Z]{0,3}\b", re.I)),
    # HyperX : HX432C16FB3K2/16
    ("HyperX", re.compile(r"\bHX\d{3}C\d{2}[A-Z]{2,3}\d?(?:K\d)?/\d{1,3}\b", re.I)),
    # Crucial Ballistix : BL2K16G36C16U4B / BLM2K8G44C19U4B
    ("Crucial", re.compile(r"\bBLM?\d?K?\d{1,2}G\d{2}C\d{2}U\d[A-Z]{0,2}\b", re.I)),
    # Crucial standard : CT16G4DFD832A
    ("Crucial", re.compile(r"\bCT\d{1,3}G4[A-Z]{3}\d{3,4}[A-Z]?\b", re.I)),
    # Patriot : PVS432G360C8K / PVB416G360C8K
    ("Patriot", re.compile(r"\bPV[SBRE]\d{2,3}G\d{3}C\d[A-Z]?K?\b", re.I)),
    # TeamGroup : TLZGD432G3200HC16CDC01
    ("TeamGroup", re.compile(r"\bT[A-Z0-9]{2,5}\d{2,3}G\d{4}HC\d{2}[A-Z]{3}\d{2}\b", re.I)),
    # SK Hynix : HMA82GU6DJR8N-XN
    ("SK Hynix", re.compile(r"\bHMA\d{2}G[UR]\d[A-Z]{3}\d[A-Z](?:[-\s]?[A-Z0-9]{2,3})?\b", re.I)),
    # Samsung : M378A2G43AB3-CWE — M378 (UDIMM non-ECC) + A (DDR4) + densité
    # + révision + code de fréquence. Le préfixe M391/M393 est traité en ECC.
    ("Samsung", re.compile(r"\bM\d{3}A\d[A-Z]\d{2}[A-Z]{2}\d(?:[-\s]?[A-Z0-9]{3})?\b", re.I)),
    # Micron : MTA16ATF2G64AZ-3G2
    ("Micron", re.compile(r"\bMTA\d{1,2}[A-Z]{3}\d[A-Z]\d{2}[A-Z]{2}(?:[-\s]?\w{3,6})?\b", re.I)),
    # ADATA XPG : AX4U320038G16A-DB35
    ("ADATA", re.compile(r"\bAX4U\d{4,7}G\d{2}[A-Z][-\s]?[A-Z]{2}\d{2}\b", re.I)),
]

# Marques citées en clair (repli quand aucun PN n'est lisible)
MARQUES = {
    "corsair": "Corsair", "g.skill": "G.Skill", "gskill": "G.Skill", "g skill": "G.Skill",
    "kingston": "Kingston", "hyperx": "HyperX", "crucial": "Crucial", "ballistix": "Crucial",
    "patriot": "Patriot", "teamgroup": "TeamGroup", "team group": "TeamGroup",
    "t-force": "TeamGroup", "adata": "ADATA", "xpg": "ADATA", "samsung": "Samsung",
    "hynix": "SK Hynix", "sk hynix": "SK Hynix", "micron": "Micron",
    "qiyida": "Qiyida", "kingspec": "Kingspec", "juhor": "JUHOR", "snoamoo": "Snoamoo",
    "gloway": "Gloway", "asgard": "Asgard", "kimtigo": "Kimtigo", "zifei": "Zifei",
    "netac": "Netac",
    # Marques secondaires légitimes, fréquentes sur Vinted FR
    "lexar": "Lexar", "apacer": "Apacer", "klevv": "KLEVV",
    "silicon power": "Silicon Power", "pny": "PNY", "geil": "GeIL",
    "mushkin": "Mushkin", "transcend": "Transcend", "neo forza": "Neo Forza",
    "neoforza": "Neo Forza", "ramaxel": "Ramaxel",
}

NO_NAME = {"Qiyida", "Kingspec", "JUHOR", "Snoamoo", "Gloway", "Asgard",
           "Kimtigo", "Zifei", "Netac"}

GAMMES = [
    ("trident z royal", "Trident Z Royal"), ("trident z neo", "Trident Z Neo"),
    ("trident z rgb", "Trident Z RGB"), ("trident z", "Trident Z"),
    ("ripjaws v", "Ripjaws V"), ("ripjaws", "Ripjaws V"),
    ("vengeance rgb pro sl", "Vengeance RGB Pro SL"),
    ("vengeance rgb pro", "Vengeance RGB Pro"), ("vengeance rgb", "Vengeance RGB Pro"),
    ("vengeance lpx", "Vengeance LPX"), ("vengeance", "Vengeance LPX"),
    ("dominator platinum rgb", "Dominator Platinum RGB"),
    ("dominator", "Dominator Platinum"),
    ("fury renegade", "FURY Renegade"), ("renegade", "FURY Renegade"),
    ("fury beast", "FURY Beast"), ("fury", "Fury"),
    ("predator", "Predator"), ("ballistix max", "Ballistix MAX"),
    ("ballistix", "Ballistix"), ("viper steel", "Viper Steel"),
    ("viper blackout", "Viper 4 Blackout"), ("blackout", "Viper 4 Blackout"),
    ("viper", "Viper"), ("vulcan", "T-Force Vulcan Z"), ("xtreem", "T-Force Xtreem ARGB"),
    ("valueram", "ValueRAM"),
    ("thor", "THOR"), ("panther", "Panther"), ("nox", "NOX"), ("bolt", "BOLT X"),
    ("cras", "CRAS XR RGB"), ("xpower", "XPOWER Turbine"), ("xlr8", "XLR8 Gaming"),
    ("super luce", "Super Luce RGB"), ("evo potenza", "EVO Potenza"),
    ("redline", "Redline"), ("jetram", "JetRam"),
]

# ─────────────────────── ECC : MOTIFS POSITIONNELS ───────────────────────
# Chaque entrée : (nom, motif ECC/registered, motif explicitement non-ECC).
ECC_MOTIFS = [
    # Kingston : KVR32E22D8/16 (E = ECC) vs KVR32N22D8/16 (N = non-ECC)
    ("Kingston KVR", re.compile(r"\bKVR\d{2}E\d{2}", re.I)),
    # Kingston Server Premier : KSM26ES8/8ME
    ("Kingston KSM", re.compile(r"\bKSM\d{2}[EDRLS]", re.I)),
    # Crucial : CT16G4WFD8266 (W = ECC) vs CT16G4DFD832A
    ("Crucial W", re.compile(r"\bCT\d{1,3}G4W", re.I)),
    # Crucial ECC/RDIMM serveur : CT16G4RFD424A
    ("Crucial R", re.compile(r"\bCT\d{1,3}G4R", re.I)),
    # SK Hynix : U7 = ECC UDIMM, R7/R8 = RDIMM (vs U6 = non-ECC)
    ("Hynix U7/R", re.compile(r"\bHMA\d{2}G[UR]7", re.I)),
    ("Hynix RDIMM", re.compile(r"\bHMA\d{2}GR\d", re.I)),
    # Samsung : M391 = UDIMM ECC, M393 = RDIMM (vs M378 = non-ECC)
    ("Samsung M391/M393", re.compile(r"\bM39[13]A", re.I)),
    # Micron ECC : MTA18ASF… (18 puces) et suffixes -2G2/-3G2 en HZ
    ("Micron 18AS", re.compile(r"\bMTA\d{2}AS[FN]", re.I)),
]

# Organisation x4 : n'existe que sur du registered/serveur
RANK_X4 = re.compile(r"\b[1248]rx4\b", re.I)

# Préfixes SO-DIMM constructeur (une SO-DIMM ne dit pas toujours "sodimm")
SODIMM_MOTIFS = [
    re.compile(r"\bM471[AB]", re.I),                 # Samsung SO-DIMM
    re.compile(r"\bHMA\d{2}GS6", re.I),              # Hynix SO-DIMM
    re.compile(r"\bCT\d{1,3}G4SFS", re.I),           # Crucial SO-DIMM
    re.compile(r"\bKVR\d{2}S\d{2}", re.I),           # Kingston SO-DIMM
    re.compile(r"\bCMSX\d", re.I),                   # Corsair Vengeance SO-DIMM
    re.compile(r"\bMTA\d{1,2}ATF\d[A-Z]\d{2}HZ", re.I),
]

# ─────────────────────── SPECS ───────────────────────
# Unités de capacité telles qu'elles s'écrivent VRAIMENT dans les annonces :
# « 16go », « 16 Go », « 16gb », « 16g », « 16giga », « 16 gigas ».
# Le « g » seul et « giga » sont fréquents et étaient ignorés : « DDR4 2x8g »
# n'était pas reconnu comme un kit, et l'annonce se faisait valoriser comme une
# barrette unique de 16 Go.
_UNITE_GO = r"(?:g[ob]|gigas?|g)\b"

# Capacité : "2x16 go", "2 x 16go", "2x8g", "32 go (2x16)"
RE_KIT = re.compile(r"(?<![a-z0-9])([1-8])\s*[x*]\s*(4|8|16|32)\s*(?:" + _UNITE_GO + r")?"
                    r"(?![a-z0-9])", re.I)
# Forme inversée, courante sur les lots : "4go x12", "8 go * 4"
RE_KIT_INVERSE = re.compile(r"(?<![a-z0-9])(4|8|16|32)\s*" + _UNITE_GO +
                            r"\s*[x*]\s*(\d{1,2})(?![a-z0-9])", re.I)
# Quantité annoncée en toutes lettres : "lot de 12", "12 barrettes"
RE_QUANTITE = re.compile(r"\b(?:lot de|ensemble de|paquet de)\s*(\d{1,2})\b|"
                         r"\b(\d{1,2})\s*barrettes\b", re.I)
# Vente à l'unité, souvent d'une barrette issue d'un kit. Formulation fréquente
# sur Vinted et c'est précisément le gisement du radar d'appariement — la
# confondre avec un kit ferait doubler la valeur estimée.
RE_UNITAIRE = re.compile(
    r"\b(?:une?\s+seule?\s+barrette|1\s*seule?\s+barrette|barrette\s+seule|"
    r"une?\s+barrette\s+(?:du|de\s+la|sur)\b|a\s+l\s*unite|vendue?\s+seule|"
    r"1\s*barrette|une\s+seule)\b", re.I)
RE_CAPACITE = re.compile(r"(?<![a-z0-9])(4|8|16|32|64|128)\s*" + _UNITE_GO, re.I)
RE_FREQUENCE = re.compile(r"(?<![a-z0-9])(1333|1600|1866|2133|2400|2666|2800|2933|3000|3066|"
                          r"3200|3333|3466|3600|3733|3800|4000|4133|4266|4400|4600|4800)"
                          r"\s*(?:mhz|mt/s)?(?![a-z0-9])", re.I)
RE_CL = re.compile(r"\bc(?:l|as)?\s*[-]?\s*(9|1[0-9]|2[0-4])(?![0-9])", re.I)
RE_TIMINGS = re.compile(r"\b(9|1[0-9]|2[0-4])-(\d{1,2})-(\d{1,2})-(\d{1,3})\b")
RE_RANK = re.compile(r"\b([12])rx(4|8|16)\b", re.I)

RGB_TERMES = ["rgb", "argb", "led", "lumineuse", "lumineuses"]
BLANC_TERMES = ["blanc", "blanche", "white"]
MAIN_PROPRE = ["main propre", "remise en main propre", "sur place", "a recuperer",
               "remise en main", "retrait", "pas d envoi", "pas d'envoi"]
SANS_BOITE = ["sans boite", "sans boîte", "pas de boite", "sans emballage", "vrac", "nue"]
DISSIPATEUR_KO = ["sans dissipateur", "dissipateur manquant", "dissipateur abime",
                  "dissipateur casse", "radiateur manquant", "heatspreader manquant"]
MEMTEST = ["memtest", "mem test", "memtest86", "teste 8 passes", "sans erreur"]
TESTE = ["teste", "testee", "testees", "fonctionne", "fonctionnel", "fonctionnelle",
         "en parfait etat de marche", "ok"]
HS_TERMES = ["hs", "ne fonctionne pas", "en panne", "defectueux", "defectueuse",
             "pour piece", "pour pieces", "non fonctionnel"]


def _int(m, groupe=1):
    try:
        return int(m.group(groupe))
    except (AttributeError, ValueError, IndexError):
        return None


def extraire_part_number(texte):
    """(part_number, marque_deduite) ou (None, None). On rend le PN tel qu'écrit
    dans l'annonce : la normalisation pour le matching se fait plus tard."""
    for marque, motif in PN_MOTIFS:
        m = motif.search(texte)
        if m:
            return m.group(0).strip(), marque
    return None, None


def detecter_marque(texte_norm, pn_marque=None):
    if pn_marque:
        return pn_marque
    for cle, marque in MARQUES.items():
        if cle in texte_norm:
            return marque
    return None


def detecter_gamme(texte_norm):
    for cle, gamme in GAMMES:
        if cle in texte_norm:
            return gamme
    return None


def extraire_specs(texte, texte_norm):
    """Capacité par barrette, nombre de barrettes, fréquence, CAS."""
    specs = {"capacite_module_go": None, "nb_modules": None, "capacite_totale_go": None,
             "frequence_mhz": None, "cas_latency": None, "rank": None, "est_kit": False}

    # 1) Forme explicite "2x16" : la plus fiable, elle donne les deux infos.
    m_kit = RE_KIT.search(texte_norm)
    if m_kit:
        specs["nb_modules"] = int(m_kit.group(1))
        specs["capacite_module_go"] = int(m_kit.group(2))
        specs["est_kit"] = specs["nb_modules"] > 1
    else:
        m_inv = RE_KIT_INVERSE.search(texte_norm)
        if m_inv:
            specs["capacite_module_go"] = int(m_inv.group(1))
            specs["nb_modules"] = int(m_inv.group(2))
            specs["est_kit"] = specs["nb_modules"] > 1

    # 2) Sinon, une capacité seule. 64/128 Go annoncés sans "x" sont
    #    presque toujours un total : on déduit le kit le plus probable.
    if specs["capacite_module_go"] is None:
        capacites = [int(x) for x in RE_CAPACITE.findall(texte_norm)]
        capacites = [c for c in capacites if c in (4, 8, 16, 32, 64, 128)]
        if capacites:
            total = max(capacites)
            if total == 64:
                specs["capacite_module_go"], specs["nb_modules"] = 32, 2
                specs["est_kit"] = True
            elif total == 128:
                specs["capacite_module_go"], specs["nb_modules"] = 32, 4
                specs["est_kit"] = True
            else:
                specs["capacite_module_go"] = total
                specs["nb_modules"] = 1
                # Une capacité annoncée seule est une capacité TOTALE. S'il
                # s'avère plus loin que l'annonce parle d'un kit, il faudra la
                # diviser, pas la multiplier : « Kit 16 Go » = 2×8, jamais 2×16.
                specs["capacite_est_totale"] = True

    if specs["capacite_module_go"] and specs["nb_modules"]:
        specs["capacite_totale_go"] = specs["capacite_module_go"] * specs["nb_modules"]

    # Quantité annoncée hors notation "NxM" : "lot de 12", "12 barrettes".
    # Indispensable pour l'exception 4 Go, qui ne vaut qu'à partir de 10 unités.
    m_qte = RE_QUANTITE.search(texte_norm)
    if m_qte and specs["nb_modules"] in (None, 1):
        qte = int(m_qte.group(1) or m_qte.group(2))
        if 2 <= qte <= 40:
            specs["nb_modules"] = qte
            specs["est_kit"] = True

    # "kit", "paire", "duo" → c'est un kit même sans quantité explicite.
    # Sauf mention contraire : « une seule barrette du kit » parle bien d'un
    # kit, mais n'en vend qu'une barrette.
    vente_unitaire = bool(RE_UNITAIRE.search(texte_norm))
    if vente_unitaire:
        specs["nb_modules"] = 1
        specs["est_kit"] = False
        specs["vente_unitaire"] = True
    elif not specs["est_kit"] and re.search(r"\b(kit|paire|duo)\b", texte_norm):
        specs["est_kit"] = True
        if specs["nb_modules"] in (None, 1):
            specs["nb_modules"] = 2
            # Déduit du mot « kit », pas annoncé : ne pas le reprocher au
            # vendeur sous forme d'alerte de cohérence.
            specs["nb_modules_infere"] = True
            # « Kit 16 Go » annonce un TOTAL de 16 Go, donc 2×8. Multiplier au
            # lieu de diviser valoriserait un kit de 16 Go au prix d'un kit de
            # 32 Go — soit environ le double, et un achat à perte assuré.
            module = specs.get("capacite_module_go")
            if specs.get("capacite_est_totale") and module and module >= 8 \
                    and module // 2 in (4, 8, 16, 32):
                specs["capacite_module_go"] = module // 2

    if specs["capacite_module_go"] and specs["nb_modules"]:
        specs["capacite_totale_go"] = specs["capacite_module_go"] * specs["nb_modules"]

    freqs = [int(f) for f in RE_FREQUENCE.findall(texte_norm)]
    if freqs:
        # La plus haute : les annonces citent souvent "2133 par défaut, 3200 en XMP"
        specs["frequence_mhz"] = max(freqs)
        specs["frequence_min_citee"] = min(freqs)

    m_tim = RE_TIMINGS.search(texte_norm)
    if m_tim:
        specs["cas_latency"] = int(m_tim.group(1))
    else:
        specs["cas_latency"] = _int(RE_CL.search(texte_norm))

    m_rank = RE_RANK.search(texte_norm)
    if m_rank:
        specs["rank"] = f"{m_rank.group(1)}Rx{m_rank.group(2)}"
    return specs


# ─────────────────────── TYPE DE PRODUIT ───────────────────────
# Une liste de phrases exactes ne suffit pas : « mini pc » ne reconnaît ni
# « PC Mini ITX », ni « ThinkCentre M720q Tiny », ni « Laptops » au pluriel.
# On raisonne donc par FAISCEAU D'INDICES — un titre qui cite un processeur ET
# un SSD décrit un ordinateur, quels que soient les mots employés.

# Machines à mémoire SO-DIMM : hors périmètre par nature, la RAM à l'intérieur
# n'est pas de l'UDIMM desktop.
MACHINES_SODIMM = re.compile(
    r"\b(mini[\s-]?pc|micro[\s-]?pc|nuc|barebone|tiny|micro[\s-]?form|usff|"
    r"tout[\s-]en[\s-]un|all[\s-]?in[\s-]?one|aio|"
    r"portables?|laptops?|notebooks?|ultrabooks?|chromebooks?|netbooks?|"
    r"macbooks?|imacs?|mac\s?mini|"
    r"thinkpad|thinkcentre|elitebook|probook|elitedesk|prodesk|latitude|"
    r"optiplex\s?(micro|mff)|inspiron|pavilion|vivobook|ideapad|zenbook|"
    r"aspire|satellite|dynabook|omnibook|travelmate|extensa)\b", re.I)

# Machines de bureau à mémoire UDIMM : la RAM dedans est dans le périmètre,
# mais il faut démonter et écouler le reste — ce n'est pas le même métier.
MACHINES_UDIMM = re.compile(
    r"\b(unite\s?centrale|tour\s?(gamer|gaming|pc)?|pc\s?(fixe|complet|bureau|"
    r"gamer|gaming|monte|multimedia|bureautique)|"
    r"ordinateur\s?(de\s?)?(bureau|fixe|complet)?|"
    r"config\s?(gamer|complete)|setup\s?(gamer|complet)|station\s?de\s?travail|"
    r"workstation|serveur\s?tour|mini[\s-]?itx|micro[\s-]?atx)\b", re.I)

# Un titre qui COMMENCE par « PC », « Ordinateur », « Tour »… décrit la machine
# elle-même. « RAM DDR4 pour PC gamer » ne commence pas par là.
RE_TITRE_MACHINE = re.compile(
    r"^\s*(pc|ordinateur|tour|unite\s?centrale|station|desktop|"
    r"lenovo|dell|hp|asus|acer|msi|medion|packard)\b", re.I)

# Indices de composants, PONDÉRÉS. Une annonce de barrette n'a aucune raison
# de citer une capacité de SSD ou une carte mère : ces deux-là pèsent double.
INDICES_COMPOSANTS = {
    "stockage": (2, re.compile(
        r"\b(ssd|nvme|hdd|disque\s?dur|m\.?2\b|\d{3,4}\s?g[ob]\s?(ssd|hdd|nvme)|"
        r"\d\s?to\b|\d{3,4}gb\s?(ssd|hdd))\b", re.I)),
    "carte_mere": (2, re.compile(
        r"\b(carte\s?mere|carte\s?mère|motherboard|socket\s?am[45]|"
        r"chipset\s?[abxz]\d{3})\b", re.I)),
    "processeur": (1, re.compile(
        r"\b(i[3579][\s-]?\d{3,5}[a-z]{0,2}|core\s?i[3579]|intel\s?core|"
        r"ryzen\s?[3579]|athlon|celeron|pentium|core\s?2|threadripper|"
        r"core\s?ultra|\d{1,2}(th|eme|e)\s?gen)\b", re.I)),
    "carte_graphique": (1, re.compile(
        r"\b(rtx\s?\d{3,4}|gtx\s?\d{3,4}|\brx\s?\d{3,4}|radeon|geforce|quadro|"
        r"carte\s?graphique|nvidia)\b", re.I)),
    "boitier": (1, re.compile(
        r"\b(boitier|alimentation|\bpsu\b|ventirad|watercooling|"
        r"ecran|clavier|souris)\b", re.I)),
    "systeme": (1, re.compile(
        r"\b(windows\s?1[01]|win\s?1[01]|windows\s?pro|licence\s?windows|"
        r"macos|preinstalle)\b", re.I)),
}

# Le titre annonce-t-il d'abord de la mémoire ? Un titre qui COMMENCE par
# « RAM », « Barrette » ou « Mémoire » vend de la mémoire, même s'il précise
# ensuite « pour PC gamer ».
RE_TITRE_MEMOIRE = re.compile(
    r"^\s*(kit\s+)?(de\s+)?(ram|barrettes?|memoire|memoires?|ddr4?|dimm|udimm|"
    r"module\s?(s|memoire)?)\b", re.I)

SEUIL_MACHINE = 3


def detecter_type_produit(titre, texte_norm, pn=None, marque=None, gamme=None):
    """Classe l'annonce : 'barrette', 'machine_sodimm', 'machine_udimm' ou
    'incertain'.

    Retourne (type, score, indices). Les indices expliquent la décision dans
    --diag, plutôt que de rejeter sans dire pourquoi.

    Faisceau d'indices pondérés, et non liste de phrases interdites : c'est ce
    qui permet de reconnaître « PC Mini ITX Ryzen 5600G » ou « ThinkCentre
    M720q Tiny » sans les avoir prévus mot pour mot. Les signaux « c'est bien
    de la mémoire » retranchent, sinon « RAM DDR4 pour PC gamer » basculerait
    à tort — or « ram pc gamer » est l'un des mots-clés de recherche.
    """
    points = 0
    indices = []

    m_sodimm = MACHINES_SODIMM.search(texte_norm)
    if m_sodimm:
        points += 3
        indices.append(f"machine portable/compacte : « {m_sodimm.group(0)} »")

    m_udimm = MACHINES_UDIMM.search(texte_norm)
    if m_udimm:
        points += 2
        indices.append(f"ordinateur de bureau : « {m_udimm.group(0)} »")

    titre_norm = normaliser(titre or "")
    if RE_TITRE_MACHINE.match(titre_norm):
        points += 2
        indices.append("titre décrivant une machine")

    for famille, (poids, motif) in INDICES_COMPOSANTS.items():
        trouve = motif.search(texte_norm)
        if trouve:
            points += poids
            indices.append(f"{famille} cité : « {trouve.group(0)} »")

    # ── Signaux inverses : l'annonce vend bien de la mémoire ──
    if RE_TITRE_MEMOIRE.match(titre_norm):
        points -= 2
        indices.append("titre annonçant de la mémoire")
    if pn:
        points -= 3
        indices.append(f"part number mémoire lu ({pn})")
    if marque and gamme:
        points -= 2
        indices.append(f"gamme mémoire identifiée ({marque} {gamme})")

    if points >= SEUIL_MACHINE:
        # SO-DIMM prime : un mini PC reste hors périmètre même s'il cite un GPU.
        return ("machine_sodimm" if m_sodimm else "machine_udimm"), points, indices
    if points == SEUIL_MACHINE - 1:
        return "incertain", points, indices
    return "barrette", points, indices


def _accorder_specs_au_pn(specs, ref):
    """Aligne la configuration lue dans le texte sur celle du part number.

    Trois cas :
      • le texte annonce la capacité totale de la référence → c'est le kit
        complet, on adopte sa configuration (2×16 et non 1×32) ;
      • le texte annonce une fraction de cette capacité → le vendeur ne vend
        qu'une partie du kit, on garde la capacité par barrette de la référence
        et on recalcule le nombre de barrettes ;
      • le texte ne dit rien → on adopte la référence.
    """
    out = dict(specs)
    ref_module = ref["capacite_module_go"]
    ref_total = ref["capacite_totale_go"]
    texte_total = specs.get("capacite_totale_go")

    # Vente explicitement à l'unité : le PN donne la capacité par barrette, pas
    # le nombre de barrettes vendues.
    if specs.get("vente_unitaire"):
        out["capacite_module_go"] = ref_module
        out["nb_modules"] = 1
        out["capacite_totale_go"] = ref_module
        out["est_kit"] = False
        if not out.get("frequence_mhz"):
            out["frequence_mhz"] = ref["frequence_mhz"]
        if not out.get("cas_latency"):
            out["cas_latency"] = ref["cas_latency"]
        return out

    if texte_total and texte_total < ref_total and texte_total % ref_module == 0:
        out["capacite_module_go"] = ref_module
        out["nb_modules"] = texte_total // ref_module
        out["capacite_totale_go"] = texte_total
        out["est_kit"] = out["nb_modules"] > 1
        return out

    # Au-delà (« 64Go » avec un PN de kit 32 Go), c'est presque toujours une
    # lecture de texte gonflée — le mot « kit » qui double le compte, une
    # capacité de disque dur dans la description. Un part number ne peut pas
    # désigner plus que ce qu'il est : on s'aligne sur lui. Sous-estimer fait
    # rater une affaire, surestimer fait acheter trop cher.
    if texte_total and texte_total > ref_total and not specs.get("nb_modules_infere"):
        out["incoherence_capacite"] = f"{texte_total} Go annoncés vs {ref_total} Go " \
                                      f"pour {ref['part_number']}"

    out["capacite_module_go"] = ref_module
    out["nb_modules"] = ref["nb_modules"]
    out["capacite_totale_go"] = ref_total
    out["est_kit"] = ref["nb_modules"] > 1
    out.setdefault("frequence_mhz", None)
    if not out.get("frequence_mhz"):
        out["frequence_mhz"] = ref["frequence_mhz"]
    if not out.get("cas_latency"):
        out["cas_latency"] = ref["cas_latency"]
    return out


def detecter_exclusions(texte, texte_norm, specs, cfg=None):
    """Retourne (motif_exclusion, explication) ou (None, None).
    Ordre volontaire : les exclusions physiques d'abord (SO-DIMM, ECC), puis
    la génération, puis la capacité — pour que le message d'erreur soit le
    plus informatif possible."""
    cfg = cfg or ram_config.get()
    perim = cfg.section("perimetre")
    excl = perim.get("exclusions", {}) or {}

    # ── Ordinateur complet plutôt qu'une barrette ──
    type_produit = specs.get("type_produit")
    if type_produit == "machine_sodimm":
        return "machine", ("ordinateur portable ou compact (mémoire SO-DIMM) : "
                           + (specs.get("indices_produit") or ["indices multiples"])[0])
    if type_produit == "machine_udimm" and not perim.get("accepter_pc_complets", False):
        return "machine", ("ordinateur de bureau complet, pas une barrette : "
                           + (specs.get("indices_produit") or ["indices multiples"])[0])

    # ── SO-DIMM / portable ──
    for terme in excl.get("sodimm", []):
        if contient_terme(texte_norm, terme):
            return "sodimm", f"SO-DIMM/portable : « {terme} »"
    for motif in SODIMM_MOTIFS:
        if motif.search(texte):
            return "sodimm", f"part number SO-DIMM ({motif.search(texte).group(0)})"

    # ── ECC / registered / serveur ──
    for terme in excl.get("ecc", []):
        if contient_terme(texte_norm, terme):
            return "ecc", f"ECC/serveur : « {terme} »"
    for nom, motif in ECC_MOTIFS:
        if motif.search(texte):
            return "ecc", f"part number ECC ({nom} : {motif.search(texte).group(0)})"
    if RANK_X4.search(texte_norm):
        return "ecc", "organisation x4 : registered/serveur uniquement"

    # ── Autres générations ──
    for terme in excl.get("generation", []):
        if contient_terme(texte_norm, terme):
            gen = "ddr5" if "5" in terme else "ddr3"
            return gen, f"génération hors périmètre : « {terme} »"

    # ── DDR3 déguisée en DDR4 : le piège n°1 sur Vinted ──
    # Ces signaux sont des PRÉSOMPTIONS, pas des preuves. « Vengeance Pro »
    # désigne une gamme DDR3 (CMY) mais c'est aussi ainsi que beaucoup de
    # vendeurs écrivent « Vengeance RGB Pro », qui est de la DDR4. Rejeter sur
    # ce seul mot fait perdre de vraies affaires : une annonce qui écrit
    # noir sur blanc « DDR4 » mérite d'être signalée, pas éliminée.
    pieges = perim.get("pieges_ddr3", {}) or {}
    mode = str(pieges.get("mode", "degrader")).lower()
    ddr4_explicite = bool(re.search(r"\bddr\s*4\b", texte_norm))

    suspicions = []
    freq = specs.get("frequence_mhz")
    freq_min = int(perim.get("frequence_min", 2133))
    if freq and freq < freq_min:
        suspicions.append((f"fréquence {freq} MHz < {freq_min} : DDR3 très probable", 2))
    for terme in pieges.get("plateformes", []):
        if contient_terme(texte_norm, terme):
            suspicions.append((f"plateforme DDR3 mentionnée : « {terme} »", 2))
            break
    for terme in pieges.get("modeles", []):
        if contient_terme(texte_norm, terme):
            suspicions.append((f"gamme aussi connue en DDR3 : « {terme} »", 1))
            break

    if suspicions:
        specs["suspicions_ddr3"] = [s for s, _ in suspicions]
        gravite = max(poids for _, poids in suspicions)
        # Un seul indice faible + « DDR4 » écrit dans l'annonce : on garde et on
        # signale. Sinon (indice fort, ou aucune mention de DDR4) : on rejette.
        rejeter = (mode == "rejeter") or not ddr4_explicite or gravite >= 2
        if rejeter:
            return "ddr3_suspecte", suspicions[0][0]

    # ── Capacité ──
    cap = specs.get("capacite_module_go")
    autorisees = perim.get("capacites_autorisees", [8, 16, 32])
    if cap is not None and cap not in autorisees:
        if cap == 4:
            exc = perim.get("exception_4go", {}) or {}
            nb = specs.get("nb_modules") or 1
            if exc.get("actif") and nb >= int(exc.get("nb_min", 10)):
                return None, None      # lot 4 Go : le contrôle de prix se fait au scoring
            return "capacite", "barrette 4 Go (hors lot de 10+ à bas prix)"
        return "capacite", f"capacité {cap} Go hors périmètre"

    return None, None


def qualite_annonce(titre, description, nb_photos, specs, pn):
    """Note 0-100 : à quel point l'annonce est renseignée.

    Notée en RATIO sur les signaux réellement observables, pas en points
    absolus. L'API catalogue de Vinted ne renvoie ni la description complète ni
    la galerie photo : compter ces absences comme des zéros pénaliserait toutes
    les annonces Vinted de la même façon, et cette note (15 % du pré-score)
    deviendrait un handicap constant plutôt qu'un discriminant.
    """
    obtenus = 0.0
    possibles = 0.0

    # Signaux toujours lisibles depuis le titre
    possibles += 30
    if pn:
        obtenus += 30                # part number annoncé : le signal le plus fort
    possibles += 12
    if specs.get("frequence_mhz"):
        obtenus += 12
    possibles += 8
    if specs.get("cas_latency"):
        obtenus += 8
    possibles += 10
    if specs.get("capacite_module_go") and specs.get("nb_modules"):
        obtenus += 10

    # Photos : comptées seulement si la source nous en a transmis au moins une.
    if nb_photos:
        possibles += 20
        obtenus += 20 if nb_photos >= 3 else 15

    # Description : idem, seulement si on l'a réellement récupérée.
    longueur = len(description or "")
    if longueur:
        possibles += 12
        if longueur >= 300:
            obtenus += 12
        elif longueur >= 120:
            obtenus += 8
        else:
            obtenus += 4

    # Preuve de test : toujours évaluable, un titre suffit à la mentionner.
    possibles += 8
    texte_norm = normaliser(f"{titre} {description}")
    if any(contient_terme(texte_norm, t) for t in MEMTEST):
        obtenus += 8
    elif any(contient_terme(texte_norm, t) for t in TESTE):
        obtenus += 4

    return round(min(obtenus / possibles * 100, 100.0), 1) if possibles else 0.0


def analyser(titre, description="", nb_photos=0, cfg=None):
    """Analyse complète d'une annonce. Retourne un dict prêt pour le scoring.

    Ne lève jamais : une annonce mal formée doit ressortir en 'non identifiée',
    pas faire tomber le worker de scraping.
    """
    cfg = cfg or ram_config.get()
    texte = f"{titre or ''}\n{description or ''}"
    texte_norm = normaliser(texte)

    pn, pn_marque = extraire_part_number(texte)
    specs = extraire_specs(texte, texte_norm)
    marque = detecter_marque(texte_norm, pn_marque)
    gamme = detecter_gamme(texte_norm)

    # Est-ce seulement une annonce de barrette ? Ce classement précède tout le
    # reste : inutile d'estimer la valeur de revente d'un mini PC.
    type_produit, points_produit, indices_produit = detecter_type_produit(
        titre, texte_norm, pn, marque, gamme)
    specs["type_produit"] = type_produit
    specs["indices_produit"] = indices_produit

    exclusion, motif = detecter_exclusions(texte, texte_norm, specs, cfg)

    # Doit-on même regarder cette annonce ?
    # « DDR4 » explicite, ou un part number reconnu, sont les cas confortables.
    # Mais la majorité des titres Vinted disent seulement « Barrette RAM 16Go »
    # ou « RAM PC gamer 8go » : exiger la mention DDR4 reviendrait à ignorer le
    # gros du gisement. On les retient donc, avec un drapeau explicite et une
    # confiance basse — c'est le prix affiché qui décidera si ça vaut un coup
    # d'œil, et le drapeau dit quoi vérifier sur les photos.
    mentionne_ddr4 = bool(re.search(r"\bddr\s*4\b", texte_norm)) or bool(pn)
    # « Corsair Vengeance LPX 16Go » ne contient ni « DDR4 » ni « RAM » : une
    # marque ET une gamme mémoire reconnues valent signal, sinon on passerait à
    # côté d'une bonne part des annonces.
    mentionne_ram = (bool(re.search(r"\b(ram|memoire|barrette|dimm|udimm)\b", texte_norm))
                     or bool(marque and gamme))
    generation_incertaine = (not mentionne_ddr4 and mentionne_ram
                             and bool(specs.get("capacite_module_go")))

    ref = None
    if pn:
        ref = ram_db.find_reference_by_pn(pn)
        if ref is None:
            ram_db.signaler_pn_inconnu(pn, marque=marque, titre=titre)
        else:
            # Le part number détermine la configuration exacte : il prime sur ce
            # que dit le titre. « RAM 32Go F4-3600C16D-32GTZN » doit se lire
            # 2×16 et pas 1×32 — sans cette correction, la valeur de revente est
            # calculée sur une barrette de 32 Go inexistante et l'affaire est
            # rejetée à tort.
            specs = _accorder_specs_au_pn(specs, ref)

    # Repli par caractéristiques : sert à estimer une valeur, jamais à
    # affirmer une identité (deux kits aux mêmes specs ne sont pas le même kit).
    # Le relâchement est progressif — voir ram_db._NIVEAUX_APPROCHE — et devient
    # conservateur (référence la moins chère) dès que la fréquence manque.
    ref_approchee = confiance_approche = niveau_approche = None
    if ref is None:
        ref_approchee, confiance_approche, niveau_approche = \
            ram_db.find_reference_approchante(
                capacite_module=specs.get("capacite_module_go"),
                nb_modules=specs.get("nb_modules"),
                frequence=specs.get("frequence_mhz"),
                cas_latency=specs.get("cas_latency"),
                marque=marque, gamme=gamme)

    # Confiance de l'identification textuelle : c'est elle qui décide si on
    # peut annoncer un part number précis dans la notification.
    if ref:
        confiance = 0.90
    elif ref_approchee:
        confiance = confiance_approche or 0.20
    elif mentionne_ddr4:
        confiance = 0.15
    else:
        confiance = 0.0

    # Génération non confirmée : on garde l'annonce, mais la confiance chute.
    # Sans couche vision, c'est l'utilisateur qui devra trancher sur les photos.
    if generation_incertaine:
        confiance = min(confiance, 0.30)

    ref_effective = ref or ref_approchee
    drapeaux = []
    if generation_incertaine:
        drapeaux.append("génération non précisée dans l'annonce — vérifier l'encoche "
                        "sur la photo (DDR3 ou DDR4 ?)")
    if ref_approchee and not specs.get("frequence_mhz"):
        drapeaux.append("fréquence non annoncée — estimation prudente au prix plancher")
    if specs.get("incoherence_capacite"):
        drapeaux.append(f"capacité incohérente : {specs['incoherence_capacite']}")
    for suspicion in specs.get("suspicions_ddr3", []):
        drapeaux.append(f"⚠️ {suspicion} — vérifier l'encoche sur la photo")
    if marque in NO_NAME:
        drapeaux.append(f"marque no-name ({marque}) : revente difficile, XMP souvent instable")
    if any(contient_terme(texte_norm, t) for t in HS_TERMES):
        drapeaux.append("annonce mentionnant HS / en panne / pour pièces")
    if any(contient_terme(texte_norm, t) for t in DISSIPATEUR_KO):
        drapeaux.append("dissipateur manquant ou abîmé (−25 %)")
    if nb_photos == 0:
        drapeaux.append("aucune photo")
    if specs.get("frequence_min_citee") and specs.get("frequence_mhz") and \
            specs["frequence_min_citee"] < 2133 <= specs["frequence_mhz"]:
        drapeaux.append("fréquences DDR3 et DDR4 citées ensemble : vérifier à l'image")

    return {
        "pn_detecte": pn,
        "pn_normalise": ram_db.normalize_pn(pn) if pn else None,
        "marque_detectee": marque,
        "gamme_detectee": gamme or (ref_effective or {}).get("gamme"),
        "capacite_module_go": specs.get("capacite_module_go"),
        "nb_modules": specs.get("nb_modules"),
        "capacite_totale_go": specs.get("capacite_totale_go"),
        "frequence_mhz": specs.get("frequence_mhz"),
        "cas_latency": specs.get("cas_latency"),
        "rank": specs.get("rank"),
        "est_kit": specs.get("est_kit", False),
        "suspicions_ddr3": specs.get("suspicions_ddr3", []),
        "type_produit": type_produit,
        "points_produit": points_produit,
        "indices_produit": indices_produit,
        "ref": ref,
        "ref_approchee": ref_approchee,
        "niveau_approche": niveau_approche,
        "generation_incertaine": generation_incertaine,
        "ref_id": (ref_effective or {}).get("id"),
        "tier": (ref_effective or {}).get("tier"),
        "exclusion": exclusion,
        "rejet_motif": motif,
        "pertinent": bool(mentionne_ddr4 or generation_incertaine
                          or (mentionne_ram and specs.get("frequence_mhz"))),
        "confiance_texte": round(confiance, 2),
        "qualite_annonce": qualite_annonce(titre, description, nb_photos, specs, pn),
        "drapeaux": drapeaux,
        "no_name": marque in NO_NAME,
        "rgb": any(contient_terme(texte_norm, t) for t in RGB_TERMES),
        "blanc": any(contient_terme(texte_norm, t) for t in BLANC_TERMES),
        "main_propre": any(t in texte_norm for t in MAIN_PROPRE),
        "sans_boite": any(t in texte_norm for t in SANS_BOITE),
        "dissipateur_manquant": any(contient_terme(texte_norm, t) for t in DISSIPATEUR_KO),
        "memtest_prouve": any(contient_terme(texte_norm, t) for t in MEMTEST),
        "hs": any(contient_terme(texte_norm, t) for t in HS_TERMES),
        "texte_norm": texte_norm,
    }


if __name__ == "__main__":
    exemples = [
        ("Ram ddr4 32go corsair", "Kit 2x16 3200mhz CMK32GX4M2E3200C16, testé, sous garantie", 3),
        ("DDR4 8GB sodimm portable", "pour pc portable", 1),
        ("Kingston KVR32E22D8/16 16Go", "serveur", 1),
        ("Corsair Vengeance 16Go DDR4", "2x8 1600mhz pour i7-4790 LGA1150", 2),
        ("G.Skill Trident Z 3200 CL14 F4-3200C14D-16GTZ", "B-die, 2x8, testé memtest 8 passes", 5),
        ("Barrettes DDR4 4Go x12", "lot de 12 barrettes 4go 2400", 1),
        ("RAM DDR5 32go", "2x16 6000", 2),
        ("Crucial CT16G4DFD832A 16Go 3200", "barrette nue", 1),
    ]
    for titre, desc, photos in exemples:
        r = analyser(titre, desc, photos)
        verdict = f"❌ {r['exclusion']} — {r['rejet_motif']}" if r["exclusion"] else "✅ retenue"
        print(f"\n« {titre} »")
        print(f"   {verdict}")
        print(f"   PN={r['pn_detecte']} marque={r['marque_detectee']} "
              f"config={r['nb_modules']}×{r['capacite_module_go']} "
              f"{r['frequence_mhz']}C{r['cas_latency']} tier={r['tier']} "
              f"confiance={r['confiance_texte']} qualité={r['qualite_annonce']}")
        for d in r["drapeaux"]:
            print(f"   ⚠️  {d}")
