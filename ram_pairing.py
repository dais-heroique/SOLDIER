"""
ram_pairing.py — Arbitrage d'appariement de kits
═══════════════════════════════════════════════════════════════════════════
LA plus grosse source de marge du système, et celle que personne ne fait
sérieusement sur Vinted : deux barrettes 16 Go identiques achetées 35 € pièce
(70 €) se revendent en kit assorti 32 Go autour de 120 €.

Hiérarchie d'appariement — dans cet ordre, sans exception :
  1. PN identique + même code de semaine  → « kit parfait », vendable comme
     kit assorti d'origine, XMP stable garanti (même batch).
  2. PN identique, batch différent        → vendable, mentionner
     « testé ensemble à XMP » dans l'annonce.
  3. Même fréquence/CL mais PN différent  → NE JAMAIS vendre comme kit.
     Désactivé par défaut (appariement.autoriser_specs_seules: false) et,
     même activé, l'appariement est marqué 'specs_seules' pour ne jamais
     produire une annonce mensongère.
"""

import time

import ram_config
import ram_db
import ram_scoring


def prix_cible(stock, ref, cfg=None):
    """Prix maximum à payer pour la seconde barrette pour que le kit complet
    tienne la marge minimale.

    Raisonnement : le kit assorti se vend `revente_kit`. On a déjà engagé
    `prix_revient` sur la première barrette. Il reste donc au plus
    revente_kit − prix_revient − marge_min à mettre sur la seconde.
    """
    cfg = cfg or ram_config.get()
    marge_min = float(cfg.val("appariement.marge_kit_min_eur", 30))
    revente_kit = revente_du_kit(ref, cfg)
    if revente_kit is None:
        return None, None
    deja_engage = float(stock.get("prix_revient") or stock.get("prix_achat") or 0)
    cible = revente_kit - deja_engage - marge_min
    return (round(max(cible, 0.0), 2), revente_kit)


def revente_du_kit(ref, cfg=None):
    """Valeur de revente du kit de 2 barrettes construit à partir de `ref`.

    Trois cas, dans cet ordre de fiabilité :
      1. La référence EST déjà un kit de 2 → son prix de référence est la réponse.
      2. Il existe en base un kit de 2 aux mêmes caractéristiques → c'est LUI
         qui donne le prix, parce que c'est un prix réellement observé sur le
         marché. Extrapoler « unitaire × 2 × 1,25 » alors qu'un prix de kit
         existe, c'est se fabriquer un objectif de vente que personne ne paiera :
         sur du 32 Go unitaire à 105 €, l'extrapolation donne 262 € quand le kit
         2×32 réel se négocie 235 €.
      3. Aucun kit de référence → extrapolation avec la prime de kit assorti.
    """
    cfg = cfg or ram_config.get()
    if not ref:
        return None
    prix = float(ref["prix_ref_occasion_eur"])
    nb = int(ref["nb_modules"] or 1)
    if nb == 2:
        return round(prix, 2)

    kits = ram_db.find_references_by_specs(
        capacite_module=ref["capacite_module_go"], nb_modules=2,
        frequence=ref["frequence_mhz"], cas_latency=ref["cas_latency"],
        marque=ref["marque"], limit=3)
    if not kits:
        kits = ram_db.find_references_by_specs(
            capacite_module=ref["capacite_module_go"], nb_modules=2,
            frequence=ref["frequence_mhz"], cas_latency=ref["cas_latency"], limit=3)
    if kits:
        return round(float(kits[0]["prix_ref_occasion_eur"]), 2)

    unitaire = prix / nb
    bonus = 1 + float(cfg.val("appariement.bonus_kit_pct", 25)) / 100.0
    return round(unitaire * 2 * bonus, 2)


def chercher_appariements(annonce, analyse=None, cfg=None):
    """Cherche, pour une annonce qui vient d'être détectée, une barrette
    unitaire en stock qu'elle viendrait compléter.

    Retourne la liste des appariements créés (dicts prêts pour la notification).
    """
    cfg = cfg or ram_config.get()
    if not cfg.val("appariement.actif", True):
        return []

    pn_annonce = annonce.get("pn_normalise")
    if not pn_annonce:
        return []

    # Une annonce qui vend déjà un kit complet ne complète rien.
    if (annonce.get("nb_modules") or 1) > 1:
        return []

    statuts = cfg.val("appariement.statuts_stock_eligibles",
                      ["recu", "en_test", "teste_ok", "liste"])
    autoriser_specs = bool(cfg.val("appariement.autoriser_specs_seules", False))
    marge_min = float(cfg.val("appariement.marge_kit_min_eur", 30))

    candidats = []
    with ram_db.get_db() as conn:
        marques = ",".join("?" * len(statuts))
        lignes = conn.execute(f"""
            SELECT s.*, r.prix_ref_occasion_eur, r.nb_modules AS ref_nb_modules,
                   r.tier AS ref_tier, r.gamme AS ref_gamme, r.marque AS ref_marque,
                   r.liquidite, r.delai_rotation_jours, r.rgb, r.couleur,
                   r.low_profile, r."rank" AS ref_rank, r.capacite_module_go
                       AS ref_capacite, r.frequence_mhz AS ref_frequence,
                   r.cas_latency AS ref_cl
            FROM ram_stock s
            LEFT JOIN ram_reference r ON r.id = s.ref_id
            WHERE s.kit_id IS NULL AND s.statut IN ({marques})
        """, statuts).fetchall()
        stock_libre = [dict(l) for l in lignes]

    for stock in stock_libre:
        pn_stock = stock.get("pn_normalise")
        type_appariement = None

        if pn_stock and pn_stock == pn_annonce:
            meme_semaine = bool(stock.get("code_semaine")
                                and stock["code_semaine"] == annonce.get("code_semaine"))
            type_appariement = "parfait" if meme_semaine else "batch_different"
        elif autoriser_specs and _memes_specs(stock, annonce):
            type_appariement = "specs_seules"
            meme_semaine = False
        else:
            continue

        ref = ram_db.get_reference(stock["ref_id"]) if stock.get("ref_id") else None
        if ref is None:
            ref = ram_db.find_reference_by_pn(pn_stock or "")
        if ref is None:
            continue

        cible, revente_kit = prix_cible(stock, ref, cfg)
        if cible is None:
            continue

        prix_annonce = float(annonce.get("prix_total") or 0)
        if prix_annonce > cible:
            continue      # trop cher pour que le kit reste rentable

        revient_kit = round(float(stock.get("prix_revient") or 0) + prix_annonce, 2)
        marge_kit = round((revente_kit or 0) - revient_kit, 2)
        if marge_kit < marge_min:
            continue

        # Gain réel de l'appariement vs revente des deux barrettes séparément.
        unitaire = ram_scoring.valeur_revente(
            ref, {"nb_modules": 1, "est_kit": False}, None, cfg)[0] or 0
        bonus = round((revente_kit or 0) - unitaire * 2, 2)

        appariement = {
            "stock_id": stock["id"], "annonce_id": annonce["id"],
            "part_number": stock.get("part_number") or annonce.get("pn_detecte"),
            "type_appariement": type_appariement,
            "meme_code_semaine": int(bool(meme_semaine)),
            "prix_cible": cible, "prix_kit_revient": revient_kit,
            "prix_kit_revente": revente_kit, "marge_kit_estimee": marge_kit,
            "bonus_kit_eur": bonus, "statut": "candidat",
        }
        appariement_id = ram_db.creer_appariement(appariement)
        if appariement_id:
            appariement["id"] = appariement_id
            appariement["stock_pn"] = stock.get("part_number")
            appariement["stock_semaine"] = stock.get("code_semaine")
            candidats.append(appariement)

    candidats.sort(key=lambda a: a["marge_kit_estimee"], reverse=True)
    return candidats


def _memes_specs(stock, annonce):
    """Mêmes caractéristiques mais PN différent : jamais un kit assorti, tout
    au plus une paire fonctionnelle. Réservé au mode explicite."""
    return (stock.get("capacite_module_go") == annonce.get("capacite_module_go")
            and stock.get("frequence_mhz") == annonce.get("frequence_mhz")
            and stock.get("cas_latency") == annonce.get("cas_latency")
            and all(x is not None for x in (stock.get("capacite_module_go"),
                                            annonce.get("capacite_module_go"))))


def radar_kits(cfg=None):
    """Vue « Radar kits » du dashboard : barrettes unitaires en stock avec,
    pour chacune, son prix cible et les candidats détectés."""
    cfg = cfg or ram_config.get()
    statuts = cfg.val("appariement.statuts_stock_eligibles",
                      ["recu", "en_test", "teste_ok", "liste"])
    lignes = []
    for stock in ram_db.list_stock(non_apparie=True):
        if stock.get("statut") not in statuts:
            continue
        ref = ram_db.get_reference(stock["ref_id"]) if stock.get("ref_id") else None
        cible, revente_kit = prix_cible(stock, ref, cfg) if ref else (None, None)
        with ram_db.get_db() as conn:
            candidats = conn.execute("""
                SELECT p.*, a.titre, a.url, a.prix_total, a.source
                FROM ram_appariement p JOIN ram_annonce a ON a.id=p.annonce_id
                WHERE p.stock_id=? AND p.statut IN ('candidat','notifie')
                ORDER BY p.marge_kit_estimee DESC LIMIT 5
            """, (stock["id"],)).fetchall()
        lignes.append({
            "stock": stock, "prix_cible": cible, "revente_kit": revente_kit,
            "candidats": [dict(c) for c in candidats],
        })
    lignes.sort(key=lambda x: len(x["candidats"]), reverse=True)
    return lignes


def assembler_kit(stock_ids, nom=None, cfg=None):
    """Assemble plusieurs lignes de stock en un kit. Vérifie la cohérence :
    assembler deux PN différents et le vendre comme « kit assorti » est
    exactement ce qu'on reproche aux autres vendeurs."""
    cfg = cfg or ram_config.get()
    if len(stock_ids) < 2:
        raise ValueError("un kit demande au moins 2 barrettes")

    with ram_db.get_db() as conn:
        marques = ",".join("?" * len(stock_ids))
        lignes = [dict(r) for r in conn.execute(
            f"SELECT * FROM ram_stock WHERE id IN ({marques})", list(stock_ids)).fetchall()]

    if len(lignes) != len(stock_ids):
        raise ValueError("barrette introuvable en stock")
    if any(l.get("kit_id") for l in lignes):
        raise ValueError("une des barrettes appartient déjà à un kit")

    pns = {l.get("pn_normalise") for l in lignes}
    semaines = {l.get("code_semaine") for l in lignes if l.get("code_semaine")}
    # « Kit parfait » exige que TOUTES les barrettes portent un code de semaine
    # et que ce soit le même : un code manquant ne vaut pas une correspondance.
    toutes_datees = all(l.get("code_semaine") for l in lignes)
    if len(pns) == 1 and toutes_datees and len(semaines) == 1:
        qualite = "parfait"
    elif len(pns) == 1:
        qualite = "batch_different"
    else:
        qualite = "heterogene"

    ref = None
    for l in lignes:
        if l.get("ref_id"):
            ref = ram_db.get_reference(l["ref_id"])
            break

    revient = round(sum(float(l.get("prix_revient") or 0) for l in lignes), 2)
    cible = revente_du_kit(ref, cfg) if ref else None
    premiere = lignes[0]

    kit_id = ram_db.creer_kit({
        "nom": nom or f"Kit {premiere.get('part_number')} ×{len(lignes)}",
        "part_number": premiere.get("part_number"),
        "nb_modules": len(lignes),
        "capacite_module_go": premiere.get("capacite_module_go"),
        "capacite_totale_go": (premiere.get("capacite_module_go") or 0) * len(lignes),
        "frequence_mhz": premiere.get("frequence_mhz"),
        "cas_latency": premiere.get("cas_latency"),
        "meme_batch": int(qualite == "parfait"),
        "code_semaine": next(iter(semaines), None) if len(semaines) == 1 else None,
        "qualite": qualite, "prix_revient_total": revient, "prix_cible": cible,
        "statut": "assemble",
    }, stock_ids=stock_ids)

    return {"kit_id": kit_id, "qualite": qualite, "prix_revient": revient,
            "prix_cible": cible,
            "vendable_comme_kit_assorti": qualite in ("parfait", "batch_different")}


if __name__ == "__main__":
    import ram_parser

    ram_db.init_db()
    print("── Simulation d'appariement ──\n")

    ref = ram_db.find_reference_by_pn("CMK32GX4M1E3200C16")
    print(f"Référence : {ref['part_number']} — barrette 32 Go unitaire à "
          f"{ref['prix_ref_occasion_eur']:.0f}€")
    print(f"Revente du kit de 2 : {revente_du_kit(ref):.0f}€")

    stock_id = ram_db.creer_stock({
        "part_number": "CMK32GX4M1E3200C16", "ref_id": ref["id"], "marque": "Corsair",
        "capacite_module_go": 32, "frequence_mhz": 3200, "cas_latency": 16,
        "code_semaine": "2134", "prix_achat": 78.0, "frais_port": 4.0,
        "frais_protection": 4.60, "statut": "teste_ok", "source": "vinted",
    })
    stock = ram_db.list_stock()[0]
    cible, revente = prix_cible(stock, ref)
    print(f"Déjà engagé : {stock['prix_revient']:.2f}€")
    print(f"→ prix cible pour la 2e barrette : {cible:.2f}€ "
          f"(kit revendable {revente:.0f}€, marge min 30€)\n")

    for prix, semaine in ((72.0, "2134"), (72.0, "2210"), (110.0, "2134")):
        annonce_id, _ = ram_db.upsert_annonce({
            "source": "vinted", "url": f"https://vinted.fr/items/{int(time.time()*1000)}{prix}",
            "titre": "Corsair Vengeance LPX 32Go DDR4 3200 CMK32GX4M1E3200C16",
            "prix_affiche": prix, "frais_port": 4.0, "frais_protection": prix * 0.05 + 0.7,
            "pn_detecte": "CMK32GX4M1E3200C16",
            "pn_normalise": ram_db.normalize_pn("CMK32GX4M1E3200C16"),
            "capacite_module_go": 32, "nb_modules": 1, "frequence_mhz": 3200,
            "cas_latency": 16, "photos": ["https://x/1.jpg"],
        })
        annonce = ram_db.get_annonce(annonce_id)
        annonce["code_semaine"] = semaine
        resultats = chercher_appariements(annonce)
        if resultats:
            a = resultats[0]
            print(f"  {prix:.0f}€ semaine {semaine} → ✅ {a['type_appariement']} · "
                  f"marge kit {a['marge_kit_estimee']:.0f}€ · "
                  f"bonus vs séparé +{a['bonus_kit_eur']:.0f}€")
        else:
            print(f"  {prix:.0f}€ semaine {semaine} → ❌ pas d'appariement rentable "
                  f"(cible {cible:.0f}€)")

    print("\n── Assemblage ──")
    stock_id2 = ram_db.creer_stock({
        "part_number": "CMK32GX4M1E3200C16", "ref_id": ref["id"], "marque": "Corsair",
        "capacite_module_go": 32, "frequence_mhz": 3200, "cas_latency": 16,
        "code_semaine": "2134", "prix_achat": 72.0, "frais_port": 4.0,
        "frais_protection": 4.30, "statut": "teste_ok", "source": "vinted",
    })
    print(assembler_kit([stock_id, stock_id2]))
