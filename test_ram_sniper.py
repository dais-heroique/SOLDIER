"""
test_ram_sniper.py — Tests du module RAM SNIPER
═══════════════════════════════════════════════════════════════════════════
Aucune dépendance réseau : tout tourne hors ligne, sur une base temporaire.

    python3 test_ram_sniper.py

Les tests portent en priorité sur ce qui coûte de l'argent quand c'est faux :
les exclusions (acheter une SO-DIMM ou de la DDR3), le calcul du prix total
d'acquisition (surestimer la marge), et le double comptage des multiplicateurs
(payer trop cher parce qu'on croit revendre plus haut).
"""

import os
import sys
import tempfile
import time
import traceback

# Base temporaire AVANT tout import du module (ram_db lit DB_FILE à l'import).
_TMP = tempfile.mkdtemp(prefix="ram_sniper_test_")
os.environ["RAM_DRY_RUN"] = "1"
os.environ["SNIPER_ETAT_SORTIES"] = os.path.join(_TMP, "sorties.json")
for _var in ("SNIPER_PROXY_URL", "SNIPER_PROXY_URLS", "SNIPER_PROXY_MAISON",
             "SNIPER_PROXY_WARP", "SNIPER_DIRECT", "SNIPER_ORDRE_SORTIES",
             "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_var, None)

import ram_db                                                     # noqa: E402
ram_db.DB_FILE = os.path.join(_TMP, "test.db")

import ram_config                                                 # noqa: E402
import ram_listing                                                # noqa: E402
import ram_pairing                                                # noqa: E402
import ram_parser                                                 # noqa: E402
import ram_reference_data                                         # noqa: E402
import ram_scoring                                                # noqa: E402
import ram_scrapers                                               # noqa: E402
import ram_telegram                                               # noqa: E402
import ram_vision                                                 # noqa: E402

RESULTATS = {"ok": 0, "ko": 0, "erreurs": []}


def test(nom):
    def decorateur(fn):
        try:
            fn()
            RESULTATS["ok"] += 1
            print(f"  ✅ {nom}")
        except AssertionError as e:
            RESULTATS["ko"] += 1
            RESULTATS["erreurs"].append((nom, str(e)))
            print(f"  ❌ {nom} — {e}")
        except Exception as e:
            RESULTATS["ko"] += 1
            RESULTATS["erreurs"].append((nom, traceback.format_exc()))
            print(f"  💥 {nom} — {type(e).__name__}: {e}")
        return fn
    return decorateur


def section(titre):
    print(f"\n── {titre} ──")


# ═══════════════════════════════════════════════════════════
section("Base de référence")

ram_db.init_db()
ram_db.seed_references(verbose=False)


@test("113+ références chargées, tous tiers représentés")
def _():
    refs = ram_db.list_references(limit=1000)
    assert len(refs) >= 60, f"{len(refs)} références, minimum 60 attendu"
    tiers = {r["tier"] for r in refs}
    assert tiers == {"S", "A", "B", "C", "D"}, f"tiers manquants : {tiers}"


@test("cohérence des données de référence")
def _():
    erreurs = ram_reference_data.verifier_coherence()
    assert not erreurs, f"{len(erreurs)} incohérence(s) : {erreurs[:3]}"


@test("le schéma refuse une SO-DIMM dans la base de référence")
def _():
    import sqlite3
    with ram_db.get_db() as conn:
        try:
            conn.execute("""INSERT INTO ram_reference
                (part_number, pn_normalise, marque, gamme, form_factor,
                 capacite_module_go, nb_modules, capacite_totale_go, frequence_mhz,
                 tier, prix_ref_occasion_eur, liquidite, cree_le, maj_le)
                VALUES ('TEST-SODIMM','TESTSODIMM','X','Y','SODIMM',8,1,8,3200,
                        'C',20,3,0,0)""")
            raise AssertionError("l'insertion d'une SO-DIMM aurait dû être refusée")
        except sqlite3.IntegrityError:
            pass


@test("le schéma refuse une capacité totale incohérente")
def _():
    import sqlite3
    with ram_db.get_db() as conn:
        try:
            conn.execute("""INSERT INTO ram_reference
                (part_number, pn_normalise, marque, gamme,
                 capacite_module_go, nb_modules, capacite_totale_go, frequence_mhz,
                 tier, prix_ref_occasion_eur, liquidite, cree_le, maj_le)
                VALUES ('TEST-INCO','TESTINCO','X','Y',16,2,99,3200,'A',100,3,0,0)""")
            raise AssertionError("capacité totale incohérente acceptée")
        except sqlite3.IntegrityError:
            pass


@test("le re-seed préserve un prix recalibré")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    ram_db.maj_prix_reference(ref["id"], 999.0, "vinted_vendu", 12)
    ram_db.seed_references(verbose=False)
    apres = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    assert apres["prix_ref_occasion_eur"] == 999.0, "un prix calibré a été écrasé par le seed"
    ram_db.maj_prix_reference(ref["id"], 115.0, "seed", 0)
    with ram_db.get_db() as conn:
        conn.execute("UPDATE ram_reference SET prix_ref_source='seed' WHERE id=?", (ref["id"],))


@test("recherche par part number : exact, normalisé et tronqué")
def _():
    assert ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    assert ram_db.find_reference_by_pn("cmk32gx4m2e3200c16")
    assert ram_db.find_reference_by_pn("F4-3200C14D-16GTZ")
    assert ram_db.find_reference_by_pn("F4 3200C14D 16GTZ"), "normalisation des espaces"
    assert ram_db.find_reference_by_pn("XXXX-INEXISTANT-999") is None


# ═══════════════════════════════════════════════════════════
section("Exclusions de périmètre")


def analyse(titre, desc="", photos=1):
    return ram_parser.analyser(titre, desc, photos)


@test("SO-DIMM rejetée (mot-clé et part number)")
def _():
    for t in ["DDR4 8Go SODIMM", "RAM DDR4 pour pc portable", "Mémoire 16Go notebook",
              "Samsung M471A2K43CB1-CTD 16Go"]:
        r = analyse(t)
        assert r["exclusion"] == "sodimm", f"« {t} » non rejetée ({r['exclusion']})"


@test("ECC / registered rejetées, y compris par part number positionnel")
def _():
    for t, attendu in [("DDR4 16Go ECC serveur", "ecc"),
                       ("Kingston KVR32E22D8/16", "ecc"),
                       ("Crucial CT16G4WFD8266", "ecc"),
                       ("SK Hynix HMA82GU7DJR8N-XN", "ecc"),
                       ("Samsung M391A2K43BB1-CTD 16Go", "ecc"),
                       ("RDIMM 2Rx4 16Go", "ecc")]:
        r = analyse(t)
        assert r["exclusion"] == attendu, f"« {t} » → {r['exclusion']} au lieu de {attendu}"


@test("le 'E' d'un PN Corsair n'est PAS de l'ECC (piège classique)")
def _():
    # CMK32GX4M2E3200C16 contient un E : une règle en sous-chaîne rejetterait
    # la référence la plus liquide du marché.
    r = analyse("Corsair CMK32GX4M2E3200C16 32Go 3200")
    assert r["exclusion"] is None, f"faux positif ECC : {r['rejet_motif']}"
    assert r["pn_detecte"] == "CMK32GX4M2E3200C16"


@test("Samsung M378 (non-ECC) accepté malgré le motif A2K43")
def _():
    r = analyse("Samsung M378A2K43CB1-CTD 16Go DDR4 2666")
    assert r["exclusion"] is None, f"faux positif : {r['rejet_motif']}"


@test("DDR3 / DDR5 rejetées")
def _():
    assert analyse("Kit DDR3 16Go 1600")["exclusion"] == "ddr3"
    assert analyse("RAM DDR5 32Go 6000")["exclusion"] == "ddr5"


@test("DDR3 déguisée : les indices FORTS rejettent")
def _():
    # Fréquence sous 2133 et plateforme DDR3 citée sont des preuves quasi
    # certaines : on rejette même si l'annonce écrit « DDR4 ».
    for t in ["DDR4 16Go 1600MHz",
              "RAM DDR4 8Go pour i7-4790 LGA1150",
              "HyperX Fury 1600 8Go DDR4",
              "Corsair Vengeance Pro 16Go 1866mhz z87"]:
        r = analyse(t)
        assert r["exclusion"] == "ddr3_suspecte", f"« {t} » non détectée ({r['exclusion']})"


@test("DDR3 déguisée : un nom de gamme seul dégrade, il ne rejette pas")
def _():
    # « Vengeance Pro » désigne une gamme DDR3 (CMY), mais c'est aussi ainsi
    # que beaucoup écrivent « Vengeance RGB Pro », qui est de la DDR4. Rejeter
    # sur ce seul mot faisait perdre de vraies affaires.
    r = analyse("Ram Corsair vengeance PRO 16g",
                "Ram DDR4 2x8g Corsair vengeance Pro 16giga")
    assert r["exclusion"] != "ddr3_suspecte", \
        "annonce disant explicitement DDR4 rejetée sur un nom de gamme"
    assert r["suspicions_ddr3"], "la présomption doit rester signalée"
    assert any("vengeance pro" in d.lower() for d in r["drapeaux"])

    # Sans mention DDR4, le doute profite à la prudence.
    sans_ddr4 = analyse("Corsair Vengeance Pro 16Go")
    assert sans_ddr4["exclusion"] == "ddr3_suspecte"


@test("unités de capacité : 2x8g, 16giga, 16g")
def _():
    # « DDR4 2x8g » n'était pas reconnu comme un kit : l'annonce se faisait
    # valoriser comme une barrette unique de 16 Go, soit le double de sa valeur.
    r = analyse("Ram Corsair vengeance PRO 16g",
                "Ram DDR4 2x8g Corsair vengeance Pro 16giga")
    assert (r["nb_modules"], r["capacite_module_go"]) == (2, 8), \
        f"lu {r['nb_modules']}×{r['capacite_module_go']} au lieu de 2×8"
    assert analyse("Barrette DDR4 16giga 3200")["capacite_module_go"] == 16
    assert analyse("RAM DDR4 8g 2666")["capacite_module_go"] == 8


@test("4 Go rejetée, sauf lot de 10+")
def _():
    assert analyse("Barrette DDR4 4Go 2400")["exclusion"] == "capacite"
    r = analyse("Lot 12 barrettes DDR4 4Go", "destockage 2400mhz")
    assert r["exclusion"] is None, "le lot de 12 aurait dû passer l'exception"
    assert r["nb_modules"] == 12


@test("ordinateurs complets rejetés : mini PC, portable, tour, carte mère")
def _():
    # C'était la principale source de bruit : une liste de phrases exactes ne
    # reconnaît ni « PC Mini ITX », ni « ThinkCentre Tiny », ni « Laptops ».
    machines = [
        "Mini PC Ryzen 5 5600G 16Go DDR4 256Go SSD",
        "PC Mini ITX multimedia Ryzen 5 5600G DDR4",
        "Lenovo ThinkCentre M720q Tiny",
        "Laptops",
        "HP EliteBook 840 G3, 2K IPS Display",
        "HP 250 G8 Intel Core i5 10th Gen | 8GB RAM | 256GB SSD",
        "Pc gaming / 8Go DDR4/256Go SSD Tour PC 4K UHD",
        "Unite centrale gamer RTX 3060 16Go DDR4",
        "Carte mere B450 + Ryzen 5 3600 + 16Go DDR4",
        "Ordinateur de bureau Dell Optiplex 390 - i3 / 4Go RAM",
    ]
    for titre in machines:
        r = analyse(titre)
        assert r["exclusion"] == "machine", \
            f"« {titre} » → {r['exclusion']} (type {r['type_produit']}, " \
            f"{r['points_produit']} pts)"


@test("les vraies annonces de barrettes ne sont pas emportées")
def _():
    # Faux positif coûteux à éviter : « ram pc gamer » est un mot-clé de
    # recherche, et « sortie de mon pc » une formulation courante.
    barrettes = [
        ("RAM DDR4 16Go pour PC gamer", "corsair vengeance"),
        ("Barrette RAM DDR4 8Go", ""),
        ("Kit RAM DDR4 32Go Corsair Vengeance LPX 3200", "CMK32GX4M2E3200C16"),
        ("Memoire vive DDR4 32go", ""),
        ("Corsair Vengeance LPX 16Go", "sortie de mon pc gamer"),
        ("DDR4 16Go 3200 pour carte mere B550", "ripjaws v"),
        ("G.Skill Trident Z Neo 32Go 3600 CL16", "F4-3600C16D-32GTZN"),
        ("2x8 Go DDR4 3200 HyperX Fury", "upgrade de mon pc"),
    ]
    for titre, desc in barrettes:
        r = analyse(titre, desc)
        assert r["exclusion"] != "machine", \
            f"faux positif sur « {titre} » ({r['points_produit']} pts) : " \
            f"{r['indices_produit']}"


@test("les PC de bureau peuvent être acceptés sur demande, jamais les SO-DIMM")
def _():
    # accepter_pc_complets: true sert au gisement « PC en panne » de Leboncoin.
    # Mais un mini PC ou un portable reste hors périmètre quoi qu'il arrive :
    # leur mémoire est de la SO-DIMM.
    class CfgPermissive:
        def val(self, chemin, defaut=None):
            if chemin == "perimetre":
                base = dict(ram_config.get().val("perimetre", {}))
                base["accepter_pc_complets"] = True
                return base
            return ram_config.get().val(chemin, defaut)
        def section(self, nom):
            return self.val(nom, {}) or {}

    cfg = CfgPermissive()
    tour = ram_parser.analyser("Unite centrale gamer RTX 3060 16Go DDR4", "", 1, cfg)
    assert tour["exclusion"] != "machine", "le PC de bureau aurait dû être accepté"

    mini = ram_parser.analyser("Mini PC Ryzen 5 5600G 16Go DDR4 256Go SSD", "", 1, cfg)
    assert mini["exclusion"] == "machine", \
        "un mini PC (SO-DIMM) doit rester rejeté même en mode permissif"


@test("annonce hors sujet ignorée sans planter")
def _():
    r = analyse("Chaussures Nike taille 42", "très bon état")
    assert not r["pertinent"]


# ═══════════════════════════════════════════════════════════
section("Extraction des caractéristiques")


@test("configuration kit : 2x16, 2 x 16 Go, 64 Go déduit")
def _():
    assert analyse("Kit 2x16 Go DDR4 3200")["nb_modules"] == 2
    assert analyse("DDR4 2 x 16go 3600")["capacite_module_go"] == 16
    r = analyse("DDR4 64Go 3200")
    assert (r["capacite_module_go"], r["nb_modules"]) == (32, 2), \
        "64 Go annoncé seul devrait se lire comme 2×32"


@test("fréquence et CAS extraits, XMP prioritaire sur la fréquence de base")
def _():
    r = analyse("DDR4 16Go 2133 par défaut, 3200MHz en XMP CL16")
    assert r["frequence_mhz"] == 3200, "la fréquence XMP doit primer"
    assert r["cas_latency"] == 16


@test("timings complets 16-18-18-38 lus comme CL16")
def _():
    assert analyse("DDR4 3600 16-18-18-38")["cas_latency"] == 16


@test("part numbers reconnus pour chaque constructeur")
def _():
    attendus = {
        "CMK32GX4M2E3200C16": "Corsair", "F4-3200C14D-16GTZ": "G.Skill",
        "KF432C16BB1K2/32": "Kingston", "HX432C16FB3K2/16": "HyperX",
        "BL2K16G36C16U4B": "Crucial", "CT16G4DFD832A": "Crucial",
        "PVS432G360C8K": "Patriot", "HMA82GU6DJR8N-XN": "SK Hynix",
        "M378A2G43AB3-CWE": "Samsung", "MTA16ATF2G64AZ-3G2": "Micron",
    }
    for pn, marque in attendus.items():
        lu, m = ram_parser.extraire_part_number(f"Vends RAM DDR4 {pn} bon état")
        assert lu, f"{pn} non détecté"
        assert ram_db.normalize_pn(lu) == ram_db.normalize_pn(pn), f"{pn} → {lu}"
        assert m == marque, f"{pn} attribué à {m} au lieu de {marque}"


@test("annonce sans fréquence : identifiée quand même, au prix plancher")
def _():
    # La majorité des titres Vinted n'annoncent aucune fréquence. Exiger un
    # MHz revenait à rejeter tout le gisement.
    r = analyse("Barrette RAM DDR4 16Go")
    assert r["exclusion"] is None, f"rejetée : {r['rejet_motif']}"
    assert r["ref_approchee"], "aucune référence approchante trouvée"
    assert any("fréquence non annoncée" in d for d in r["drapeaux"])


@test("estimation sans fréquence : prudente, jamais optimiste")
def _():
    sans = analyse("RAM DDR4 16Go Corsair")
    avec = analyse("RAM DDR4 16Go 3600 Corsair")
    p_sans = sans["ref_approchee"]["prix_ref_occasion_eur"] / sans["ref_approchee"]["nb_modules"]
    p_avec = avec["ref_approchee"]["prix_ref_occasion_eur"] / avec["ref_approchee"]["nb_modules"]
    assert p_sans <= p_avec, \
        "sans fréquence, l'estimation doit retomber sur la référence la moins chère"


@test("marque + gamme sans le mot RAM ni DDR4 : reconnue")
def _():
    r = analyse("Corsair Vengeance LPX 16Go", "sortie de mon pc")
    assert r["pertinent"], "une marque et une gamme mémoire connues suffisent"
    assert r["generation_incertaine"]
    assert any("génération non précisée" in d for d in r["drapeaux"])
    assert r["confiance_texte"] <= 0.30, "génération non confirmée = confiance basse"


@test("le part number prime sur la capacité du titre")
def _():
    # « 32Go » + PN d'un kit 2×16 : lire 1×32 diviserait la valeur par deux et
    # ferait rejeter l'affaire à tort.
    r = analyse("RAM DDR4 32Go 3600 CL16", "F4-3600C16D-32GTZN")
    assert (r["nb_modules"], r["capacite_module_go"]) == (2, 16), \
        f"lu {r['nb_modules']}×{r['capacite_module_go']} au lieu de 2×16"


@test("une barrette vendue seule n'est pas valorisée comme un kit")
def _():
    r = analyse("RAM 16Go Corsair", "CMK32GX4M2E3200C16 une seule barrette du kit")
    assert (r["nb_modules"], r["capacite_module_go"]) == (1, 16), \
        f"lu {r['nb_modules']}×{r['capacite_module_go']} : le kit entier serait valorisé"
    assert not r["est_kit"]


@test("capacité au-delà du part number : on s'aligne sur le PN")
def _():
    r = analyse("RAM DDR4 64Go", "CMK32GX4M2E3200C16")
    assert r["capacite_totale_go"] == 32, "surestimer la capacité = payer trop cher"
    assert any("incohérente" in d for d in r["drapeaux"])


@test("« Kit 16 Go » se lit 2×8, jamais 2×16")
def _():
    # Le mot « kit » avec une capacité annoncée seule désigne un TOTAL.
    # Le lire comme une capacité par barrette double la valeur estimée et fait
    # payer un kit de 16 Go au prix d'un kit de 32 Go.
    r = analyse("Kit RAM Lexar THOR White 16 Go DDR4 3600 MHz")
    assert (r["nb_modules"], r["capacite_module_go"]) == (2, 8), \
        f"lu {r['nb_modules']}×{r['capacite_module_go']} au lieu de 2×8"
    assert r["capacite_totale_go"] == 16

    # La notation explicite « 2x16 » reste, elle, une capacité par barrette.
    r2 = analyse("Kit RAM 2x16 Go DDR4 3600")
    assert (r2["nb_modules"], r2["capacite_module_go"]) == (2, 16)


@test("marque inconnue : estimation conservatrice, jamais au prix d'une grande marque")
def _():
    inconnue = analyse("Kit RAM Lexar THOR White 16 Go DDR4 3600 MHz")
    connue = analyse("Kit RAM Corsair Vengeance 16 Go DDR4 3600 MHz")
    v_inc = inconnue["ref_approchee"]["prix_ref_occasion_eur"]
    v_con = connue["ref_approchee"]["prix_ref_occasion_eur"]
    assert v_inc <= v_con, \
        f"une marque secondaire ({v_inc}€) ne peut pas valoir plus qu'un Corsair ({v_con}€)"


@test("qualité d'annonce : pas de pénalité pour une description que l'API ne fournit pas")
def _():
    # L'API catalogue de Vinted ne renvoie pas la description. La noter 0
    # pénaliserait toutes les annonces Vinted à l'identique.
    sans_desc = ram_parser.qualite_annonce("RAM DDR4 3200 16Go", "", 1,
                                           {"frequence_mhz": 3200,
                                            "capacite_module_go": 16, "nb_modules": 1}, None)
    assert sans_desc >= 40, f"qualité {sans_desc} : la description absente est sur-pénalisée"


@test("prix plancher disponible pour chaque capacité du périmètre")
def _():
    for capacite in (8, 16, 32):
        plancher = ram_db.prix_plancher(capacite)
        assert plancher and plancher > 0, f"pas de plancher pour {capacite} Go"
    assert ram_db.prix_plancher(8) < ram_db.prix_plancher(32), \
        "le plancher doit croître avec la capacité"


@test("marque no-name signalée")
def _():
    r = analyse("Barrette DDR4 16Go 3200 Qiyida")
    assert r["no_name"], "Qiyida devrait être signalée comme no-name"
    assert any("no-name" in d for d in r["drapeaux"])


# ═══════════════════════════════════════════════════════════
section("Frais et scoring")


@test("prix total Vinted = affiché + port + 5% + 0,70€")
def _():
    port, protection, total = ram_scoring.frais_acquisition("vinted", 45.0, port_connu=4.0)
    assert port == 4.0
    assert abs(protection - 2.95) < 0.01, f"protection {protection} au lieu de 2,95"
    assert abs(total - 51.95) < 0.01, f"total {total} au lieu de 51,95"


@test("main propre Leboncoin : zéro frais")
def _():
    port, protection, total = ram_scoring.frais_acquisition(
        "leboncoin", 60.0, main_propre=True)
    assert (port, protection, total) == (0.0, 0.0, 60.0)


@test("pas de double comptage des multiplicateurs sur un PN exact")
def _():
    # Le prix de référence d'un kit 2×16 RGB low profile inclut DÉJÀ le kit,
    # le RGB et le low profile. Les recompter gonflerait la revente de 40-60 %.
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    a = {"nb_modules": 2, "est_kit": True, "rgb": False, "blanc": False,
         "memtest_prouve": False, "sans_boite": False,
         "dissipateur_manquant": False, "no_name": False, "rank": "2Rx8"}
    valeur, details = ram_scoring.valeur_revente(ref, a)
    base = ref["prix_ref_occasion_eur"]
    assert abs(valeur - (base - 1.20)) < 0.01, \
        f"revente {valeur} au lieu de {base - 1.20} (multiplicateurs comptés en double : {details})"


@test("barrette dépareillée : prorata avec décote")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    valeur, _ = ram_scoring.valeur_revente(ref, {"nb_modules": 1, "est_kit": False})
    attendu = ref["prix_ref_occasion_eur"] / 2 * 0.88 - 1.20
    assert abs(valeur - attendu) < 0.5, f"{valeur} au lieu de {attendu}"


@test("dissipateur manquant : −25 %")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    base, _ = ram_scoring.valeur_revente(ref, {"nb_modules": 2, "est_kit": True})
    abime, _ = ram_scoring.valeur_revente(
        ref, {"nb_modules": 2, "est_kit": True, "dissipateur_manquant": True})
    assert abime < base * 0.80, f"décote insuffisante : {abime} vs {base}"


@test("marge minimum : rejet seulement si < 20€ ET < 45%")
def _():
    assert not ram_scoring.marge_suffisante(10, 30), "10€/30% aurait dû être rejeté"
    assert ram_scoring.marge_suffisante(10, 60), "10€/60% est acceptable"
    assert ram_scoring.marge_suffisante(50, 20), "50€/20% est acceptable"
    assert not ram_scoring.marge_suffisante(None, None)


@test("pré-score de l'exemple du cahier des charges (~115€ revente, ~62€ marge)")
def _():
    annonce = {"source": "vinted", "prix_affiche": 45.0, "frais_port": 4.0,
               "frais_protection": 2.95, "prix_total": 51.95,
               "vendeur_note": 4.9, "vendeur_ventes": 127}
    a = analyse("Ram ddr4 32go corsair",
                "Kit 2x16 3200mhz CMK32GX4M2E3200C16 bon état", 3)
    pre = ram_scoring.pre_score(annonce, a)
    assert pre["exclusion"] is None, pre["rejet_motif"]
    assert 105 <= pre["revente_estimee"] <= 125, f"revente {pre['revente_estimee']}"
    assert 55 <= pre["marge_estimee"] <= 70, f"marge {pre['marge_estimee']}"
    assert pre["pre_score"] >= 65, f"pré-score {pre['pre_score']} sous le seuil de notification"


@test("score final rejette une DDR3 vue à l'image malgré un texte propre")
def _():
    annonce = {"source": "vinted", "prix_affiche": 45.0, "prix_total": 51.95}
    a = analyse("Corsair CMK32GX4M2E3200C16 32Go", "kit 2x16 3200", 3)
    vision = {"generation_suspectee": "DDR3", "photo_lisible": True,
              "confiance": 0.9, "statut": "ok"}
    fin = ram_scoring.score_final(annonce, a, vision)
    assert fin["statut_verif"] == "rejete"
    assert "DDR3" in fin["rejet_motif"]


@test("score final : photo illisible → à vérifier, jamais confirmé")
def _():
    annonce = {"source": "vinted", "prix_affiche": 45.0, "prix_total": 51.95}
    a = analyse("Corsair CMK32GX4M2E3200C16 32Go", "kit 2x16 3200", 3)
    fin = ram_scoring.score_final(annonce, a, {"photo_lisible": False, "confiance": 0.3,
                                               "statut": "ok"})
    assert fin["statut_verif"] == "a_verifier"


@test("score final : 9 puces = ECC → rejet")
def _():
    annonce = {"source": "vinted", "prix_affiche": 45.0, "prix_total": 51.95}
    a = analyse("Corsair CMK32GX4M2E3200C16 32Go", "kit 2x16 3200", 3)
    fin = ram_scoring.score_final(annonce, a, {"est_ecc": True, "photo_lisible": True,
                                               "confiance": 0.9, "statut": "ok"})
    assert fin["statut_verif"] == "rejete" and "ECC" in fin["rejet_motif"]


# ═══════════════════════════════════════════════════════════
section("Couche vision")


@test("parsing défensif : backticks, préambule, virgule traînante, types laxistes")
def _():
    cas = [
        '{"confiance": 0.9}',
        '```json\n{"confiance": 0.9}\n```',
        'Voici le JSON :\n{"confiance": 0.9,}',
        '{"confiance": "0.9"}',
    ]
    for brut in cas:
        data, err = ram_vision.parser_reponse(brut)
        assert data["confiance"] == 0.9, f"{brut!r} → {data['confiance']}"


@test("parsing défensif : réponse cassée ne lève jamais")
def _():
    for brut in ["", None, "pas de json", "{cassé", "[]", "null"]:
        data, err = ram_vision.parser_reponse(brut)
        assert isinstance(data, dict), f"{brut!r} n'a pas rendu un dict"
        assert data["confiance"] is None or isinstance(data["confiance"], float)


@test("schéma vision complet et confiance bornée à [0,1]")
def _():
    data, _ = ram_vision.parser_reponse('{"confiance": 5.0, "est_ecc": true}')
    assert data["confiance"] == 1.0, "confiance non bornée"
    for cle in ("est_sodimm", "part_number_lu", "nb_puces_par_face", "drapeaux"):
        assert cle in data, f"champ {cle} absent du schéma normalisé"


@test("sélection des photos : la première + les 2 plus grandes")
def _():
    urls = ["https://x/a_100x100.jpg", "https://x/b_800x600.jpg",
            "https://x/c_1600x1200.jpg", "https://x/d_400x300.jpg"]
    choix = ram_vision.choisir_photos(urls, 3)
    assert len(choix) == 3
    assert choix[0] == urls[0], "la photo vitrine doit toujours être envoyée"
    assert urls[2] in choix, "la plus grande photo doit être retenue"


@test("quota : compteurs persistés, blocage puis fenêtre suivante")
def _():
    ok, _ = ram_db.quota_disponible(3, 100, "test")
    assert ok
    for _ in range(3):
        ram_db.consommer_quota("test", 3, 100)
    ok, detail = ram_db.quota_disponible(3, 100, "test")
    assert not ok and "minute" in detail["motif"]
    ok, detail = ram_db.quota_disponible(10, 100, "test")
    assert ok, "un plafond plus large devrait redébloquer"


@test("cache vision : clé stable, insensible à l'ordre des photos")
def _():
    a = ram_db.cache_key("https://x/1", ["p1.jpg", "p2.jpg"])
    b = ram_db.cache_key("https://x/1", ["p2.jpg", "p1.jpg"])
    c = ram_db.cache_key("https://x/1", ["p1.jpg", "p3.jpg"])
    assert a == b, "l'ordre des photos ne doit pas changer la clé"
    assert a != c, "un jeu de photos différent doit changer la clé"


@test("file vision : priorité au meilleur score, pas de double prise")
def _():
    ids = []
    for score, prix in ((60, 40), (88, 50), (72, 45)):
        aid, _ = ram_db.upsert_annonce({
            "source": "vinted", "url": f"https://vinted.fr/file/{score}",
            "titre": f"test {score}", "prix_affiche": prix, "photos": ["p.jpg"]})
        ram_db.enfiler_vision(aid, score)
        ids.append(aid)
    premiere = ram_db.prochaine_annonce_vision()
    assert premiere["priorite"] == 88, f"priorité {premiere['priorite']} au lieu de 88"
    deuxieme = ram_db.prochaine_annonce_vision()
    assert deuxieme["annonce_id"] != premiere["annonce_id"], "annonce prise deux fois"
    ram_db.cloturer_vision(premiere["file_id"], "fait")
    ram_db.cloturer_vision(deuxieme["file_id"], "fait")


@test("quota épuisé : annonce différée puis reprise, jamais perdue")
def _():
    aid, _ = ram_db.upsert_annonce({
        "source": "vinted", "url": "https://vinted.fr/differe/1",
        "titre": "test différé", "prix_affiche": 50, "photos": ["p.jpg"]})
    ram_db.enfiler_vision(aid, 70)
    tache = ram_db.prochaine_annonce_vision()
    ram_db.differer_vision(tache["file_id"])
    assert ram_db.etat_file_vision().get("differe", 0) >= 1
    reprises = ram_db.reprendre_differees()
    assert reprises >= 1, "les différées doivent repasser en file"


# ═══════════════════════════════════════════════════════════
section("Appariement de kits")


@test("prix cible calculé pour garder la marge minimale")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M1E3200C16")
    stock = {"prix_revient": 86.60}
    cible, revente_kit = ram_pairing.prix_cible(stock, ref)
    assert revente_kit is not None
    assert abs(cible - (revente_kit - 86.60 - 30)) < 0.01


@test("revente d'un kit s'appuie sur un vrai kit de référence, pas sur une extrapolation")
def _():
    ref_unitaire = ram_db.find_reference_by_pn("CMK32GX4M1E3200C16")
    kit = ram_pairing.revente_du_kit(ref_unitaire)
    extrapolation = ref_unitaire["prix_ref_occasion_eur"] * 2 * 1.25
    assert kit < extrapolation, \
        f"extrapolation utilisée ({kit}) alors qu'un kit 2×32 existe en base"


@test("appariement détecté au bon prix, ignoré au-dessus de la cible")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M1E3200C16")
    stock_id = ram_db.creer_stock({
        "part_number": "CMK32GX4M1E3200C16", "ref_id": ref["id"],
        "capacite_module_go": 32, "frequence_mhz": 3200, "cas_latency": 16,
        "code_semaine": "2134", "prix_achat": 78.0, "frais_port": 4.0,
        "frais_protection": 4.60, "statut": "teste_ok"})

    def candidat(prix, semaine, suffixe):
        aid, _ = ram_db.upsert_annonce({
            "source": "vinted", "url": f"https://vinted.fr/appar/{suffixe}",
            "titre": "Corsair CMK32GX4M1E3200C16 32Go",
            "prix_affiche": prix, "frais_port": 4.0,
            "frais_protection": round(prix * 0.05 + 0.7, 2),
            "pn_detecte": "CMK32GX4M1E3200C16",
            "pn_normalise": ram_db.normalize_pn("CMK32GX4M1E3200C16"),
            "capacite_module_go": 32, "nb_modules": 1, "photos": ["p.jpg"]})
        annonce = ram_db.get_annonce(aid)
        annonce["code_semaine"] = semaine
        return ram_pairing.chercher_appariements(annonce)

    bons = candidat(72.0, "2134", "a")
    assert bons and bons[0]["type_appariement"] == "parfait", \
        "même PN + même semaine = kit parfait"
    autres = candidat(72.0, "2210", "b")
    assert autres and autres[0]["type_appariement"] == "batch_different"
    trop_cher = candidat(200.0, "2134", "c")
    assert not trop_cher, "une annonce au-dessus du prix cible ne doit pas remonter"
    return stock_id


@test("kit assorti : 'parfait' exige un code semaine sur TOUTES les barrettes")
def _():
    ref = ram_db.find_reference_by_pn("F4-3200C16S-16GVK")
    a = ram_db.creer_stock({"part_number": "F4-3200C16S-16GVK", "ref_id": ref["id"],
                            "capacite_module_go": 16, "prix_achat": 30.0,
                            "code_semaine": "2201", "statut": "teste_ok"})
    b = ram_db.creer_stock({"part_number": "F4-3200C16S-16GVK", "ref_id": ref["id"],
                            "capacite_module_go": 16, "prix_achat": 32.0,
                            "statut": "teste_ok"})
    resultat = ram_pairing.assembler_kit([a, b])
    assert resultat["qualite"] == "batch_different", \
        "sans code semaine sur les deux, le kit ne peut pas être déclaré parfait"

    c = ram_db.creer_stock({"part_number": "F4-3200C16S-16GVK", "ref_id": ref["id"],
                            "capacite_module_go": 16, "prix_achat": 30.0,
                            "code_semaine": "2201", "statut": "teste_ok"})
    d = ram_db.creer_stock({"part_number": "F4-3200C16S-16GVK", "ref_id": ref["id"],
                            "capacite_module_go": 16, "prix_achat": 31.0,
                            "code_semaine": "2201", "statut": "teste_ok"})
    assert ram_pairing.assembler_kit([c, d])["qualite"] == "parfait"


@test("assembler deux PN différents ne donne jamais un kit assorti")
def _():
    r1 = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    r2 = ram_db.find_reference_by_pn("F4-3200C16D-32GVK")
    a = ram_db.creer_stock({"part_number": r1["part_number"], "ref_id": r1["id"],
                            "capacite_module_go": 16, "prix_achat": 50.0,
                            "statut": "teste_ok"})
    b = ram_db.creer_stock({"part_number": r2["part_number"], "ref_id": r2["id"],
                            "capacite_module_go": 16, "prix_achat": 50.0,
                            "statut": "teste_ok"})
    resultat = ram_pairing.assembler_kit([a, b])
    assert resultat["qualite"] == "heterogene"
    assert not resultat["vendable_comme_kit_assorti"], \
        "deux PN différents vendus comme kit assorti = annonce mensongère"


# ═══════════════════════════════════════════════════════════
section("Notifications")


@test("message étape 1 : non vérifié, prix total détaillé")
def _():
    annonce = {"id": 1, "titre": "Ram ddr4 32go corsair", "prix_affiche": 45.0,
               "frais_port": 4.0, "frais_protection": 2.95, "prix_total": 51.95,
               "nb_modules": 2, "capacite_module_go": 16, "frequence_mhz": 3200}
    m = ram_telegram._sans_html(ram_telegram.message_etape1(
        annonce, {"pre_score": 72, "revente_estimee": 110, "marge_estimee": 58}))
    assert "NON VÉRIFIÉ" in m and "72" in m
    # Le prix affiché ET le prix réellement payé doivent apparaître : c'est la
    # différence entre les deux qui fait rater ou saisir une affaire.
    assert "51.95€" in m and "45€" in m and "2.95€" in m


@test("avec Gemini opérationnel : le message annonce bien l'analyse")
def _():
    annonce = {"id": 1, "titre": "DDR4 32Go", "prix_affiche": 45.0, "prix_total": 51.95}

    class CfgAvecVision:
        def val(self, chemin, defaut=None):
            if chemin == "vision.actif":
                return True
            if chemin == "vision.provider":
                return "gemini"
            return ram_config.get().val(chemin, defaut)

    ancien = ram_config.secret
    ram_config.secret = lambda nom, defaut=None: "cle-de-test"
    try:
        m = ram_telegram._sans_html(ram_telegram.message_etape1(
            annonce, {"pre_score": 72}, CfgAvecVision()))
        assert "Analyse image en cours" in m
    finally:
        ram_config.secret = ancien


@test("message étape 2 : les quatre états produisent le bon en-tête")
def _():
    annonce = {"id": 1, "titre": "test", "prix_affiche": 45.0, "prix_total": 51.95}
    attendus = {"confirme": "CONFIRMÉ", "probable": "PROBABLE",
                "a_verifier": "À VÉRIFIER", "rejete": "REJETÉ"}
    for statut, marqueur in attendus.items():
        fin = {"statut_verif": statut, "score_final": 87, "rejet_motif": "motif test",
               "drapeaux": [], "details_revente": []}
        m = ram_telegram._sans_html(ram_telegram.message_etape2(annonce, fin, {}))
        assert marqueur in m, f"{statut} → en-tête incorrect"


@test("sans Gemini : le message n'annonce PAS une analyse qui n'arrivera jamais")
def _():
    import ram_setup                                              # noqa: F401
    annonce = {"id": 1, "titre": "DDR4 32Go", "prix_affiche": 45.0, "prix_total": 51.95}
    pre = {"pre_score": 72, "revente_estimee": 115, "marge_estimee": 63}

    class CfgSansVision:
        """Config où la vision est éteinte."""
        def val(self, chemin, defaut=None):
            return False if chemin == "vision.actif" else ram_config.get().val(chemin, defaut)

    m = ram_telegram._sans_html(ram_telegram.message_etape1(annonce, pre, CfgSansVision()))
    assert "Analyse image en cours" not in m, \
        "un « analyse en cours » sans worker vision reste affiché pour toujours"
    assert "Vérifie les photos" in m, "le mode texte seul doit dire quoi faire à la main"


@test("sans clé Gemini, la vision est considérée non opérationnelle")
def _():
    import os as _os
    ancienne = _os.environ.pop("GEMINI_API_KEY", None)
    ram_config._env_cache = {}
    try:
        assert not ram_telegram.vision_operationnelle(ram_config.get()), \
            "vision annoncée opérationnelle sans clé API"
    finally:
        if ancienne:
            _os.environ["GEMINI_API_KEY"] = ancienne
        ram_config._env_cache = None


@test("anti-spam : une rafale passe, un flot est bridé")
def _():
    # Trois bonnes affaires publiées dans la même minute doivent TOUTES partir.
    # Avec un simple délai de 60 s, seule la première passait et les autres
    # arrivaient trop tard sur un marché où une affaire tient quelques minutes.
    with ram_db.get_db() as conn:
        conn.execute("DELETE FROM ram_notification")
    cfg = ram_config.get()
    rafale = int(cfg.val("telegram.rafale_max", 4))
    envoyees = 0
    for _ in range(rafale + 3):
        if ram_telegram.anti_spam_ok(cfg):
            ram_db.enregistrer_notification({"chat_id": "test", "type": "annonce",
                                             "etat": "non_verifie", "mode": "edit"})
            envoyees += 1
    assert envoyees == rafale, \
        f"{envoyees} notifications parties au lieu de {rafale} (rafale mal bornée)"
    with ram_db.get_db() as conn:
        conn.execute("DELETE FROM ram_notification")


@test("âge de publication affiché dans la notification")
def _():
    import time as _t
    maintenant = _t.time()
    assert "min" in ram_telegram.age_lisible(maintenant - 600)
    assert "h" in ram_telegram.age_lisible(maintenant - 7200)
    assert "j" in ram_telegram.age_lisible(maintenant - 3 * 86400)
    assert ram_telegram.age_lisible(None) is None

    annonce = {"id": 1, "titre": "test", "prix_affiche": 45.0, "prix_total": 51.95,
               "publie_le": maintenant - 300}
    m = ram_telegram._sans_html(ram_telegram.message_etape1(annonce, {"pre_score": 70}))
    assert "Publiée" in m and "min" in m

    vieille = dict(annonce, publie_le=maintenant - 10 * 86400)
    m2 = ram_telegram._sans_html(ram_telegram.message_etape1(vieille, {"pre_score": 70}))
    assert "En ligne depuis 10 j" in m2, m2
    assert "depuis il y a" not in m2, "formulation en double"


@test("fraîcheur : une annonce récente monte, une vieille descend")
def _():
    import time as _t
    maintenant = _t.time()
    base = {"source": "vinted", "prix_affiche": 45.0, "prix_total": 51.95}
    a = analyse("Corsair Vengeance 32Go", "CMK32GX4M2E3200C16", 2)

    recente = ram_scoring.pre_score(dict(base, publie_le=maintenant - 300), a)
    vieille = ram_scoring.pre_score(dict(base, publie_le=maintenant - 10 * 86400), a)
    sans_date = ram_scoring.pre_score(dict(base), a)

    assert recente["ajustement_fraicheur"] > 0
    assert vieille["ajustement_fraicheur"] < 0
    assert sans_date["ajustement_fraicheur"] == 0, \
        "sans date de publication, aucun ajustement ne doit être inventé"
    assert vieille["pre_score"] < recente["pre_score"]
    assert any("semaine" in d for d in vieille.get("drapeaux", [])), \
        "une annonce ancienne doit être signalée comme telle"


@test("état 'à vérifier' propose le message pré-rédigé au vendeur")
def _():
    annonce = {"id": 1, "titre": "test", "prix_affiche": 45.0, "prix_total": 51.95}
    m = ram_telegram._sans_html(ram_telegram.message_etape2(
        annonce, {"statut_verif": "a_verifier", "drapeaux": []}, {}))
    assert "photo du sticker" in m


@test("échappement HTML des titres d'annonce")
def _():
    annonce = {"id": 1, "titre": "RAM <b>32Go</b> & \"promo\"", "prix_affiche": 45.0,
               "prix_total": 51.95}
    brut = ram_telegram.message_etape1(annonce, {"pre_score": 70})
    assert "<b>32Go</b>" not in brut.split("« ")[1].split(" »")[0]
    assert "&amp;" in brut or "&lt;" in brut


@test("boutons adaptés à l'état")
def _():
    annonce = {"id": 1, "url": "https://vinted.fr/1"}
    textes = lambda st: [b["text"] for rang in
                         ram_telegram.boutons(annonce, statut_verif=st)["inline_keyboard"]
                         for b in rang]
    assert any("ACHETER" in t for t in textes("confirme"))
    assert any("VOIR" in t for t in textes(None))
    assert any("photo sticker" in t for t in textes("a_verifier"))
    assert any("Archiver" in t for t in textes("rejete"))


@test("« Ignorer » retire le message du chat")
def _():
    aid, _ = ram_db.upsert_annonce({
        "source": "vinted", "url": "https://vinted.fr/suppr/1",
        "titre": "Kit DDR4 32Go Corsair", "prix_affiche": 45, "photos": []})
    ram_db.enregistrer_notification({
        "annonce_id": aid, "chat_id": "123", "type": "annonce", "message_id": 4242,
        "etat": "non_verifie", "mode": "edit",
        "texte": "⚡ NON VÉRIFIÉ\n\n« Kit DDR4 32Go Corsair »\nPrix 45€"})

    appels = []
    ancien = ram_telegram._appel
    ram_telegram._appel = lambda m, c, **k: appels.append((m, c)) or {}

    class CfgReel:
        dry_run = False
        def val(self, chemin, defaut=None):
            return ram_config.get().val(chemin, defaut)
    try:
        ram_telegram.traiter_callback({"data": f"ignore:{aid}"}, CfgReel())
        assert appels and appels[0][0] == "deleteMessage", \
            f"aucune suppression demandée : {appels}"
        assert appels[0][1]["message_id"] == 4242
        assert ram_db.notification_de_annonce(aid)["supprime_le"], \
            "la suppression n'est pas tracée en base"
    finally:
        ram_telegram._appel = ancien


@test("message trop vieux pour être supprimé : réduit à une ligne barrée")
def _():
    # Telegram interdit à un bot de supprimer ses messages au-delà de 48 h.
    # Laisser la fiche complète noierait les affaires suivantes.
    aid, _ = ram_db.upsert_annonce({
        "source": "vinted", "url": "https://vinted.fr/suppr/2",
        "titre": "RAM DDR4 16Go HyperX", "prix_affiche": 30, "photos": []})
    ram_db.enregistrer_notification({
        "annonce_id": aid, "chat_id": "123", "type": "annonce", "message_id": 4243,
        "etat": "non_verifie", "mode": "edit",
        "texte": "⚡ NON VÉRIFIÉ\n\n« RAM DDR4 16Go HyperX »\nPrix 30€"})

    appels = []
    ancien = ram_telegram._appel

    def refuse_suppression(methode, charge, **k):
        if methode == "deleteMessage":
            raise ram_telegram.TelegramError("message can't be deleted")
        appels.append((methode, charge))
        return {}

    ram_telegram._appel = refuse_suppression

    class CfgReel:
        dry_run = False
        def val(self, chemin, defaut=None):
            return ram_config.get().val(chemin, defaut)
    try:
        ram_telegram.traiter_callback({"data": f"ignore:{aid}"}, CfgReel())
        assert appels and appels[0][0] == "editMessageText", \
            f"aucun repli après refus de suppression : {appels}"
        assert "<s>" in appels[0][1]["text"], "le repli doit barrer le titre"
        assert "HyperX" in appels[0][1]["text"]
    finally:
        ram_telegram._appel = ancien


@test("callbacks : ignorer, archiver, demander photo")
def _():
    aid, _ = ram_db.upsert_annonce({
        "source": "vinted", "url": "https://vinted.fr/cb/1", "titre": "test callback",
        "prix_affiche": 50, "photos": []})
    assert "ignorée" in ram_telegram.traiter_callback({"data": f"ignore:{aid}"})
    assert ram_db.get_annonce(aid)["statut"] == "ignore"
    assert "Archivée" in ram_telegram.traiter_callback({"data": f"archive:{aid}"})
    assert "photo du sticker" in ram_telegram.traiter_callback({"data": f"photo:{aid}"})
    assert ram_telegram.traiter_callback({"data": "n'importe quoi"}) == "action inconnue"


# ═══════════════════════════════════════════════════════════
section("Pipeline et génération d'annonces")


@test("pipeline complet : brut → base → pré-score")
def _():
    brut = {"url": "https://vinted.fr/pipeline/1",
            "titre": "Kit DDR4 32Go Corsair Vengeance LPX 3200",
            "description": "CMK32GX4M2E3200C16 2x16Go testé", "prix": 62.0,
            "photos": ["https://x/a.jpg"], "vendeur_note": 4.8, "vendeur_ventes": 64}
    r = ram_scrapers.traiter_annonce(brut, "vinted", "ddr4 32go")
    assert r and r["nouvelle"]
    assert r["annonce"]["prix_total"] > 62.0, "les frais Vinted doivent être ajoutés"
    assert r["pre"]["pre_score"] > 0
    encore = ram_scrapers.traiter_annonce(brut, "vinted", "ddr4 32go")
    assert not encore["nouvelle"], "une annonce déjà vue ne doit pas être recréée"


@test("une annonce déjà notifiée n'est pas re-scorée à chaque scan")
def _():
    brut = {"url": "https://vinted.fr/pipeline/2", "titre": "DDR4 32Go 3200 Corsair",
            "description": "CMK32GX4M2E3200C16", "prix": 55.0, "photos": ["a.jpg"]}
    r1 = ram_scrapers.traiter_annonce(brut, "vinted")
    ram_db.maj_annonce(r1["annonce"]["id"], {"statut_verif": "confirme",
                                             "score_final": 88})
    ram_scrapers.traiter_annonce(brut, "vinted")
    apres = ram_db.get_annonce(r1["annonce"]["id"])
    assert apres["statut_verif"] == "confirme", "un re-scan a écrasé un verdict acquis"
    assert apres["score_final"] == 88


@test("générateur : titre orienté recherche, PN en description")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    infos = {"part_number": ref["part_number"], "marque": ref["marque"],
             "gamme": ref["gamme"], "capacite_module_go": 16, "nb_modules": 2,
             "capacite_totale_go": 32, "frequence_mhz": 3200, "cas_latency": 16,
             "rank": "2Rx8", "couleur": "noir", "low_profile": 1, "memtest_ok": 1,
             "memtest_passes": 8, "xmp_stable": 1, "kit_assorti": True,
             "prix_revient": 51.95, "ref": ref}
    sortie = ram_listing.generer(infos=infos)
    v = sortie["versions"]["vinted"]
    assert v["titre"].startswith("32Go"), f"titre : {v['titre']}"
    assert "DDR4" in v["titre"] and "3200" in v["titre"]
    assert ref["part_number"] in v["description"]
    assert "MemTest86" in v["description"]
    assert "SO-DIMM" in v["description"], "préciser que ce n'est pas de la SO-DIMM"
    lbc = sortie["versions"]["leboncoin"]
    assert len(lbc["titre"]) <= 50, f"titre Leboncoin trop long ({len(lbc['titre'])})"
    assert v["prix"] > lbc["prix"], "le prix Vinted doit être plus haut (frais acheteur)"


@test("argument Ryzen FCLK 1:1 généré automatiquement en 3600")
def _():
    ref = ram_db.find_reference_by_pn("CMK32GX4M2D3600C18")
    infos = {"part_number": ref["part_number"], "marque": ref["marque"],
             "gamme": ref["gamme"], "capacite_module_go": 16, "nb_modules": 2,
             "capacite_totale_go": 32, "frequence_mhz": 3600, "cas_latency": 18,
             "ref": ref, "prix_revient": 70}
    d = ram_listing.generer(infos=infos)["versions"]["vinted"]["description"]
    assert "FCLK" in d and "1:1" in d


# ═══════════════════════════════════════════════════════════
section("Configuration et P&L")


@test("config : valeurs par défaut si une clé manque")
def _():
    cfg = ram_config.get()
    seuil = cfg.val("scoring.seuil_notification")
    assert isinstance(seuil, (int, float)) and 30 <= seuil <= 90, f"seuil aberrant : {seuil}"
    assert cfg.val("scoring.seuil_vision") <= seuil, \
        "le seuil d'analyse doit être sous celui de notification"
    assert cfg.val("cle.qui.nexiste.pas", "défaut") == "défaut"
    assert cfg.notif_mode in ("edit", "second_message")


@test("surcharge locale : ram_config.yaml n'est jamais modifié")
def _():
    import ram_setup
    # Écrire dans le fichier versionné ferait échouer chaque « git pull » avec
    # « your local changes would be overwritten by merge » — l'utilisateur reste
    # bloqué sur une vieille version sans comprendre pourquoi.
    avant = open(ram_config.CONFIG_FILE, encoding="utf-8").read()
    existant = os.path.exists(ram_config.LOCAL_FILE)
    sauvegarde = open(ram_config.LOCAL_FILE, encoding="utf-8").read() if existant else None
    try:
        assert ram_setup.basculer_vision(False)
        apres = open(ram_config.CONFIG_FILE, encoding="utf-8").read()
        assert avant == apres, "ram_config.yaml a été modifié : les git pull vont casser"
        assert ram_config.get(force=True).val("vision.actif") is False, \
            "la surcharge locale n'est pas prise en compte"
        assert ram_config.get(force=True).val("scoring.seuil_notification"), \
            "la surcharge locale a effacé le reste de la configuration"
    finally:
        if sauvegarde is not None:
            open(ram_config.LOCAL_FILE, "w", encoding="utf-8").write(sauvegarde)
        elif os.path.exists(ram_config.LOCAL_FILE):
            os.remove(ram_config.LOCAL_FILE)
        ram_config.get(force=True)


@test("quota vision : marge de sécurité appliquée")
def _():
    cfg = ram_config.get()
    minute, jour = cfg.quota_vision()
    assert minute < cfg.val("vision.quota.par_minute"), "marge de sécurité non appliquée"
    assert jour < cfg.val("vision.quota.par_jour")


@test("KPI : capital dormant, rotation, alerte au-delà de 40%")
def _():
    k = ram_db.kpis()
    for cle in ("capital_engage", "capital_dormant", "part_dormant_pct",
                "rotation_moyenne_jours", "alerte_dormant"):
        assert cle in k, f"KPI {cle} absent"
    assert k["capital_engage"] > 0, "le stock de test devrait apparaître"


@test("délai de rotation calculé automatiquement à la vente")
def _():
    import time as _t
    sid = ram_db.creer_stock({"part_number": "TEST-ROTATION", "capacite_module_go": 16,
                              "prix_achat": 40.0, "statut": "recu",
                              "achete_le": _t.time() - 5 * 86400})
    ram_db.maj_stock(sid, {"statut": "vendu", "prix_vente": 70.0, "marge_nette": 30.0})
    with ram_db.get_db() as conn:
        ligne = conn.execute("SELECT delai_rotation_jours FROM ram_stock WHERE id=?",
                             (sid,)).fetchone()
    assert 4.5 <= ligne["delai_rotation_jours"] <= 5.5, \
        f"rotation {ligne['delai_rotation_jours']} au lieu de ~5 jours"


@test("PN inconnu enregistré comme candidat à qualifier")
def _():
    ram_db.signaler_pn_inconnu("ZZZZ99GX4M2E9999C99", marque="Inconnue", prix=40)
    ram_db.signaler_pn_inconnu("ZZZZ99GX4M2E9999C99", marque="Inconnue", prix=55)
    candidats = [c for c in ram_db.list_pn_candidats()
                 if c["pn_normalise"] == "ZZZZ99GX4M2E9999C99"]
    assert candidats and candidats[0]["occurrences"] == 2


@test("un délai réseau Telegram ne s'échappe pas en TimeoutError")
def _():
    # urlopen(timeout=…) lève TimeoutError, qui n'est PAS un URLError. Non
    # capturé, il remontait jusqu'au worker de notification — qui n'attrape que
    # TelegramError — et tuait le thread : une seule alerte, puis plus rien,
    # pendant que le scan continuait de tourner normalement.
    import urllib.request
    vrai = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out"))
    try:
        try:
            ram_telegram._appel("sendMessage", {"chat_id": "x", "text": "y"},
                                token="1234567890:" + "a" * 35)
            raise AssertionError("aucune exception levée")
        except ram_telegram.TelegramError:
            pass
        except TimeoutError:
            raise AssertionError("TimeoutError s'échappe encore et tuera le worker")
    finally:
        urllib.request.urlopen = vrai


@test("un worker qui plante redémarre au lieu de mourir")
def _():
    import threading as _th
    import time as _t
    import ram_sniper
    appels = {"n": 0}

    def worker_capricieux(_cfg):
        appels["n"] += 1
        if appels["n"] == 1:
            raise RuntimeError("panne simulée")
        _t.sleep(0.05)

    ram_sniper.ARRET.clear()
    t = _th.Thread(target=ram_sniper._superviser,
                   args=("test_sante", worker_capricieux, (None,)), daemon=True)
    t.start()
    _t.sleep(0.3)
    ram_sniper.ARRET.set()
    t.join(timeout=3)
    ram_sniper.ARRET.clear()
    assert appels["n"] >= 1
    sante = ram_sniper.SANTE.get("test_sante", {})
    assert sante.get("redemarrages", 0) >= 1, "le plantage n'a pas été enregistré"


@test("file de notification reconstruite depuis la base")
def _():
    import ram_sniper
    import time as _t
    cfg = ram_config.get()
    ram_sniper._file_notif.clear()
    with ram_db.get_db() as conn:
        conn.execute("DELETE FROM ram_notification")

    for i, prix in enumerate((45, 40, 50)):
        ram_scrapers.traiter_annonce({
            "url": f"https://vinted.fr/durable/{i}",
            "titre": "Kit DDR4 32Go Corsair Vengeance LPX 3200",
            "description": "CMK32GX4M2E3200C16 2x16", "prix": prix,
            "photos": ["a.jpg"], "publie_le": _t.time() - 600}, "vinted", "ddr4")

    n = ram_sniper.recharger_file_notification(cfg)
    assert n >= 1, "une annonce notifiable non envoyée doit pouvoir être reprise"
    assert ram_sniper.recharger_file_notification(cfg) == 0, \
        "la reprise ne doit pas créer de doublons"

    # Une annonce déjà notifiée ne doit jamais repartir
    annonce_id = ram_sniper._file_notif[0][1]
    ram_db.enregistrer_notification({"annonce_id": annonce_id, "chat_id": "x",
                                     "type": "annonce", "etat": "non_verifie",
                                     "mode": "edit"})
    ram_sniper._file_notif.clear()
    reste = ram_sniper.recharger_file_notification(cfg)
    assert reste == n - 1, f"{reste} reprises au lieu de {n - 1}"
    ram_sniper._file_notif.clear()


@test("service : plist macOS et unité systemd valides")
def _():
    import xml.dom.minidom
    import ram_service

    plist = ram_service._plist_contenu()
    xml.dom.minidom.parseString(plist)          # lève si le XML est cassé
    for cle in ("KeepAlive", "RunAtLoad", "ThrottleInterval"):
        assert cle in plist, f"{cle} absent : le service ne redémarrerait pas"
    assert "caffeinate" in plist, "sans caffeinate, un Mac endormi ne scanne rien"

    unite = ram_service._unite_contenu()
    assert "Restart=always" in unite
    assert "ram_sniper.py" in unite


@test("élection de leader : une seule machine notifie")
def _():
    # Deux machines sur le même groupe Telegram. L'API n'autorise qu'un
    # getUpdates par bot : celle qui l'obtient notifie, l'autre se met en
    # retrait. Sans ça, chaque affaire arriverait en double dans le groupe.
    ancien = ram_telegram._appel
    ram_telegram._role.update({"valeur": None, "verifie_le": 0})
    cfg = ram_config.get()

    class CfgReel:
        """dry_run désactivé : sinon l'élection est court-circuitée."""
        dry_run = False
        def val(self, chemin, defaut=None):
            return cfg.val(chemin, defaut)

    try:
        ram_telegram._appel = lambda *a, **k: (_ for _ in ()).throw(
            ram_telegram.TelegramError("HTTP 409 : terminated by other getUpdates"))
        role, raison = ram_telegram.role_instance(CfgReel(), force=True)
        assert role == "secours", f"rôle {role} : la machine notifierait en double"

        ram_telegram._appel = lambda *a, **k: []
        role, _ = ram_telegram.role_instance(CfgReel(), force=True)
        assert role == "principal", "la reprise après arrêt de l'autre machine échoue"

        # Panne réseau : on ne se tait PAS, mieux vaut un doublon qu'un silence.
        ram_telegram._appel = lambda *a, **k: (_ for _ in ()).throw(
            ram_telegram.TelegramError("réseau : injoignable"))
        role, _ = ram_telegram.role_instance(CfgReel(), force=True)
        assert role == "principal", "une panne réseau ne doit pas faire taire le bot"
    finally:
        ram_telegram._appel = ancien
        ram_telegram._role.update({"valeur": None, "verifie_le": 0})


@test("service : l'interpréteur du venv est trouvé sur les trois systèmes")
def _():
    import ram_service
    py = ram_service.python_venv()
    assert py and os.path.exists(py), f"interpréteur introuvable : {py}"
    # Le .bat Windows doit relancer le bot en boucle, comme KeepAlive sur macOS.
    bat = ram_service._bat_contenu()
    assert "goto boucle" in bat, "le script Windows ne relancerait pas le bot"
    assert "ram_sniper.py" in bat


@test("service : prérequis vérifiés avant installation")
def _():
    import ram_service
    problemes = ram_service.verifier_prerequis()
    # Sans .env, l'installation doit être refusée plutôt que de créer un
    # service qui redémarrerait en boucle sans jamais rien envoyer.
    bloquants = [p for p in problemes if not p.startswith("ℹ️")]
    assert isinstance(problemes, list)
    if not os.path.exists(os.path.join(ram_service.BASE_DIR, ".env")):
        assert any(".env" in p for p in bloquants), \
            "un .env manquant doit bloquer l'installation"


@test("une option inconnue est refusée, jamais ignorée en silence")
def _():
    import ram_sniper
    # Sur une version pas à jour, « --dashboard » était ignoré et le bot complet
    # démarrait : le terminal affichait un scan là où on attendait une page web.
    assert ram_sniper._valider_options(["--dry-run", "--once"])
    assert ram_sniper._valider_options(["--diag", "--limite=50"])
    assert ram_sniper._valider_options(["--dashboard", "--port=8010"])
    assert not ram_sniper._valider_options(["--dashbord"], silencieux=True), \
        "faute de frappe acceptée"
    assert not ram_sniper._valider_options(["--inexistant"], silencieux=True)


@test("verrou d'instance : la seconde exécution est refusée")
def _():
    import ram_sniper
    premier = ram_sniper._verrou_instance()
    assert premier is not None, "le premier verrou doit être obtenu"
    try:
        assert ram_sniper._verrou_instance() is None, \
            "deux instances doubleraient les requêtes Vinted et se disputeraient Telegram"
    finally:
        premier.close()
    apres = ram_sniper._verrou_instance()
    assert apres is not None, "le verrou doit être libéré à la fermeture"
    apres.close()


@test("replay : rejoue sans planter et compte les rejets")
def _():
    stats = ram_scrapers.rejouer(200, verbose=False)
    assert stats["total"] > 0
    assert "par_exclusion" in stats


@test("recalibrage : médiane appliquée, variation plafonnée")
def _():
    import ram_calibration
    import time as _t
    ref = ram_db.find_reference_by_pn("F4-3600C16D-32GVKC")
    ancien = ref["prix_ref_occasion_eur"]
    for i, prix in enumerate((900, 950, 1000, 1100, 980)):   # aberrant exprès
        ram_db.enregistrer_observation({
            "ref_id": ref["id"], "part_number": ref["part_number"],
            "source": "vinted_vendu", "prix": prix, "nb_modules": 2,
            "url": f"https://vinted.fr/calib/{i}", "vendu_le": _t.time()})
    changements = ram_calibration.recalibrer(ref_id=ref["id"], verbose=False,
                                             appliquer=True)
    assert changements and changements[0]["plafonne"], \
        "une variation aberrante doit être plafonnée"
    apres = ram_db.get_reference(ref["id"])
    assert apres["prix_ref_occasion_eur"] <= ancien * 1.45, \
        f"le garde-fou n'a pas tenu : {ancien} → {apres['prix_ref_occasion_eur']}"


# ═══════════════════════════════════════════════════════════
section("Filtre vendeur : au moins un avis")

import vendeur_avis                                               # noqa: E402
import vinted_client                                              # noqa: E402
vendeur_avis.PAUSE_S = (0, 0)       # pas de pause réseau dans les tests


class _FauxVinted:
    def __init__(self, avis=None, erreur=None):
        self.avis, self.erreur, self.appels = avis, erreur, 0

    def get_user_feedback(self, vendeur_id):
        self.appels += 1
        if self.erreur:
            raise self.erreur
        return {"avis": self.avis, "note": 4.5, "pseudo": f"vendeur{vendeur_id}"}


def _cfg_filtre(**kw):
    return ram_config.Config({"filtre_vendeur": kw})


@test("vendeur à 0 avis : écarté, avec un motif lisible")
def _():
    vendeur_avis.vider_cache()
    v = vendeur_avis.evaluer("vinted", _FauxVinted(avis=0), 101)
    assert not v["ok"] and not v["transitoire"]
    assert "0 avis" in v["motif"], v["motif"]


@test("vendeur à 1 avis : accepté ; SNIPER_MIN_REVIEWS relève le seuil")
def _():
    vendeur_avis.vider_cache()
    assert vendeur_avis.evaluer("vinted", _FauxVinted(avis=1), 102)["ok"]
    os.environ["SNIPER_MIN_REVIEWS"] = "3"
    try:
        vendeur_avis.vider_cache()
        assert not vendeur_avis.evaluer("vinted", _FauxVinted(avis=2), 103)["ok"]
        assert vendeur_avis.evaluer("vinted", _FauxVinted(avis=3), 104)["ok"]
    finally:
        del os.environ["SNIPER_MIN_REVIEWS"]


@test("filtre désactivé (avis_min 0) : aucune requête de profil")
def _():
    vendeur_avis.vider_cache()
    faux = _FauxVinted(avis=0)
    v = vendeur_avis.evaluer("vinted", faux, 105, _cfg_filtre(avis_min=0))
    assert v["ok"] and faux.appels == 0


@test("profil illisible : rejeté par défaut, marqué transitoire ; « garder » le laisse passer")
def _():
    vendeur_avis.vider_cache()
    faux = _FauxVinted(erreur=vinted_client.VintedError("HTTP 403", status=403))
    v = vendeur_avis.evaluer("vinted", faux, 106)
    assert not v["ok"] and v["transitoire"]
    vendeur_avis.vider_cache()
    v2 = vendeur_avis.evaluer("vinted", faux, 107, _cfg_filtre(si_inconnu="garder"))
    assert v2["ok"] and v2["avis"] is None


@test("identifiant vendeur absent : traité comme inconnu, pas comme transitoire")
def _():
    v = vendeur_avis.evaluer("vinted", _FauxVinted(avis=9), None)
    assert not v["ok"] and not v["transitoire"]


@test("un vendeur n'est interrogé qu'une fois (cache) ; un échec n'est pas réessayé aussitôt")
def _():
    vendeur_avis.vider_cache()
    faux = _FauxVinted(avis=5)
    for _ in range(3):
        vendeur_avis.evaluer("vinted", faux, 108)
    assert faux.appels == 1, f"{faux.appels} requêtes pour un seul vendeur"
    vendeur_avis.vider_cache()
    ko = _FauxVinted(erreur=RuntimeError("réseau"))
    for _ in range(3):
        vendeur_avis.evaluer("vinted", ko, 109)
    assert ko.appels == 1, "un vendeur en échec ne doit pas être martelé"


@test("Vinted : le compteur d'avis est lu, ou reconstitué depuis les trois compteurs")
def _():
    f = vinted_client.VintedClient._parse_feedback
    assert f({"feedback_count": 12, "feedback_reputation": 0.9})["avis"] == 12
    assert f({"feedback_count": 0})["avis"] == 0
    assert f({"positive_feedback_count": 4, "neutral_feedback_count": 1,
              "negative_feedback_count": 1})["avis"] == 6
    assert f({"feedback_count": 3, "feedback_reputation": 1})["note"] == 5.0
    try:
        f({"login": "x"})
        assert False, "un profil sans compteur ne doit pas valoir « 0 avis »"
    except vinted_client.VintedError:
        pass


@test("Vinted : la normalisation expose l'identifiant du vendeur")
def _():
    vc = vinted_client.VintedClient.__new__(vinted_client.VintedClient)
    vc.currency = "EUR"
    n = vc._normalize({"id": 77, "title": "Kit DDR4", "price": {"amount": "30"},
                       "url": "https://www.vinted.fr/items/77",
                       "user": {"id": 555, "login": "marie"}})
    assert n["vendeur_id"] == 555 and n["vendeur_pseudo"] == "marie" and n["id"] == 77


@test("Vinted : get_user_feedback lit le profil (session simulée)")
def _():
    class _Rep:
        status_code = 200
        text = ""
        def json(self):
            return {"user": {"id": 9, "login": "paul", "feedback_count": 7,
                             "feedback_reputation": 0.98}}

    class _Session:
        def get(self, url, **kw):
            assert url.endswith("/api/v2/users/9"), url
            return _Rep()

    vc = vinted_client.VintedClient.__new__(vinted_client.VintedClient)
    vc.host, vc.verbose, vc.session = "https://www.vinted.fr", False, _Session()
    vc._ensure_session = lambda force=False: None
    info = vc.get_user_feedback(9)
    assert info["avis"] == 7 and info["pseudo"] == "paul"


@test("Leboncoin : avis lus sur le profil, repli sur la fiche boutique des pros")
def _():
    from types import SimpleNamespace as NS
    particulier = NS(name="Léa", feedback=NS(received_count=4, score=4.7), pro=None)
    zero = NS(name="Neuf", feedback=NS(received_count=0, score=None), pro=None)
    pro = NS(name="Boutique", feedback=NS(received_count=None, score=None),
             pro=NS(rating=NS(user_ratings_total=120)))

    class _Client:
        def get_user(self, uid):
            return {"a": particulier, "b": zero, "c": pro}[uid]

    vendeur_avis.vider_cache()
    assert vendeur_avis.evaluer("leboncoin", _Client(), "a")["avis"] == 4
    assert not vendeur_avis.evaluer("leboncoin", _Client(), "b")["ok"]
    assert vendeur_avis.evaluer("leboncoin", _Client(), "c")["avis"] == 120


def _brut_vendeur(n):
    return {"url": f"https://vinted.fr/vendeur/{n}",
            "titre": "Kit DDR4 32Go Corsair Vengeance LPX 3200",
            "description": "CMK32GX4M2E3200C16 2x16Go testé", "prix": 62.0,
            "photos": ["https://x/a.jpg"], "vendeur_id": 4000 + n}


@test("pipeline : un vendeur à 0 avis est rejeté et jamais notifiable")
def _():
    vendeur_avis.vider_cache()
    verif = lambda brut: vendeur_avis.evaluer("vinted", _FauxVinted(avis=0), brut["vendeur_id"])
    r = ram_scrapers.traiter_annonce(_brut_vendeur(1), "vinted", cfg=None, verif_vendeur=verif)
    assert r["nouvelle"] and not r["notifiable"] and not r["a_analyser"]
    assert r["pre"]["exclusion"] == "vendeur_sans_avis"
    en_base = ram_db.get_annonce(r["annonce"]["id"])
    assert en_base["statut"] == "rejete" and en_base["vendeur_avis"] == 0


@test("pipeline : un vendeur avec avis passe, et son nombre d'avis est conservé")
def _():
    vendeur_avis.vider_cache()
    verif = lambda brut: vendeur_avis.evaluer("vinted", _FauxVinted(avis=12), brut["vendeur_id"])
    r = ram_scrapers.traiter_annonce(_brut_vendeur(2), "vinted", cfg=None, verif_vendeur=verif)
    assert not r["pre"]["exclusion"], r["pre"].get("rejet_motif")
    en_base = ram_db.get_annonce(r["annonce"]["id"])
    assert en_base["vendeur_avis"] == 12 and en_base["statut"] != "rejete"
    assert en_base["vendeur_note"] == 4.5, "la note du profil doit être gardée"


@test("pipeline : profil illisible → l'annonce est oubliée puis retentée, pas rejetée à vie")
def _():
    vendeur_avis.vider_cache()
    ko = _FauxVinted(erreur=vinted_client.VintedError("HTTP 429", status=429))
    verif_ko = lambda brut: vendeur_avis.evaluer("vinted", ko, brut["vendeur_id"])
    brut = _brut_vendeur(3)
    r = ram_scrapers.traiter_annonce(brut, "vinted", cfg=None, verif_vendeur=verif_ko)
    assert not r["nouvelle"] and not r["notifiable"]
    assert not ram_db.annonce_existe(brut["url"]), "l'annonce doit être oubliée pour être retentée"
    vendeur_avis.vider_cache()
    ok = _FauxVinted(avis=8)
    verif_ok = lambda b: vendeur_avis.evaluer("vinted", ok, b["vendeur_id"])
    r2 = ram_scrapers.traiter_annonce(brut, "vinted", cfg=None, verif_vendeur=verif_ok)
    assert r2["nouvelle"] and not r2["pre"]["exclusion"]


@test("pipeline : une annonce hors périmètre ne déclenche aucune requête de profil")
def _():
    vendeur_avis.vider_cache()
    faux = _FauxVinted(avis=3)
    verif = lambda brut: vendeur_avis.evaluer("vinted", faux, brut["vendeur_id"])
    brut = {"url": "https://vinted.fr/vendeur/ddr3", "titre": "Barrette RAM DDR3 8Go 1600",
            "description": "DDR3 PC3-12800", "prix": 10.0, "photos": ["a.jpg"],
            "vendeur_id": 4999}
    r = ram_scrapers.traiter_annonce(brut, "vinted", cfg=None, verif_vendeur=verif)
    assert r["pre"]["exclusion"] and faux.appels == 0, \
        "les requêtes de profil doivent être réservées aux annonces dans le périmètre"


@test("disjoncteur : une source qui échoue d'affilée est mise de côté pour le cycle")
def _():
    import ram_sniper

    class _Bloquee:
        source = "vinted"
        echecs_consecutifs = 0
        appels = 0
        def mots_cles(self): return [f"mot{i}" for i in range(10)]
        def _du_a_passer(self, mot): return True
        def scanner_mot_cle(self, mot):
            self.appels += 1
            self.echecs_consecutifs += 1
            return []

    faux = _Bloquee()
    orig = ram_scrapers.ScraperVinted
    ram_scrapers.ScraperVinted = lambda cfg: faux
    orig_pause = ram_scrapers._pause
    orig_get = ram_config.get
    ram_scrapers._pause = lambda *a, **k: None
    cfg = ram_config.Config({"sources": {"vinted": {"actif": True, "disjoncteur_echecs": 3},
                                         "leboncoin": {"actif": False}}})
    ram_config.get = lambda force=False: cfg     # le worker relit la config à chaque cycle
    try:
        ram_sniper.worker_scraping(cfg, une_seule_fois=True)
    finally:
        ram_scrapers.ScraperVinted = orig
        ram_scrapers._pause = orig_pause
        ram_config.get = orig_get
    assert faux.appels == 3, f"{faux.appels} requêtes malgré le disjoncteur"


@test("proxy sortant : absent = aucun proxy ; identifiants encodés conservés pour lbc")
def _():
    import reseau
    os.environ.pop("SNIPER_PROXY_URL", None)
    assert reseau.proxies_http() is None
    url = "http://us%40er:p%3Aw@proxy.example:8080"
    os.environ["SNIPER_PROXY_URL"] = url
    try:
        assert reseau.proxies_http() == {"http": url, "https": url}
        try:
            proxy = reseau.proxy_lbc()
        except ImportError:
            return      # module lbc absent de cet environnement de test
        assert proxy.url == url, f"URL proxy altérée : {proxy.url}"
    finally:
        del os.environ["SNIPER_PROXY_URL"]


@test("mode --ci : option reconnue, pas d'élection de leader")
def _():
    import ram_sniper
    assert ram_sniper._valider_options(["--ci"], silencieux=True)
    os.environ["RAM_SANS_ELECTION"] = "1"
    try:
        role, _ = ram_telegram.role_instance(ram_config.get(), force=True)
        assert role == "principal"
    finally:
        del os.environ["RAM_SANS_ELECTION"]


# ═══════════════════════════════════════════════════════════
section("Anti-blocage : chaîne de sorties réseau")

import reseau                                                     # noqa: E402


class _Env:
    """Variables d'environnement temporaires + état des sorties remis à zéro."""
    def __init__(self, **variables):
        self.variables = variables
        self.avant = {}

    def __enter__(self):
        for k, v in self.variables.items():
            self.avant[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import uuid
        self.fichier = os.path.join(_TMP, f"sorties_{uuid.uuid4().hex}.json")
        self.avant["SNIPER_ETAT_SORTIES"] = os.environ.get("SNIPER_ETAT_SORTIES")
        os.environ["SNIPER_ETAT_SORTIES"] = self.fichier
        reseau.recharger()
        return self

    def __exit__(self, *exc):
        for k, v in self.avant.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reseau.recharger()


_MAISON = "socks5h://127.0.0.1:1055"
_WARP = "socks5h://127.0.0.1:40000"


@test("sorties : direct seul par défaut ; maison, WARP puis proxys dans cet ordre")
def _():
    with _Env():
        assert [x.nom for x in reseau.sorties()] == ["direct"]
    with _Env(SNIPER_PROXY_WARP=_WARP, SNIPER_PROXY_MAISON=_MAISON,
              SNIPER_PROXY_URLS="http://a:1, http://b:2", SNIPER_PROXY_URL="http://a:1"):
        assert [x.nom for x in reseau.sorties()] == \
            ["direct", "maison", "warp", "proxy1", "proxy2"], "doublon ou ordre faux"
    with _Env(SNIPER_PROXY_MAISON=_MAISON, SNIPER_ORDRE_SORTIES="maison,direct"):
        assert [x.nom for x in reseau.sorties()][:2] == ["maison", "direct"]
    with _Env(SNIPER_DIRECT="0"):
        assert [x.nom for x in reseau.sorties()] == ["direct"], \
            "sans aucune autre sortie, on garde le direct plutôt que rien"


@test("sortie bloquée : mise à l'écart de plus en plus longue, la suivante prend le relais")
def _():
    with _Env(SNIPER_PROXY_WARP=_WARP):
        direct, warp = reseau.sorties()
        assert reseau.choisir("vinted") == direct
        d1 = reseau.signaler_echec("vinted", direct, "HTTP 403")
        assert reseau.choisir("vinted") == warp
        reseau.recharger()                                  # nouveau passage, même fichier
        assert reseau.choisir("vinted") == warp, "l'état doit survivre au redémarrage"
        d2 = reseau.signaler_echec("vinted", direct, "HTTP 403")
        assert d2 > d1, "la mise à l'écart doit s'allonger"
        assert reseau.choisir("leboncoin") == direct, "chaque source a son propre état"
        reseau.signaler_succes("vinted", direct)
        assert reseau.choisir("vinted") == direct, "un succès lève la mise à l'écart"


@test("tout est bloqué : une seule sonde, puis on n'insiste plus")
def _():
    with _Env(SNIPER_PROXY_WARP=_WARP):
        for x in reseau.sorties():
            reseau.signaler_echec("vinted", x, "403")
        sonde = reseau.choisir("vinted")
        assert sonde is not None, "il faut une sonde pour détecter le déblocage"
        assert reseau.choisir("vinted") is None, "pas de deuxième sonde dans la foulée"


@test("état abîmé ou absent : aucun plantage ; identifiants de proxy jamais dans les logs")
def _():
    with _Env() as env:
        with open(env.fichier, "w") as f:
            f.write("{pas du json")
        reseau.recharger()
        assert reseau.choisir("vinted").nom == "direct"
        reseau.signaler_echec("vinted", reseau.sorties()[0], "x")
    assert "secret" not in reseau.masquer("échec via http://moi:secret@hote:8080")


@test("alertes : « bloqué » après 2 cycles ratés, « rétabli » ensuite, secours annoncé une fois")
def _():
    with _Env(SNIPER_PROXY_WARP=_WARP):
        direct, warp = reseau.sorties()
        for attendu in (None, ("bloque", None)):
            reseau.debut_cycle()
            reseau.signaler_echec("vinted", direct, "403")
            reseau.signaler_echec("vinted", warp, "403")
            assert reseau.evaluer_alertes("vinted") == attendu
        reseau.debut_cycle()
        reseau.signaler_succes("vinted", warp)
        assert reseau.evaluer_alertes("vinted") == ("retabli", "warp")
    with _Env(SNIPER_PROXY_WARP=_WARP):
        direct, warp = reseau.sorties()
        reseau.debut_cycle()
        reseau.signaler_succes("vinted", direct)
        assert reseau.evaluer_alertes("vinted") is None
        reseau.debut_cycle()
        reseau.signaler_echec("vinted", direct, "403")
        reseau.signaler_succes("vinted", warp)
        assert reseau.evaluer_alertes("vinted") == ("secours", "warp")
        reseau.debut_cycle()
        reseau.signaler_succes("vinted", direct)
        reseau.evaluer_alertes("vinted")
        reseau.debut_cycle()
        reseau.signaler_succes("vinted", warp)
        assert reseau.evaluer_alertes("vinted") is None, "pas de spam si le direct va et vient"


@test("besoin de secours : vrai seulement si le direct a été bloqué récemment")
def _():
    with _Env():
        assert not reseau.besoin_secours()
        reseau.signaler_echec("leboncoin", reseau.sorties()[0], "Datadome")
        assert reseau.besoin_secours()


class _VintedSelonSortie:
    """Faux client Vinted : bloqué en direct, fonctionne via WARP."""
    appels = []

    def __init__(self, country=None, verbose=False, proxy=None, **kw):
        self.proxy = proxy

    def search(self, search_text=None, max_retries=3, **kw):
        _VintedSelonSortie.appels.append(self.proxy)
        if self.proxy is None:
            raise vinted_client.VintedError("bloqué par Datadome (HTTP 403)", status=403)
        return [{"id": 9001, "vendeur_id": 77, "subject": "Kit DDR4 32Go Corsair Vengeance 3200",
                 "description": "CMK32GX4M2E3200C16", "price": 40,
                 "url": f"https://www.vinted.fr/items/bascule-{len(_VintedSelonSortie.appels)}",
                 "image": "https://x/1.jpg"}]

    def get_user_feedback(self, uid):
        return {"avis": 12, "note": 4.9, "pseudo": "ok"}


@test("Vinted : 403 en direct → même mot-clé repris via WARP, sans le perdre")
def _():
    orig = ram_scrapers.VintedClient
    ram_scrapers.VintedClient = _VintedSelonSortie
    vendeur_avis.vider_cache()
    try:
        with _Env(SNIPER_PROXY_WARP=_WARP):
            _VintedSelonSortie.appels = []
            sc = ram_scrapers.ScraperVinted(ram_config.get())
            resultats = sc.scanner_mot_cle("ddr4 32go")
            assert resultats, "l'annonce doit être trouvée via la sortie de secours"
            assert _VintedSelonSortie.appels == [None, _WARP], _VintedSelonSortie.appels
            assert sc.echecs_consecutifs == 0
            reseau.recharger()                       # passage suivant (nouveau processus)
            _VintedSelonSortie.appels = []
            ram_scrapers.ScraperVinted(ram_config.get()).scanner_mot_cle("kit ddr4")
            assert _VintedSelonSortie.appels == [_WARP], \
                "le passage suivant ne doit pas re-griller une requête sur l'IP bloquée"
    finally:
        ram_scrapers.VintedClient = orig


@test("Vinted : une erreur qui ne vient pas de l'IP (HTTP 500) ne change pas de sortie")
def _():
    class _Erreur500(_VintedSelonSortie):
        def search(self, **kw):
            _VintedSelonSortie.appels.append(self.proxy)
            raise vinted_client.VintedError("HTTP 500 inattendu", status=500)

    orig = ram_scrapers.VintedClient
    ram_scrapers.VintedClient = _Erreur500
    try:
        with _Env(SNIPER_PROXY_WARP=_WARP):
            _VintedSelonSortie.appels = []
            ram_scrapers.ScraperVinted(ram_config.get()).scanner_mot_cle("ddr4")
            assert _VintedSelonSortie.appels == [None]
            assert not reseau.bloquee("vinted", reseau.sorties()[0])
    finally:
        ram_scrapers.VintedClient = orig


@test("Leboncoin : Datadome dès la connexion → bascule sur la sortie maison")
def _():
    from types import SimpleNamespace as NS

    class DatadomeError(Exception):
        pass

    connexions = []

    def faux_client(sortie, **kw):
        connexions.append(sortie.nom)
        if sortie.directe:
            raise DatadomeError("Access blocked by Datadome")
        return NS(search=lambda **kw: NS(ads=[]), get_user=lambda uid: None)

    faux_lbc = NS(Sort=NS(NEWEST=1), AdType=NS(OFFER=1),
                  Category=NS(ELECTRONIQUE_ACCESSOIRES_INFORMATIQUE=1))
    sauvegarde = (ram_scrapers._HAS_LBC, getattr(ram_scrapers, "lbc", None), reseau.client_lbc)
    ram_scrapers._HAS_LBC, ram_scrapers.lbc, reseau.client_lbc = True, faux_lbc, faux_client
    try:
        with _Env(SNIPER_PROXY_MAISON=_MAISON):
            sc = ram_scrapers.ScraperLeboncoin(ram_config.get())
            sc.scanner_mot_cle("ddr4")
            assert connexions == ["direct", "maison"], connexions
            assert sc.echecs_consecutifs == 0 and sc.sortie.nom == "maison"
    finally:
        ram_scrapers._HAS_LBC, ram_scrapers.lbc, reseau.client_lbc = sauvegarde


@test("Vinted : quand toutes les sorties sont à l'écart, aucune requête n'est envoyée")
def _():
    orig = ram_scrapers.VintedClient
    ram_scrapers.VintedClient = _VintedSelonSortie
    try:
        with _Env():
            direct = reseau.sorties()[0]
            reseau.signaler_echec("vinted", direct, "403")
            reseau.choisir("vinted")                       # la sonde du passage est consommée
            _VintedSelonSortie.appels = []
            sc = ram_scrapers.ScraperVinted(ram_config.get())
            assert sc.scanner_mot_cle("ddr4") == []
            assert _VintedSelonSortie.appels == [] and sc.echecs_consecutifs == 1
    finally:
        ram_scrapers.VintedClient = orig


@test("mode --ci : tout bloqué → vert si Telegram prévient, rouge (3) sinon")
def _():
    import ram_sniper

    class _ToujoursBloque(_VintedSelonSortie):
        def search(self, **kw):
            raise vinted_client.VintedError("HTTP 403", status=403)

    orig = ram_scrapers.VintedClient
    orig_get, orig_pause = ram_config.get, ram_scrapers._pause
    ram_scrapers.VintedClient = _ToujoursBloque
    ram_scrapers._pause = lambda *a, **k: None
    cfg = ram_config.Config({
        "sources": {"vinted": {"actif": True, "mots_cles": ["ddr4", "kit ddr4"]},
                    "leboncoin": {"actif": False}},
        "ci": {"budget_notifications_s": 1}, "vision": {"actif": False},
        "alertes": {"signe_de_vie_h": 0}, "calibrage": {"actif": False,
                                                         "heure_job": "99:99"}})
    ram_config.get = lambda force=False: cfg
    try:
        with _Env(SNIPER_PROXY_WARP=_WARP):
            assert ram_sniper.executer_ci(cfg) == 3, "sans Telegram, le run rouge est le seul signal"
        with _Env(SNIPER_PROXY_WARP=_WARP, TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="1"):
            assert ram_sniper.executer_ci(cfg) == 0, \
                "avec Telegram : alerte sur le téléphone, pas un e-mail GitHub toutes les 10 min"
    finally:
        ram_scrapers.VintedClient = orig
        ram_config.get, ram_scrapers._pause = orig_get, orig_pause


@test("signe de vie : un message par jour, pas plus ; maintenance une fois par jour")
def _():
    import ram_sniper
    cfg = ram_config.Config({"alertes": {"signe_de_vie_h": 24},
                             "calibrage": {"actif": False, "heure_job": "00:00"}})
    with _Env(TELEGRAM_BOT_TOKEN="x", TELEGRAM_CHAT_ID="1"):
        assert ram_sniper.signe_de_vie(cfg, ["vinted"]) is True
        assert ram_sniper.signe_de_vie(cfg, ["vinted"]) is False
        texte = ram_sniper.message_signe_de_vie(["vinted", "leboncoin"])
        assert "Vinted" in texte and "Leboncoin" in texte
        assert ram_sniper.maintenance_quotidienne(cfg) is True
        assert ram_sniper.maintenance_quotidienne(cfg) is False
    with _Env():
        assert ram_sniper.signe_de_vie(cfg, ["vinted"]) is False, "sans Telegram : rien"


@test("messages d'alerte lisibles, et conseil adapté s'il n'y a qu'une sortie")
def _():
    import ram_sniper
    with _Env():
        assert "Tailscale" in ram_sniper.message_alerte("vinted", "bloque")
    with _Env(SNIPER_PROXY_MAISON=_MAISON):
        m = ram_sniper.message_alerte("leboncoin", "bloque")
        assert "Leboncoin" in m and "maison" in m
        assert "warp" in ram_sniper.message_alerte("vinted", "secours", "warp")


@test("empreintes navigateur : la version qui marchait reste en tête, versions inconnues écartées")
def _():
    cands = vinted_client.IMPERSONATE_CANDIDATES
    assert cands and cands[0] == "chrome124", cands
    assert len(cands) == len(set(cands))



# ═══════════════════════════════════════════════════════════
section("Plage horaire, arnaques, composants")


def _utc(y, mo, d, h, mi=0):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


@test("plage horaire : 7h-23h à l'heure de Paris, été comme hiver")
def _():
    import ram_sniper
    cfg = ram_config.Config({"ci": {"heures_actives": [7, 23], "fuseau": "Europe/Paris"}})
    ok = lambda dt: ram_sniper.dans_plage_horaire(cfg, dt)
    with _Env(SNIPER_HEURES=None, SNIPER_IGNORER_PLAGE=None):
        assert not ok(_utc(2026, 12, 1, 5, 30)), "06:30 à Paris (hiver) : trop tôt"
        assert ok(_utc(2026, 12, 1, 6, 0)), "07:00 à Paris (hiver)"
        assert ok(_utc(2026, 12, 1, 21, 59)), "22:59 à Paris (hiver)"
        assert not ok(_utc(2026, 12, 1, 22, 0)), "23:00 : fin exclue"
        assert ok(_utc(2026, 7, 1, 5, 0)), "07:00 à Paris (été, UTC+2)"
        assert not ok(_utc(2026, 7, 1, 4, 59)), "06:59 à Paris (été)"
        assert not ok(_utc(2026, 7, 1, 21, 0)), "23:00 à Paris (été)"


@test("plage horaire : SNIPER_HEURES prend le pas, « 0-24 » = toujours, plage qui passe minuit")
def _():
    import ram_sniper
    cfg = ram_config.Config({"ci": {"heures_actives": [7, 23]}})
    midi, tard, nuit = _utc(2026, 12, 1, 11), _utc(2026, 12, 1, 22, 30), _utc(2026, 12, 1, 2)
    with _Env(SNIPER_HEURES="9-18"):
        assert ram_sniper.dans_plage_horaire(cfg, midi)
        assert not ram_sniper.dans_plage_horaire(cfg, tard)
    with _Env(SNIPER_HEURES="0-24"):
        assert ram_sniper.dans_plage_horaire(cfg, nuit)
    with _Env(SNIPER_HEURES="22-6"):
        assert ram_sniper.dans_plage_horaire(cfg, tard) and ram_sniper.dans_plage_horaire(cfg, nuit)
        assert not ram_sniper.dans_plage_horaire(cfg, midi)
    with _Env(SNIPER_HEURES="n'importe quoi"):
        assert ram_sniper.dans_plage_horaire(cfg, nuit), "valeur illisible : jamais bloquer"
    with _Env(SNIPER_HEURES="9-18", SNIPER_IGNORER_PLAGE="1"):
        assert ram_sniper.dans_plage_horaire(cfg, nuit), "lancement manuel : toujours"
    with _Env(SNIPER_HEURES=None, SNIPER_IGNORER_PLAGE=None):
        assert ram_sniper.dans_plage_horaire(ram_config.Config({}), nuit), "pas de plage = 24 h/24"


@test("hors plage : le passage est ignoré sans toucher la base ni le réseau")
def _():
    import ram_sniper
    cfg = ram_config.Config({"ci": {"heures_actives": [7, 23]}})
    orig_plage, orig_init = ram_sniper.dans_plage_horaire, ram_db.init_db

    def _interdit():
        raise AssertionError("la base ne doit pas être ouverte hors plage")
    ram_sniper.dans_plage_horaire = lambda c=None, m=None: False
    ram_db.init_db = _interdit
    try:
        assert ram_sniper.executer_ci(cfg) == 0
    finally:
        ram_sniper.dans_plage_horaire, ram_db.init_db = orig_plage, orig_init


@test("workflow : le job « plage » décide, avec la même logique que le code (vrai bash, fausse horloge)")
def _():
    import subprocess
    import yaml
    wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          ".github", "workflows", "ram-sniper.yml"),
                             encoding="utf-8"))
    assert wf["jobs"]["scan"]["needs"] == "plage"
    assert "needs.plage.outputs.actif" in wf["jobs"]["scan"]["if"]
    heures_cron = wf[True]["schedule"][0]["cron"].split()[1]
    assert heures_cron == "5-21", "le cron en UTC doit couvrir 7h-23h Paris, été et hiver"
    script = wf["jobs"]["plage"]["steps"][0]["run"]
    boite = tempfile.mkdtemp(prefix="fausse_horloge_")

    def actif(heure, plage, evenement="schedule"):
        d = os.path.join(boite, f"h{heure}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "date"), "w") as f:
            f.write(f"#!/bin/sh\necho {heure}\n")
        os.chmod(os.path.join(d, "date"), 0o755)
        sortie = os.path.join(boite, f"out_{heure}_{plage}_{evenement}")
        subprocess.run(["bash", "-c", script], check=True, capture_output=True, env={
            "PATH": d + ":" + os.environ["PATH"], "EVENEMENT": evenement,
            "SNIPER_HEURES": plage, "GITHUB_OUTPUT": sortie})
        return open(sortie).read().strip() == "actif=true"

    assert [actif(h, "7-23") for h in (6, 7, 12, 22, 23, 0)] == [False, True, True, True, False, False]
    assert actif(3, "0-24") and actif(12, "9-18") and not actif(19, "9-18")
    assert actif(23, "22-6") and actif(3, "22-6") and not actif(12, "22-6")
    assert actif(3, "7-23", "workflow_dispatch"), "un lancement manuel ignore la plage"


@test("arnaque : « neuf » à vil prix + WhatsApp + urgence = rejet ; annonce honnête = rien")
def _():
    import arnaque
    a = arnaque.evaluer("RTX 4090 neuve", "jamais ouverte, urgent, contactez moi sur whatsapp", 400, 1500)
    assert a["rejeter"] and a["niveau"] == "eleve" and a["score"] == 100, a
    b = arnaque.evaluer("RTX 3060 Ti", "Utilisée 2 ans, changement de config, facture dispo", 190, 230)
    assert b["niveau"] == "faible" and not b["rejeter"] and arnaque.drapeau(b) is None, b
    c = arnaque.evaluer("Ryzen 7 5800X", "Appelez moi au 06 12 34 56 78, virement uniquement", 100, 190)
    assert c["rejeter"] and "paiement hors plateforme" in c["raisons"], c
    d = arnaque.evaluer("Ryzen 7 5800X", "Processeur en bon état, vendu avec sa boîte d'origine et sa facture.", 70, 190)
    assert d["niveau"] == "moyen" and not d["rejeter"], "le prix seul alerte, il ne rejette pas"
    assert "37 %" in arnaque.drapeau(d) or "37%" in arnaque.drapeau(d), arnaque.drapeau(d)
    e = arnaque.evaluer("Carte graphique", "écran noir : pas de signal vidéo", 50, None)
    assert e["niveau"] == "faible", "« signal » seul n'est pas un contact hors plateforme"


@test("arnaque : un code SMS demandé ou un e-mail dans l'annonce sont des signaux forts")
def _():
    import arnaque
    assert arnaque.evaluer("GPU", "envoyez le code de verification recu par sms", 100, 150)["rejeter"]
    assert arnaque.evaluer("GPU", "ecrivez moi a jean.dupont@gmail.com", 100, 150)["niveau"] != "faible"
    assert arnaque.evaluer("GPU", "paiement par bitcoin accepté", 100, 150)["niveau"] != "faible"


@test("RAM : un kit « neuf » bradé avec WhatsApp est rejeté pour arnaque, jamais notifié")
def _():
    brut = {"url": "https://vinted.fr/arnaque/ram1",
            "titre": "Kit DDR4 32Go Corsair Vengeance LPX 3200",
            "description": "CMK32GX4M2E3200C16 2x16Go neuf jamais ouvert urgent contactez moi sur whatsapp",
            "prix": 9.0, "photos": ["https://x/a.jpg"]}
    r = ram_scrapers.traiter_annonce(brut, "vinted", "ddr4 32go")
    assert r["pre"]["exclusion"] == "arnaque" and not r["notifiable"], r["pre"]
    assert ram_db.get_annonce(r["annonce"]["id"])["statut"] == "rejete"


@test("RAM : un risque modéré n'écarte pas l'annonce mais ajoute un drapeau ⚠️ en tête")
def _():
    brut = {"url": "https://vinted.fr/arnaque/ram2",
            "titre": "Kit DDR4 32Go Corsair Vengeance LPX 3200",
            "description": "CMK32GX4M2E3200C16 2x16Go testé, raison familiale, livraison uniquement",
            "prix": 62.0, "photos": ["https://x/a.jpg"]}
    r = ram_scrapers.traiter_annonce(brut, "vinted", "ddr4 32go")
    assert not r["pre"]["exclusion"], r["pre"].get("rejet_motif")
    assert "arnaque" in r["pre"]["drapeaux"][0] and r["pre"]["drapeaux"][0].startswith("⚠️")


import ram_composants as comp                                     # noqa: E402
_CAT = comp.catalogue(["CPU", "GPU", "MOBO"], 25)


@test("identification : le modèle exact, jamais un voisin (« 9600 » ≠ i5-9600K)")
def _():
    nom = lambda t: (comp.identifier(t, _CAT).nom if comp.identifier(t, _CAT) else None)
    assert nom("Ryzen 5 5600X 100€") == "Ryzen 5 5600X"
    assert nom("Ryzen 5 5600 AM4") == "Ryzen 5 5600", "5600 ne doit pas devenir 5600X"
    assert nom("i5-9600K") == "i5-9600K", "pas un Ryzen 5 9600"
    assert nom("Ryzen 5 9600 AM5") == "Ryzen 5 9600"
    assert nom("RTX3060Ti Gigabyte") == "RTX 3060 Ti" == nom("rtx 3060 ti")
    assert nom("RTX 3060 12go") == "RTX 3060 12GB"
    assert nom("i5 12400f") == "i5-12400F" == nom("i5-12400F neuf")
    assert nom("i7 14700KF") == "i7-14700KF", "KF et K sont deux produits"
    assert nom("Carte mère MSI B550M Pro") == "B550 (AM4)"
    assert nom("9700K") == "i7-9700K"
    assert nom("Ryzen 7 5800X3D") == "Ryzen 7 5800X3D"


@test("identification : titre ambigu ou sans modèle → on s'abstient")
def _():
    for t in ("Ryzen 5 5600X + RTX 3060", "RTX 3070 ou RTX 3060 Ti", "i5-12400F ou i5-12400",
              "Vend mon PC", "carte graphique gaming 8go", "9600"):
        assert comp.identifier(t, _CAT) is None, t


@test("catalogue : market_db + estimations de market_extra (marquées, seuils plus stricts)")
def _():
    import market_db
    estimes = [m for m in _CAT if m.estime]
    assert len(_CAT) > 250 and 40 < len(estimes) < len(_CAT)
    m = next(x for x in estimes if x.nom == "i5-12400")
    assert abs(m.good / m.fair - 0.80) < 0.01 and abs(m.steal / m.fair - 0.65) < 0.01
    noms = {x.nom for x in _CAT}
    assert {k for k, v in market_db.CPU.items() if v["fair"] >= 25} <= noms, \
        "tout market_db (≥ 25 €) doit être là"
    assert all(x.fair >= 25 for x in _CAT), "les modèles sous 25 € sont écartés"


@test("prix de référence : la grille est corrigée par le marché observé, sans dériver")
def _():
    cfg = ram_config.Config({"composants": {}})
    m = comp.Modele("CPU", "X 1000", 100, 83, 68, ["x 1000"])
    e = comp.Modele("CPU", "X 1000", 100, 80, 65, ["x 1000"], estime=True)
    assert comp.prix_reference(m, cfg, observes=[150] * 5)[0] == 100, "moins de 8 annonces : grille"
    assert abs(comp.prix_reference(m, cfg, observes=[150] * 20)[0] - 117.5) < 0.01, \
        "150 × 0,93 plafonné à +35 % = 135, mélangé à 50 %"
    assert abs(comp.prix_reference(e, cfg, observes=[150] * 20)[0] - 124.5) < 0.01, \
        "une estimation se corrige plus vite (70 %)"
    assert abs(comp.prix_reference(m, cfg, observes=[60] * 10)[0] - 82.5) < 0.01, \
        "le marché peut aussi baisser (plancher −35 %)"
    assert comp.prix_reference(m, cfg, observes=[5] * 20)[0] == 100, "annonces farfelues ignorées"


def _brut_c(n, titre, prix, **plus):
    return dict({"url": f"https://vinted.fr/composant/{n}", "titre": titre, "prix": prix,
                 "description": "Processeur utilisé un an, très bon état, facture disponible",
                 "photos": ["https://x/a.jpg"], "vendeur_id": 9000 + n}, **plus)


@test("composants : une affaire est repérée, vérifiée, enregistrée, et jamais retraitée")
def _():
    cfg = ram_config.Config({})
    appels = []
    verif = lambda b: appels.append(b["url"]) or {"ok": True, "avis": 12}
    r = comp.evaluer_annonce(_brut_c(1, "Ryzen 5 5600X", 70), "vinted", verif=verif)
    assert r["statut"] == "a_notifier" and r["niveau"] == "bonne", r
    assert r["marge"] > 15 and r["prix_total"] > 70, "frais Vinted ajoutés"
    assert comp._ligne(r["url"])["vendeur_avis"] == 12
    assert comp.evaluer_annonce(_brut_c(1, "Ryzen 5 5600X", 70), "vinted", verif=verif)["statut"] == "deja_vue"
    assert len(appels) == 1, "un seul profil vendeur demandé"


@test("composants : pas d'affaire = aucune requête de profil ; vendeur à 0 avis = rejet")
def _():
    appels = []
    verif = lambda b: appels.append(1) or {"ok": True, "avis": 12}
    r = comp.evaluer_annonce(_brut_c(2, "Ryzen 5 5600X", 95), "vinted", verif=verif)
    assert r["statut"] == "vue" and not appels
    r = comp.evaluer_annonce(_brut_c(3, "Ryzen 5 5600X", 70), "vinted",
                             verif=lambda b: {"ok": False, "avis": 0, "motif": "vendeur sans avis"})
    assert r["statut"] == "rejetee" and "avis" in r["motif"]


@test("composants : profil illisible (403) → annonce oubliée et retentée, pas rejetée à vie")
def _():
    r = comp.evaluer_annonce(_brut_c(4, "Ryzen 5 5600X", 70), "vinted",
                             verif=lambda b: {"ok": False, "transitoire": True, "motif": "HTTP 403"})
    assert r["statut"] == "transitoire" and comp._ligne(r["url"]) is None
    r = comp.evaluer_annonce(_brut_c(4, "Ryzen 5 5600X", 70), "vinted",
                             verif=lambda b: {"ok": True, "avis": 5})
    assert r["statut"] == "a_notifier"


@test("composants : à 35 % du marché, un vendeur à 3 avis est rejeté, un vendeur à 45 avis est signalé ⚠️")
def _():
    r = comp.evaluer_annonce(_brut_c(5, "Ryzen 5 5600X", 35), "vinted",
                             verif=lambda b: {"ok": True, "avis": 3})
    assert r["statut"] == "rejetee" and "trop beau" in r["motif"], r
    r = comp.evaluer_annonce(_brut_c(6, "Ryzen 5 5600X", 35), "vinted",
                             verif=lambda b: {"ok": True, "avis": 45})
    assert r["statut"] == "a_notifier" and r["niveau"] == "affaire", r
    assert "arnaque" in r["motif"], "le doute doit être écrit dans la notification"


@test("composants : boîte vide, HS, PC complet, portable, lot ambigu → pas de fausse alerte")
def _():
    verif = lambda b: {"ok": True, "avis": 50}
    for n, titre, prix in ((7, "Boite vide carte graphique RTX 3060", 40),
                           (8, "RTX 3060 HS pour pièces", 40),
                           (9, "PC complet Ryzen 5 5600X", 70),
                           (10, "Asus portable RTX 3060 gaming", 150)):
        r = comp.evaluer_annonce(_brut_c(n, titre, prix), "vinted", verif=verif)
        assert r and r["statut"] == "rejetee", (titre, r)
    assert comp.evaluer_annonce(_brut_c(11, "PC gamer Ryzen 5 5600X RTX 3060", 300), "vinted",
                                verif=verif) is None
    r = comp.evaluer_annonce(_brut_c(12, "RTX 3060", 12), "vinted", verif=verif)
    assert r["statut"] == "rejetee" and "plancher" in r["motif"]


@test("composants : annonce WhatsApp / acompte → rejet pour arnaque")
def _():
    r = comp.evaluer_annonce(
        _brut_c(13, "Ryzen 5 5600X", 60, description="urgent, contactez moi par whatsapp"),
        "vinted", verif=lambda b: {"ok": True, "avis": 50})
    assert r["statut"] == "rejetee" and "arnaque" in r["motif"]


class _FauxScraper:
    source = "vinted"

    def __init__(self, resultats):
        self.resultats, self.requetes = resultats, []
        self.echecs_consecutifs, self._nb_bruts = 0, 0

    def rechercher(self, mot):
        self.requetes.append(mot)
        bruts = self.resultats.get(mot)
        self._nb_bruts = len(bruts or [])
        return bruts

    def _verif_vendeur(self, brut):
        return {"ok": True, "avis": 20}


@test("composants : scan d'une source, notification plafonnée, expiration des vieilles affaires")
def _():
    import ram_scrapers
    cfg = ram_config.Config({"composants": {"requetes_par_source": 1, "max_notifs_par_passage": 2,
                                            "pause_notif_s": 0},
                             "sources": {"vinted": {"delai_entre_requetes_s": [0, 0]}}})
    with ram_db.get_db() as conn:     # affaires laissées en attente par les tests précédents
        conn.execute("UPDATE ram_composant SET statut='notifiee' WHERE statut='a_notifier'")
    modele = [comp.Modele("CPU", "Ryzen 5 5600X", 100, 83, 68, ["Ryzen 5 5600X"])]
    faux = _FauxScraper({"Ryzen 5 5600X": [
        _brut_c(20, "Ryzen 5 5600X", 70), _brut_c(21, "Ryzen 5 5600X", 72),
        _brut_c(22, "Ryzen 5 5600X", 71), _brut_c(23, "Ryzen 5 5600X", 120)]})
    orig = ram_scrapers._pause
    ram_scrapers._pause = lambda *a, **k: None
    try:
        evals = comp.scanner_source(faux, cfg, limite=1, modeles=modele)
    finally:
        ram_scrapers._pause = orig
    assert faux.requetes == ["Ryzen 5 5600X"], faux.requetes
    assert sorted(e["statut"] for e in evals) == ["a_notifier"] * 3 + ["vue"], evals
    stats = [s for s in ram_db.scan_stats() if s["mot_cle"] == "Ryzen 5 5600X"]
    assert stats and stats[0]["requetes"] >= 1, ram_db.scan_stats()
    n1, n2, n3 = comp.notifier(cfg), comp.notifier(cfg), comp.notifier(cfg)
    assert (n1, n2, n3) == (2, 1, 0), (n1, n2, n3)
    # (les annonces vues plus haut ont fait baisser le prix de référence : le
    # marché apprend, 70 € n'est plus une affaire pour ce modèle dans ce test)
    comp.evaluer_annonce(_brut_c(24, "Ryzen 5 5600X", 45), "vinted", verif=lambda b: {"ok": True, "avis": 9})
    with ram_db.get_db() as conn:
        conn.execute("UPDATE ram_composant SET vu_le=? WHERE url=?", (time.time() - 10 * 3600,
                                                                      "https://vinted.fr/composant/24"))
    assert comp.notifier(cfg) == 0, "une affaire vieille de 10 h est abandonnée"
    assert comp._ligne("https://vinted.fr/composant/24")["statut"] == "expiree", \
        comp._ligne("https://vinted.fr/composant/24")


@test("composants : les recherches tournent (les plus en retard d'abord) et le disjoncteur coupe")
def _():
    cfg = ram_config.Config({"composants": {"requetes_par_source": 4}})
    premieres = comp.requetes_du_passage("leboncoin", cfg, modeles=_CAT)
    assert len(premieres) == len(set(premieres)) == 4
    for mot in premieres:
        ram_db.maj_scan_stat("leboncoin", mot, requetes=1)
    suivantes = comp.requetes_du_passage("leboncoin", cfg, modeles=_CAT)
    assert not set(premieres) & set(suivantes), "une recherche faite à l'instant passe après les autres"
    import ram_scrapers
    faux = _FauxScraper({})
    faux.source = "leboncoin"
    faux.echecs_consecutifs = 4
    ram_scrapers_pause, ram_scrapers._pause = ram_scrapers._pause, lambda *a, **k: None
    try:
        comp.scanner_source(faux, ram_config.Config({"sources": {"leboncoin": {"disjoncteur_echecs": 4}}}),
                            limite=3, modeles=_CAT)
    finally:
        ram_scrapers._pause = ram_scrapers_pause
    assert faux.requetes == [], "source bloquée : aucune requête composants"


@test("composants : message Telegram lisible et échappé ; rapport de prix complet")
def _():
    ligne = {"niveau": "affaire", "categorie": "GPU", "modele": "RTX 3060 <b>", "source": "vinted",
             "prix_affiche": 100.0, "prix_total": 108.0, "reference": 200.0, "marge": 90.8,
             "vendeur_avis": 33, "motif": None, "titre": "RTX 3060 <script>", "url": "https://vinted.fr/x"}
    texte = comp.message(ligne, estime=True)
    assert "AFFAIRE EN OR" in texte and "&lt;b&gt;" in texte and "&lt;script&gt;" in texte
    assert "<script>" not in texte and "référence estimée" in texte and "https://vinted.fr/x" in texte
    lignes = comp.rapport_prix(ram_config.Config({}), "CPU")
    assert len(lignes) > 50 and all(x["reference"] > 0 for x in lignes)
    assert any(x["estime"] for x in lignes) and any(x["observees"] > 0 for x in lignes
                                                    if x["modele"] == "Ryzen 5 5600X")


class _VintedBoutEnBout:
    def __init__(self, **kw):
        pass

    def search(self, search_text=None, max_retries=2, **kw):
        if search_text != "Ryzen 5 5600X":
            return []
        base = {"description": "Processeur utilisé un an, très bon état, facture disponible"}
        return [dict(base, id=501, url="https://vinted.fr/e2e/501", subject="Ryzen 5 5600X",
                     price=45.0, image="https://x/a.jpg", vendeur_id=71),
                dict(base, id=502, url="https://vinted.fr/e2e/502", subject="Ryzen 5 5600X neuf",
                     price=20.0, image="https://x/b.jpg", vendeur_id=72,
                     description="jamais ouvert urgent contactez moi sur whatsapp")]

    def get_user_feedback(self, vendeur_id):
        return {"avis": 25, "note": 4.8, "pseudo": f"vendeur{vendeur_id}"}


@test("mode --ci de bout en bout : l'affaire est notifiée, l'arnaque rejetée, les deux en base")
def _():
    import ram_sniper
    vendeur_avis.vider_cache()
    orig, orig_get, orig_pause = ram_scrapers.VintedClient, ram_config.get, ram_scrapers._pause
    orig_req, orig_pause_v = comp.requetes_du_passage, vendeur_avis.PAUSE_S
    ram_scrapers.VintedClient = _VintedBoutEnBout
    ram_scrapers._pause = lambda *a, **k: None
    vendeur_avis.PAUSE_S = (0, 0)
    comp.requetes_du_passage = lambda *a, **k: ["Ryzen 5 5600X"]
    cfg = ram_config.Config({
        "sources": {"vinted": {"actif": True, "mots_cles": ["ddr4"]},
                    "leboncoin": {"actif": False}},
        "composants": {"categories": ["CPU"], "pause_notif_s": 0},
        "ci": {"budget_notifications_s": 1}, "vision": {"actif": False},
        "alertes": {"signe_de_vie_h": 0},
        "calibrage": {"actif": False, "heure_job": "99:99"}})
    ram_config.get = lambda force=False: cfg
    try:
        with _Env(SNIPER_HEURES=None, SNIPER_IGNORER_PLAGE="1"):
            assert ram_sniper.executer_ci(cfg) == 0
    finally:
        (ram_scrapers.VintedClient, ram_config.get, ram_scrapers._pause) = orig, orig_get, orig_pause
        comp.requetes_du_passage, vendeur_avis.PAUSE_S = orig_req, orig_pause_v
    bon, arnaque_ = comp._ligne("https://vinted.fr/e2e/501"), comp._ligne("https://vinted.fr/e2e/502")
    assert bon and bon["statut"] == "notifiee" and bon["vendeur_avis"] == 25, bon
    assert arnaque_ and arnaque_["statut"] == "rejetee" and "arnaque" in arnaque_["motif"], arnaque_



@test("config réelle : plage 7-23, composants CPU/GPU/MOBO, arnaque 60/30")
def _():
    cfg = ram_config.get()
    assert cfg.val("ci.heures_actives") == [7, 23] and cfg.val("ci.fuseau") == "Europe/Paris"
    assert cfg.val("composants.categories") == ["CPU", "GPU", "MOBO"]
    assert cfg.val("arnaque.seuil_rejet") == 60 and cfg.val("arnaque.seuil_alerte") == 30
    assert int(cfg.val("composants.requetes_par_source")) <= 8, "volume de requêtes borné"


# ─── Vinted : page de recherche (l'API /api/v2/catalog/items renvoie 404) ───
def _page_vinted(items):
    """Imite la page /catalog : JSON embarqué dans self.__next_f.push([1,"..."])
    (échappé comme Next.js le fait), avec du bruit avant et après."""
    import json as _json
    flux = ('{"catalog":{"uiState":"SUCCESS"},"items":{"items":' + _json.dumps(items)
            + ',"pagination":{"current_page":1,"total_entries":%d}}}' % len(items))
    coupe = len(flux) // 2   # le flux est livré en plusieurs morceaux
    pushes = "".join('<script>self.__next_f.push([1,%s])</script>' % _json.dumps(m)
                     for m in (flux[:coupe], flux[coupe:]))
    return "<html><body><script>self.__next_f.push([0])</script>" + pushes + "</body></html>"


_ANNONCES_VINTED = [  # extraits de vraies annonces (novembre 2026)
    {"id": 10749346611, "productItem": {
        "id": 10749346611, "title": "DDR4-2666 2x4GB",
        "url": "/items/10749346611-ddr4-2666-2x4gb",
        "price": {"amount": "20.00", "currencyCode": "EUR"},
        "serviceFee": {"amount": "1.70", "currencyCode": "EUR"},
        "totalItemPrice": {"amount": "21.70", "currencyCode": "EUR"},
        "user": {"id": 58478467, "isBusiness": False, "photo": None},
        "photos": [{"url": "https://images1.vinted.net/a/f800/x.webp?s=1", "isMain": True, "imageNo": 1},
                   {"url": "https://images1.vinted.net/b/f800/y.webp?s=2", "isMain": False, "imageNo": 2}],
        "itemBox": {"firstLine": "Kingston", "secondLine": "Très bon état"}}},
    {"id": 10749317237, "productItem": {
        "id": 10749317237, "title": "RAM Samsung DDR4 4GB \"3200\"MHz",
        "url": "/items/10749317237-ram-samsung-ddr4-4gb-3200mhz",
        "price": {"amount": "17.30", "currencyCode": "EUR"},
        "totalItemPrice": {"amount": "18.87", "currencyCode": "EUR"},
        "user": {"id": 113022532}, "photos": [],
        "itemBox": {"firstLine": "Samsung", "secondLine": "Très bon état"}}},
]


@test("Vinted : la page de recherche est lue (JSON embarqué, morceaux, guillemets)")
def _():
    import vinted_client
    items = vinted_client.extraire_items_catalogue(_page_vinted(_ANNONCES_VINTED))
    assert [i["id"] for i in items] == [10749346611, 10749317237], items
    assert items[1]["productItem"]["title"] == 'RAM Samsung DDR4 4GB "3200"MHz'
    assert vinted_client.extraire_items_catalogue(_page_vinted([])) == [], "0 résultat ≠ page inconnue"
    assert vinted_client.extraire_items_catalogue("<html>Access denied</html>") is None
    assert vinted_client.extraire_items_catalogue("") is None


@test("Vinted : annonce normalisée (URL absolue, prix hors frais, photos, vendeur)")
def _():
    import vinted_client
    vc = vinted_client.VintedClient.__new__(vinted_client.VintedClient)
    vc.host, vc.currency = "https://www.vinted.fr", "EUR"
    a = vc._normalize_catalogue(_ANNONCES_VINTED[0])
    assert a["url"] == "https://www.vinted.fr/items/10749346611-ddr4-2666-2x4gb", a["url"]
    assert a["price"] == 20, "prix de l'annonce, sans les 1,70 € de protection (ajoutés par le scoring)"
    assert a["vendeur_id"] == 58478467 and a["subject"] == "DDR4-2666 2x4GB"
    assert a["image"].endswith("x.webp?s=1") and len(a["photos"]) == 2
    assert a["description"] == "Kingston, Très bon état" and a["source"] == "vinted"
    b = vc._normalize_catalogue(_ANNONCES_VINTED[1])
    assert b["price"] == 17 and b["photos"] == [] and b["image"] == ""


@test("Vinted : search() passe par /catalog, borne max_items, et retombe sur l'API si besoin")
def _():
    import vinted_client
    appels = []

    class _Rep:
        def __init__(self, code, texte="", data=None):
            self.status_code, self.text, self._d, self.cookies = code, texte, data, {}
        def json(self):
            return self._d

    class _Session:
        def __init__(self, routes): self.routes = routes
        def get(self, url, **kw):
            appels.append(url.split("vinted.fr")[-1])
            return self.routes[url.split("vinted.fr")[-1]]()

    def client(routes):
        vc = vinted_client.VintedClient.__new__(vinted_client.VintedClient)
        vc.host, vc.domain, vc.currency, vc.verbose = "https://www.vinted.fr", "vinted.fr", "EUR", False
        vc.items_api = vc.host + "/api/v2/catalog/items"
        vc._cookie_ok, vc._cookie_ts, vc._impersonate = True, time.time(), None
        vc.session = _Session(routes)
        vc._ensure_session = lambda force=False: None   # pas de réseau dans les tests
        return vc

    page = _page_vinted(_ANNONCES_VINTED)
    vc = client({"/catalog": lambda: _Rep(200, page)})
    res = vc.search(search_text="ddr4", price_to=40, max_items=1)
    assert [r["id"] for r in res] == [10749346611] and appels == ["/catalog"], (res, appels)
    assert vc.search(search_text="ddr4")[1]["id"] == 10749317237

    appels.clear()   # page non reconnue -> ancienne API
    vc = client({"/catalog": lambda: _Rep(200, "<html>refonte</html>"),
                 "/api/v2/catalog/items": lambda: _Rep(200, data={"items": [
                     {"id": 7, "title": "DDR4 8Go", "url": "https://www.vinted.fr/items/7",
                      "price": {"amount": "12.0"}, "user": {"id": 3}}]})})
    res = vc.search(search_text="ddr4")
    assert res[0]["id"] == 7 and appels == ["/catalog", "/api/v2/catalog/items"], appels

    appels.clear()   # blocage Datadome : pas de repli (l'API serait bloquée aussi)
    vinted_client.time.sleep = lambda *_: None
    vc = client({"/catalog": lambda: _Rep(403, "blocked")})
    try:
        vc.search(search_text="ddr4", max_retries=2)
        assert False, "403 doit lever VintedError"
    except vinted_client.VintedError as e:
        assert e.status == 403, e
    assert "/api/v2/catalog/items" not in appels, appels


# ─── Mode continu : voie rapide, un fil par source, session avec relais ───
class _ScraperFactice:
    source = "vinted"
    echecs_consecutifs = 0

    def mots_cles(self):
        return ["ddr4", "barrette ram", "kit ddr4", "trident z", "hyperx"]


@test("tournée : les mots rapides reviennent à l'échéance, les autres tournent tous")
def _():
    import ram_sniper
    t = [1000.0]
    cfg = ram_config.Config({
        "sources": {"vinted": {"mots_cles_rapides": ["ddr4", "barrette ram"],
                               "rapide_toutes_les_s": [10, 10]}},
        "composants": {"actif": True, "requetes_par_source": 2, "requetes_larges": []}})
    tournee = ram_sniper.Tournee(_ScraperFactice(), horloge=lambda: t[0])
    assert [tournee.suivante(cfg) for _ in range(2)] == [("ram", "ddr4"), ("ram", "barrette ram")]
    lents = [tournee.suivante(cfg) for _ in range(5)]      # échéance pas atteinte : voie lente
    assert sorted(lents, key=str) == sorted(
        [("ram", "kit ddr4"), ("ram", "trident z"), ("ram", "hyperx"),
         ("composant", None), ("composant", None)], key=str), lents
    t[0] += 11                                             # échéance passée : rapide d'abord
    assert tournee.suivante(cfg) == ("ram", "ddr4")
    assert tournee.suivante(cfg) == ("ram", "barrette ram")
    assert tournee.suivante(cfg)[1] not in ("ddr4", "barrette ram"), "puis la voie lente reprend"
    # Sans réglage : le premier mot-clé de la source sert de voie rapide.
    assert ram_sniper.mots_rapides(_ScraperFactice(), ram_config.Config({})) == ["ddr4"]


@test("composants : recherches larges régulières, et « rtx » fait remonter une 3060 Ti et une 3070")
def _():
    import ram_sniper
    t = [5000.0]
    cfg = ram_config.Config({
        "sources": {"vinted": {"mots_cles_rapides": ["ddr4"], "rapide_toutes_les_s": [1000, 1000]}},
        "composants": {"categories": ["GPU", "CPU"], "requetes_par_source": 1,
                       "requetes_larges": ["rtx", "ryzen"],
                       "larges_toutes_les_s": {"vinted": [100, 100]}}})
    tournee = ram_sniper.Tournee(_ScraperFactice(), horloge=lambda: t[0])
    vus = []
    for _ in range(40):                       # 40 créneaux de 5 s = 200 s
        vus.append(tournee.suivante(cfg))
        t[0] += 5
    larges = [m for g, m in vus if g == "large"]
    assert 2 <= larges.count("rtx") <= 3 and 2 <= larges.count("ryzen") <= 3, larges
    assert any(g == "composant" for g, _ in vus) and any(m == "trident z" for _, m in vus), \
        "les recherches par modèle et les autres mots-clés RAM continuent"
    assert comp.reglages(ram_config.get())["requetes_larges"], "activé dans la vraie config"

    class _S:
        source, echecs_consecutifs, _nb_bruts = "vinted", 0, 3
        def rechercher(self, mot):
            assert mot == "rtx"
            base = {"description": "Carte graphique utilisée deux ans, très bon état, facture",
                    "image": "https://x/g.jpg", "vendeur_id": 9}
            return [dict(base, id=1, url="https://vinted.fr/large/1", titre="MSI RTX 3060 Ti Gaming X", prix=120.0),
                    dict(base, id=2, url="https://vinted.fr/large/2", titre="Gigabyte RTX 3070 Eagle 8Go", prix=260.0),
                    dict(base, id=3, url="https://vinted.fr/large/3", titre="Boîte vide rtx", prix=5.0)]
        def _verif_vendeur(self, brut):
            return {"ok": True, "avis": 40, "note": 4.9}
    evs = comp.scanner_large(_S(), "rtx", ram_config.Config({"composants": {"categories": ["GPU"]}}))
    par_modele = {e.get("modele"): e["statut"] for e in evs}
    assert par_modele.get("RTX 3060 Ti") == "a_notifier", evs      # 120 € pour ~230 €
    assert par_modele.get("RTX 3070") in ("vue", "rejetee"), evs   # au prix du marché : vue, pas notifiée
    assert len(evs) == 2, "la boîte vide n'est rattachée à aucun modèle"


@test("scan continu : la notification est mise en file AVANT la fin du lot, et le blocage met en pause")
def _():
    import ram_sniper
    ordre = []

    class _S(_ScraperFactice):
        def __init__(self):
            self.echecs_consecutifs = 0
            self.appels = 0

        def scanner_mot_cle(self, mot, sur_resultat=None):
            self.appels += 1
            if self.appels > 1:
                self.echecs_consecutifs += 1               # ensuite : bloqué
                return []
            lot = [{"nouvelle": True, "id": i} for i in (1, 2)]
            for r in lot:
                sur_resultat(r)
                ordre.append(("évaluée", r["id"]))
            ordre.append("fin du lot")
            return lot

    origs = (ram_sniper.traiter_resultats, ram_sniper._attendre_scan,
             ram_sniper.alerter_sorties, ram_config.get)
    pauses = []
    cfg = ram_config.Config({"sources": {"vinted": {"disjoncteur_echecs": 2,
                                                     "delai_entre_requetes_s": [0, 0]}},
                             "composants": {"actif": False}})
    ram_sniper.traiter_resultats = lambda rs, c: ordre.append(("file", rs[0]["id"]))
    ram_sniper.alerter_sorties = lambda c, sources: ordre.append(("bilan", tuple(sources)))

    def attendre(secondes):
        pauses.append(secondes)
        if secondes >= 60:
            ram_sniper.ARRET_SCAN.set()                    # fin du test à la 1re pause de blocage
    ram_sniper._attendre_scan = attendre
    ram_config.get = lambda force=False: cfg
    ram_sniper.ARRET.clear(); ram_sniper.ARRET_SCAN.clear()
    s = _S()
    try:
        with _Env():
            ram_sniper.worker_source(s, cfg)
    finally:
        (ram_sniper.traiter_resultats, ram_sniper._attendre_scan,
         ram_sniper.alerter_sorties, ram_config.get) = origs
        ram_sniper.ARRET_SCAN.clear()
    assert ordre[:5] == [("file", 1), ("évaluée", 1), ("file", 2), ("évaluée", 2), "fin du lot"], ordre
    assert s.appels == 3, "2 échecs d'affilée = disjoncteur"
    assert pauses[-1] == 60 and ("bilan", ("vinted",)) in ordre, (pauses, ordre)
    assert s.echecs_consecutifs == 0, "compteur remis à zéro pour la reprise"


@test("--continu de bout en bout : affaire notifiée, relais demandé à la durée, rien hors plage")
def _():
    import ram_sniper
    vendeur_avis.vider_cache()
    orig, orig_get, orig_pause = ram_scrapers.VintedClient, ram_config.get, ram_scrapers._pause
    orig_req, orig_pause_v = comp.requetes_du_passage, vendeur_avis.PAUSE_S
    orig_dans = ram_sniper.dans_plage_horaire

    class _Client(_VintedBoutEnBout):
        def search(self, search_text=None, max_retries=2, **kw):
            return [dict(r, id=r["id"] + 100, url=r["url"].replace("e2e", "continu"))
                    for r in super().search(search_text, max_retries)]

    ram_scrapers.VintedClient = _Client
    ram_scrapers._pause = lambda *a, **k: None
    vendeur_avis.PAUSE_S = (0, 0)
    comp.requetes_du_passage = lambda *a, **k: ["Ryzen 5 5600X"]
    cfg = ram_config.Config({
        "sources": {"vinted": {"actif": True, "mots_cles": ["ddr4"],
                               "delai_entre_requetes_s": [0.05, 0.05]},
                    "leboncoin": {"actif": False}},
        "composants": {"categories": ["CPU"], "pause_notif_s": 0, "requetes_par_source": 1},
        "ci": {"budget_notifications_s": 4}, "vision": {"actif": False},
        "alertes": {"signe_de_vie_h": 0},
        "calibrage": {"actif": False, "heure_job": "99:99"}})
    ram_config.get = lambda force=False: cfg
    sortie_gh = os.path.join(_TMP, "github_output_continu.txt")
    try:
        with _Env(SNIPER_HEURES="0-24", SNIPER_IGNORER_PLAGE=None, GITHUB_OUTPUT=sortie_gh):
            assert ram_sniper.executer_continu(cfg, duree_s=1.5, pas_s=0.1) == 0
            assert open(sortie_gh).read().strip().splitlines()[-1] == "relais=true"
            assert not ram_sniper.ARRET_SCAN.is_set() or ram_sniper.ARRET.is_set()
            # Hors plage, sans lancement manuel : rien ne démarre, pas de relais.
            ram_sniper.dans_plage_horaire = lambda *a, **k: False
            ram_scrapers.VintedClient = None               # tout appel réseau planterait
            assert ram_sniper.executer_continu(cfg, duree_s=1.5, pas_s=0.1) == 0
            assert open(sortie_gh).read().strip().splitlines()[-1] == "relais=false"
    finally:
        (ram_scrapers.VintedClient, ram_config.get, ram_scrapers._pause) = orig, orig_get, orig_pause
        comp.requetes_du_passage, vendeur_avis.PAUSE_S = orig_req, orig_pause_v
        ram_sniper.dans_plage_horaire = orig_dans
        ram_sniper.ARRET.clear(); ram_sniper.ARRET_SCAN.clear()
    bon, arnaque_ = comp._ligne("https://vinted.fr/continu/501"), comp._ligne("https://vinted.fr/continu/502")
    assert bon and bon["statut"] == "notifiee", bon
    assert arnaque_ and arnaque_["statut"] == "rejetee", arnaque_


@test("workflow : session continue, une seule à la fois, chien de garde et relais")
def _():
    import yaml
    wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          ".github", "workflows", "ram-sniper.yml"), encoding="utf-8"))
    scan = wf["jobs"]["scan"]
    assert scan["concurrency"] == {"group": "ram-sniper", "cancel-in-progress": False}
    assert 330 <= scan["timeout-minutes"] <= 355, "sous la limite GitHub de 6 h"
    assert float(ram_config.get().val("ci.duree_session_min")) * 60 < scan["timeout-minutes"] * 60 - 900
    etapes = [e.get("name") for e in scan["steps"]]
    assert etapes.index("Session continue") < etapes.index("Sauvegarder la base et l'état réseau") \
        < etapes.index("Relais vers la session suivante")
    session = next(e for e in scan["steps"] if e.get("name") == "Session continue")
    assert session["id"] == "session" and "--continu" in session["run"]
    assert "status != " in wf["jobs"]["plage"]["steps"][0]["run"], "le cron ne double pas une session"
    cfg = ram_config.get()
    assert cfg.val("sources.vinted.mots_cles_rapides") and cfg.val("sources.leboncoin.mots_cles_rapides")


# ─── Revue « sniper » : cadences, âge estimé, reprises rapides ───
@test("cadences : chaque recherche a la sienne, la plus en retard passe d'abord, la voie lente n'est jamais affamée")
def _():
    import ram_sniper
    t = [0.0]
    cfg = ram_config.Config({
        "sources": {"vinted": {"mots_cles_rapides": {"ddr4": [10, 10], "barrette ram": [50, 50]},
                               "composants_larges": {"rtx": [30, 30]}}},
        "composants": {"actif": True, "requetes_par_source": 1}})
    tournee = ram_sniper.Tournee(_ScraperFactice(), horloge=lambda: t[0])
    vus = []
    for _ in range(100):                      # 100 créneaux de 4 s = 400 s
        vus.append(tournee.suivante(cfg))
        t[0] += 4
    n = lambda genre, mot: vus.count((genre, mot))
    assert 28 <= n("ram", "ddr4") <= 40, n("ram", "ddr4")            # ~ toutes les 10-12 s
    assert 6 <= n("ram", "barrette ram") <= 9 and 10 <= n("large", "rtx") <= 14, vus
    lents = [v for v in vus if v not in (("ram", "ddr4"), ("ram", "barrette ram"), ("large", "rtx"))]
    assert {m for g, m in lents if g == "ram"} == {"kit ddr4", "trident z", "hyperx"}, lents
    # Voies fréquentes saturées (tout est toujours en retard) : 1 créneau sur 6 reste à la voie lente.
    sature = ram_config.Config({"sources": {"vinted": {"mots_cles_rapides": {"ddr4": [0, 0]}}},
                                "composants": {"actif": False}})
    tournee = ram_sniper.Tournee(_ScraperFactice(), horloge=lambda: t[0])
    serie = [tournee.suivante(sature) for _ in range(12)]
    assert sum(1 for v in serie if v != ("ram", "ddr4")) == 2, serie
    # La vraie config : ddr4 au plus toutes les 12 s sur Vinted, rtx et ryzen suivis sur les deux sites.
    reel = ram_config.get()
    assert dict((m, hi) for m, _, hi in ram_sniper.cadences_rapides(
        ram_scrapers.ScraperVinted(reel), reel))["ddr4"] <= 12
    for source in ("vinted", "leboncoin"):
        larges = {m for m, _, _ in ram_sniper.cadences_larges(source, reel)}
        assert {"rtx", "ryzen", "gtx", "radeon rx"} <= larges, (source, larges)


@test("Vinted : âge estimé par l'identifiant (repères, interpolation, prudence)")
def _():
    import vinted_age
    with _Env():
        vinted_age.reinitialiser()
        t0 = 1_000_000.0
        assert vinted_age.estimer(5000, t0) is None, "aucun repère : on ne dit rien"
        vinted_age.observer([900, 1000], t0)            # démarrage : base non datée
        assert vinted_age.estimer(1000, t0) is None
        vinted_age.observer([1000, 1100], t0 + 10)      # 1100 vient d'apparaître
        vinted_age.observer([1100], t0 + 20)            # rien de neuf
        vinted_age.observer([2100, "x", None], t0 + 1010)
        assert vinted_age.estimer(2100, t0 + 1015) == t0 + 1010, "vue à l'instant"
        assert abs(vinted_age.estimer(1600, t0 + 1015) - (t0 + 510)) < 1, "interpolée"
        assert vinted_age.estimer(900, t0 + 1015) is None, "trop peu de recul pour extrapoler"
        assert vinted_age.estimer(9999, t0 + 1015) is None and vinted_age.estimer("abc") is None
        vinted_age.observer([4100], t0 + 3010)          # 50 min de recul : 1 id/s
        vieux = vinted_age.estimer(100, t0 + 3015)
        assert abs(vieux - (t0 + 10 - 1000)) < 5, vieux
        # Redémarrage : les repères persistent, la première recherche n'est pas datée.
        vinted_age.reinitialiser()
        vinted_age.observer([4500], t0 + 9000)
        assert vinted_age.estimer(4500, t0 + 9000) is None
        assert abs(vinted_age.estimer(1600, t0 + 9000) - (t0 + 510)) < 1
        vinted_age.reinitialiser()


@test("Vinted : page servie sans annonces = blocage (on change de sortie), pas une erreur muette")
def _():
    import vinted_client

    class _Rep:
        def __init__(self, code, texte=""):
            self.status_code, self.text, self.cookies = code, texte, {}
        def json(self):
            raise ValueError("pas du JSON")

    def client(page):
        vc = vinted_client.VintedClient.__new__(vinted_client.VintedClient)
        vc.host, vc.domain, vc.currency, vc.verbose = "https://www.vinted.fr", "vinted.fr", "EUR", False
        vc.items_api = vc.host + "/api/v2/catalog/items"
        vc._cookie_ok = True
        vc._ensure_session = lambda force=False: None

        class _S:
            def get(self, url, **kw):
                return _Rep(200, page) if url.endswith("/catalog") else _Rep(404, "<title>La page n'existe pas</title>")
        vc.session = _S()
        return vc

    orig = vinted_client.time.sleep
    vinted_client.time.sleep = lambda *_: None
    try:
        for page, statut in (("<html>refonte</html>", 200),
                             ("<html><script src='https://geo.captcha-delivery.com/c.js'></script></html>", 403)):
            try:
                client(page).search(search_text="ddr4", max_retries=1)
                assert False, "doit lever"
            except vinted_client.VintedError as e:
                assert e.status == statut and ram_scrapers._bloque_vinted(e), (statut, e)
    finally:
        vinted_client.time.sleep = orig


@test("notification : un envoi Telegram raté repart en quelques secondes, pas après 5 minutes")
def _():
    import ram_sniper
    with ram_db.get_db() as conn:
        conn.execute("UPDATE ram_annonce SET statut='archive' WHERE statut='nouveau'")
    annonce_id, _n = ram_db.upsert_annonce({
        "source": "vinted", "url": "https://vinted.fr/reprise/1", "titre": "DDR4 32Go reprise",
        "prix_affiche": 40.0, "photos": [], "brut": {}})
    ram_db.maj_annonce(annonce_id, {"pre_score": 90.0, "statut": "nouveau"})
    appels, attentes = [], []
    origs = (ram_telegram.notifier_etape1, ram_telegram.anti_spam_ok, ram_telegram.role_instance,
             ram_sniper.ARRET.wait)

    def envoi(annonce, pre, cfg=None, chat_id=None):
        appels.append(annonce["id"])
        if len(appels) < 3:
            raise ram_telegram.TelegramError("HTTP 429 : Too Many Requests")
        ram_sniper.ARRET.set()                 # 3e essai réussi : fin du test

    def attendre(secondes=None):
        attentes.append(secondes)
        return ram_sniper.ARRET.is_set()

    ram_telegram.notifier_etape1, ram_telegram.anti_spam_ok = envoi, (lambda cfg=None: True)
    ram_telegram.role_instance = lambda cfg=None, force=False: ("principal", "test")
    ram_sniper.ARRET.wait = attendre
    ram_sniper.ARRET.clear()
    with ram_sniper._verrou_notif:
        ram_sniper._file_notif.clear()
    ram_sniper._essais_notif.clear()
    try:
        ram_sniper._enfiler_notification({"annonce": {"id": annonce_id}, "pre": {"pre_score": 90.0}})
        ram_sniper.worker_notification(ram_config.Config({}))
    finally:
        (ram_telegram.notifier_etape1, ram_telegram.anti_spam_ok, ram_telegram.role_instance,
         ram_sniper.ARRET.wait) = origs
        ram_sniper.ARRET.clear()
    assert appels == [annonce_id] * 3, appels
    assert attentes[:2] == [3, 6], attentes
    assert ram_db.get_annonce(annonce_id)["statut"] == "notifie"
    assert annonce_id not in ram_sniper._essais_notif

# ═══════════════════════════════════════════════════════════
print("\n" + "═" * 60)
total = RESULTATS["ok"] + RESULTATS["ko"]
if RESULTATS["ko"]:
    print(f"❌ {RESULTATS['ko']}/{total} test(s) en échec")
    for nom, err in RESULTATS["erreurs"]:
        print(f"\n── {nom} ──\n{err}")
    sys.exit(1)
print(f"✅ {RESULTATS['ok']}/{total} tests passés")
print("═" * 60)
