"""
ram_listing.py — Générateur d'annonces de revente
═══════════════════════════════════════════════════════════════════════════
À partir d'une entrée de stock (ou d'un kit), produit deux versions prêtes à
coller :
  • VINTED     — prix plus haut (c'est l'acheteur qui paie les frais), titre
                 saturé de mots-clés, envoi systématique.
  • LEBONCOIN  — prix net, retrait en main propre mis en avant.

Le titre reprend les mots que les acheteurs TAPENT réellement : capacité,
fréquence, CL, marque, « DDR4 », « PC Gamer ». Un titre qui commence par le
part number est invisible dans les résultats de recherche — le PN va dans la
description, où il rassure.

Le 3600 CL16-18 est l'optimum Ryzen 5000 (FCLK 1800 en 1:1). Cet argument
n'est pas décoratif : c'est lui qui fait acheter, il est donc généré
automatiquement dès que la fréquence s'y prête.
"""

import ram_config
import ram_db
import ram_scoring

# Compatibilités mises en avant selon la fréquence. Sur Ryzen 5000, le ratio
# 1:1 entre FCLK et mémoire décroche au-delà de 3600-3733 : au-dessus, la
# fréquence brute devient un argument Intel, pas AMD.
def _compatibilites(frequence, capacite_totale):
    lignes = []
    if frequence and frequence >= 3800:
        lignes.append("• Intel 12e/13e génération (DDR4) : profil XMP 1 clic")
        lignes.append("• AMD Ryzen : fonctionne, mais le ratio FCLK 1:1 se règle "
                      "plutôt autour de 3600 MHz")
    elif frequence and 3400 <= frequence <= 3733:
        lignes.append("• AMD Ryzen 5000 / AM4 : ★ fréquence idéale, FCLK 1800 en 1:1 "
                      "(le meilleur réglage pour ces processeurs)")
        lignes.append("• Intel 10e à 13e génération (cartes mères DDR4)")
    elif frequence and frequence >= 3000:
        lignes.append("• AMD Ryzen (AM4) et Intel 10e à 13e génération (DDR4)")
        lignes.append("• Profil XMP/DOCP à activer dans le BIOS pour la pleine fréquence")
    else:
        lignes.append("• Compatible toutes cartes mères DDR4 (AM4, LGA1200, LGA1700 DDR4)")
    if capacite_totale and capacite_totale >= 32:
        lignes.append("• 32 Go : confortable pour le jeu en streaming, la 3D et le montage vidéo")
    return lignes


def _titre(infos, plateforme):
    """Ordre volontaire : capacité → DDR4 → fréquence → CL → marque → usage.
    C'est l'ordre dans lequel les acheteurs tapent leur recherche."""
    bouts = []
    if infos.get("capacite_totale_go"):
        if (infos.get("nb_modules") or 1) > 1:
            bouts.append(f"{infos['capacite_totale_go']}Go "
                         f"({infos['nb_modules']}x{infos['capacite_module_go']}Go)")
        else:
            bouts.append(f"{infos['capacite_totale_go']}Go")
    bouts.append("DDR4")
    if infos.get("frequence_mhz"):
        bouts.append(f"{infos['frequence_mhz']}MHz")
    if infos.get("cas_latency"):
        bouts.append(f"CL{infos['cas_latency']}")
    if infos.get("marque"):
        bouts.append(infos["marque"])
    if infos.get("gamme") and infos["gamme"] not in ("OEM nue", "no-name"):
        bouts.append(infos["gamme"])
    if infos.get("rgb"):
        bouts.append("RGB")
    bouts.append("PC Gamer")
    titre = " ".join(bouts)
    # Vinted coupe à 100 caractères, Leboncoin à 50.
    limite = 100 if plateforme == "vinted" else 50
    if len(titre) > limite:
        essentiel = [b for b in bouts if b not in ("PC Gamer",)]
        titre = " ".join(essentiel)[:limite].rstrip()
    return titre


def _description(infos, plateforme, cfg=None):
    cfg = cfg or ram_config.get()
    l = []

    config = (f"{infos['nb_modules']}x{infos['capacite_module_go']} Go"
              if (infos.get("nb_modules") or 1) > 1
              else f"{infos.get('capacite_module_go')} Go")
    l.append(f"Mémoire DDR4 {config} ({infos.get('capacite_totale_go')} Go au total) "
             f"pour PC de bureau.")
    l.append("")

    l.append("CARACTÉRISTIQUES")
    if infos.get("marque") or infos.get("gamme"):
        l.append(f"• {infos.get('marque', '')} {infos.get('gamme', '')}".strip())
    if infos.get("part_number"):
        l.append(f"• Référence constructeur : {infos['part_number']}")
    if infos.get("frequence_mhz"):
        cl = f" CL{infos['cas_latency']}" if infos.get("cas_latency") else ""
        l.append(f"• Fréquence : {infos['frequence_mhz']} MHz{cl} (profil XMP)")
    if infos.get("rank"):
        rang = "double rang (2Rx8)" if infos["rank"] == "2Rx8" else "simple rang (1Rx8)"
        l.append(f"• Organisation : {rang}"
                 + (" — léger gain de performance sur Ryzen" if infos["rank"] == "2Rx8" else ""))
    if infos.get("die_type"):
        l.append(f"• Puces : {infos['die_type']} — très recherchées pour l'overclocking")
    l.append("• Format UDIMM 288 broches — PC de bureau (ce n'est PAS de la SO-DIMM portable)")
    l.append("• Non-ECC")
    if infos.get("low_profile"):
        l.append("• Profil bas : passe sous la grande majorité des ventirads")
    if infos.get("couleur"):
        l.append(f"• Coloris : {infos['couleur']}")
    l.append("")

    # Le test est le premier argument de vente : il se paie et il accélère.
    l.append("ÉTAT ET TESTS")
    if infos.get("memtest_ok") and infos.get("memtest_passes"):
        l.append(f"• Testé MemTest86 : {infos['memtest_passes']} passes SANS AUCUNE ERREUR "
                 f"(capture d'écran disponible sur demande)")
    elif infos.get("memtest_ok"):
        l.append("• Testé MemTest86 sans erreur (capture d'écran disponible sur demande)")
    if infos.get("xmp_stable"):
        l.append(f"• Profil XMP testé et stable à "
                 f"{infos.get('frequence_max_stable') or infos.get('frequence_mhz')} MHz")
    if infos.get("kit_assorti"):
        l.append("• Kit assorti d'origine : les barrettes ont été appairées en usine "
                 "(fonctionnement XMP garanti à deux)")
    elif infos.get("teste_ensemble"):
        l.append("• Les deux barrettes ont été testées ensemble au profil XMP")
    if not infos.get("memtest_ok"):
        l.append("• Testées et fonctionnelles sur banc avant mise en vente")
    l.append("")

    l.append("COMPATIBILITÉ")
    l += _compatibilites(infos.get("frequence_mhz"), infos.get("capacite_totale_go"))
    l.append("")

    if plateforme == "vinted":
        l.append("Envoi rapide et soigné (barrettes en pochette antistatique + "
                 "protection bulle). Envoi sous 24 h après réception du paiement.")
    else:
        l.append("Remise en main propre possible (test sur place bienvenu) ou "
                 "envoi soigné à votre charge.")
        l.append("Prix net, pas de frais supplémentaires.")

    if infos.get("no_name"):
        l.append("")
        l.append("Note : marque d'entrée de gamme, vendue au prix correspondant.")
    return "\n".join(l)


def prix_suggere(infos, plateforme, cfg=None):
    """Prix de référence ajusté des multiplicateurs, puis adapté à la
    plateforme.

    Sur Vinted, l'acheteur paie protection + port : à prix affiché égal, il
    dépense ~15 % de plus que sur Leboncoin. Afficher le même montant des deux
    côtés, c'est laisser de l'argent sur la table côté Vinted — ou se rendre
    invisible côté Leboncoin.
    """
    cfg = cfg or ram_config.get()
    ref = infos.get("ref")
    if not ref:
        return None

    analyse = {
        "nb_modules": infos.get("nb_modules"), "est_kit": bool(infos.get("kit_assorti")),
        "rgb": infos.get("rgb"), "blanc": "blanc" in (infos.get("couleur") or "").lower(),
        "memtest_prouve": bool(infos.get("memtest_ok")),
        "sans_boite": bool(infos.get("sans_boite")),
        "dissipateur_manquant": bool(infos.get("dissipateur_manquant")),
        "no_name": bool(infos.get("no_name")), "rank": infos.get("rank"),
    }
    valeur, details = ram_scoring.valeur_revente(ref, analyse, None, cfg)
    if valeur is None:
        return None

    # La valeur de revente est nette d'emballage : on la remet côté vendeur.
    valeur += float(cfg.val("frais.revente.emballage_eur", 1.20))

    if plateforme == "vinted":
        # Le prix Vinted peut être affiché ~8 % plus haut : l'acheteur compare
        # des prix affichés, et il a la protection acheteur en contrepartie.
        prix = valeur * 1.08
    else:
        prix = valeur * 0.95     # prix net, retrait possible : légèrement sous Vinted

    # Arrondi psychologique au multiple de 5 le plus proche, moins 1.
    arrondi = max(5, int(round(prix / 5.0) * 5) - 1)
    return {"prix": arrondi, "valeur_estimee": round(valeur, 2), "details": details}


def _infos_depuis_stock(stock_id):
    """Assemble les informations d'une ligne de stock et de sa référence."""
    with ram_db.get_db() as conn:
        ligne = conn.execute("SELECT * FROM ram_stock WHERE id=?", (stock_id,)).fetchone()
    if not ligne:
        raise ValueError(f"stock {stock_id} introuvable")
    stock = dict(ligne)
    ref = ram_db.get_reference(stock["ref_id"]) if stock.get("ref_id") else None
    if ref is None and stock.get("part_number"):
        ref = ram_db.find_reference_by_pn(stock["part_number"])

    nb_modules = 1
    kit_assorti = False
    if stock.get("kit_id"):
        with ram_db.get_db() as conn:
            kit = conn.execute("SELECT * FROM ram_kit WHERE id=?",
                               (stock["kit_id"],)).fetchone()
            n = conn.execute("SELECT COUNT(*) c FROM ram_stock WHERE kit_id=?",
                             (stock["kit_id"],)).fetchone()["c"]
        nb_modules = n or 1
        kit_assorti = bool(kit and kit["qualite"] == "parfait")
        teste_ensemble = bool(kit and kit["qualite"] == "batch_different")
    else:
        teste_ensemble = False

    capacite = stock.get("capacite_module_go") or (ref or {}).get("capacite_module_go")
    return {
        "stock_id": stock_id,
        "part_number": stock.get("part_number") or (ref or {}).get("part_number"),
        "marque": stock.get("marque") or (ref or {}).get("marque"),
        "gamme": stock.get("gamme") or (ref or {}).get("gamme"),
        "capacite_module_go": capacite,
        "nb_modules": nb_modules,
        "capacite_totale_go": (capacite or 0) * nb_modules,
        "frequence_mhz": stock.get("frequence_mhz") or (ref or {}).get("frequence_mhz"),
        "cas_latency": stock.get("cas_latency") or (ref or {}).get("cas_latency"),
        "rank": stock.get("rank") or (ref or {}).get("rank"),
        "die_type": (ref or {}).get("die_type"),
        "rgb": stock.get("rgb") if stock.get("rgb") is not None else (ref or {}).get("rgb"),
        "couleur": stock.get("couleur") or (ref or {}).get("couleur"),
        "low_profile": (ref or {}).get("low_profile"),
        "memtest_ok": stock.get("memtest_ok"),
        "memtest_passes": stock.get("memtest_passes"),
        "xmp_stable": stock.get("xmp_stable"),
        "frequence_max_stable": stock.get("frequence_max_stable"),
        "kit_assorti": kit_assorti, "teste_ensemble": teste_ensemble,
        "no_name": (ref or {}).get("tier") == "D",
        "prix_revient": stock.get("prix_revient"),
        "ref": ref,
    }


def generer(stock_id=None, infos=None, cfg=None):
    """Retourne {'vinted': {...}, 'leboncoin': {...}} + les informations
    communes. `infos` permet de générer sans passer par le stock (simulation)."""
    cfg = cfg or ram_config.get()
    infos = infos or _infos_depuis_stock(stock_id)

    sortie = {"infos": infos, "versions": {}}
    for plateforme in ("vinted", "leboncoin"):
        prix = prix_suggere(infos, plateforme, cfg)
        revient = float(infos.get("prix_revient") or 0)
        marge = round(prix["prix"] - revient, 2) if prix and revient else None
        sortie["versions"][plateforme] = {
            "titre": _titre(infos, plateforme),
            "description": _description(infos, plateforme, cfg),
            "prix": prix["prix"] if prix else None,
            "valeur_estimee": prix["valeur_estimee"] if prix else None,
            "ajustements": prix["details"] if prix else [],
            "marge_si_vendu": marge,
        }
    return sortie


if __name__ == "__main__":
    ram_db.init_db()
    ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
    infos = {
        "part_number": ref["part_number"], "marque": ref["marque"], "gamme": ref["gamme"],
        "capacite_module_go": 16, "nb_modules": 2, "capacite_totale_go": 32,
        "frequence_mhz": 3200, "cas_latency": 16, "rank": ref["rank"],
        "die_type": ref["die_type"], "rgb": ref["rgb"], "couleur": ref["couleur"],
        "low_profile": ref["low_profile"], "memtest_ok": 1, "memtest_passes": 8,
        "xmp_stable": 1, "frequence_max_stable": 3200, "kit_assorti": True,
        "prix_revient": 51.95, "ref": ref,
    }
    sortie = generer(infos=infos)
    for plateforme, v in sortie["versions"].items():
        print("═" * 68)
        print(f"  {plateforme.upper()}   —   {v['prix']} €   "
              f"(marge {v['marge_si_vendu']} €)")
        print("═" * 68)
        print(f"TITRE ({len(v['titre'])} car.) : {v['titre']}\n")
        print(v["description"])
        print()

    ref3600 = ram_db.find_reference_by_pn("CMK32GX4M2D3600C18")
    infos3600 = dict(infos, part_number=ref3600["part_number"], frequence_mhz=3600,
                     cas_latency=18, ref=ref3600, gamme=ref3600["gamme"])
    v = generer(infos=infos3600)["versions"]["vinted"]
    print("═" * 68)
    print("  ARGUMENT RYZEN AUTOMATIQUE (3600 CL18)")
    print("═" * 68)
    print(f"TITRE : {v['titre']}")
    print([l for l in v["description"].split("\n") if "FCLK" in l])
