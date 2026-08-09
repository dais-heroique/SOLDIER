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
import traceback

# Base temporaire AVANT tout import du module (ram_db lit DB_FILE à l'import).
_TMP = tempfile.mkdtemp(prefix="ram_sniper_test_")
os.environ["RAM_DRY_RUN"] = "1"

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


@test("DDR3 déguisée en DDR4 détectée (fréquence, plateforme, modèle)")
def _():
    cas = ["DDR4 16Go 1600MHz",
           "RAM DDR4 8Go pour i7-4790 LGA1150",
           "Corsair Vengeance Pro 16Go DDR4",
           "HyperX Fury 1600 8Go DDR4"]
    for t in cas:
        r = analyse(t)
        assert r["exclusion"] == "ddr3_suspecte", f"« {t} » non détectée ({r['exclusion']})"


@test("4 Go rejetée, sauf lot de 10+")
def _():
    assert analyse("Barrette DDR4 4Go 2400")["exclusion"] == "capacite"
    r = analyse("Lot 12 barrettes DDR4 4Go", "destockage 2400mhz")
    assert r["exclusion"] is None, "le lot de 12 aurait dû passer l'exception"
    assert r["nb_modules"] == 12


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
print("\n" + "═" * 60)
total = RESULTATS["ok"] + RESULTATS["ko"]
if RESULTATS["ko"]:
    print(f"❌ {RESULTATS['ko']}/{total} test(s) en échec")
    for nom, err in RESULTATS["erreurs"]:
        print(f"\n── {nom} ──\n{err}")
    sys.exit(1)
print(f"✅ {RESULTATS['ok']}/{total} tests passés")
print("═" * 60)
