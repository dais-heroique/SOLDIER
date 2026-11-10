"""
diag_vendeur.py — Test réseau par sortie + lecture des avis vendeurs
═══════════════════════════════════════════════════════════════════════════
Pour CHAQUE sortie réseau configurée (direct, maison, WARP, proxys — voir
reseau.py), vérifie :

  1. que Vinted et Leboncoin répondent par cette sortie
     (depuis une IP de centre de données, Datadome répond souvent 403) ;
  2. que les avis vendeurs sont bien lus (valeurs brutes affichées pour
     comparer avec ce que montre le site).

    python3 diag_vendeur.py            # Vinted + Leboncoin
    python3 diag_vendeur.py vinted     # une seule source
    python3 diag_vendeur.py leboncoin

Code de sortie : 0 si chaque source a AU MOINS une sortie qui fonctionne
(c'est tout ce qu'il faut au bot, grâce à la bascule automatique), 1 sinon.
Aucune adresse IP n'est affichée.
"""

import sys

import reseau

RECHERCHE = "ddr4 16go"
PROFILS_A_LIRE = 2


def tester_vinted(sortie):
    from vinted_client import VintedClient, VintedError
    try:
        vc = VintedClient(country="FR", proxy=sortie.url)
        items = vc.search(search_text=RECHERCHE, max_retries=2)
    except VintedError as e:
        motif = "bloqué par Datadome" if e.status in (401, 403) else reseau.masquer(e)
        print(f"    ❌ recherche : {motif}")
        corps = getattr(e, "body", None)
        if corps and e.status not in (401, 403):
            import re as _re
            titre = _re.search(r"<title>(.*?)</title>", corps, _re.S | _re.I)
            print(f"       réponse : {reseau.masquer((titre.group(1) if titre else corps))[:100].strip()}")
        return False
    except Exception as e:
        print(f"    ❌ recherche : {type(e).__name__}: {reseau.masquer(e)[:120]}")
        return False
    print(f"    ✅ recherche « {RECHERCHE} » : {len(items)} annonce(s)")
    if not items:
        return True
    lus = 0
    for it in items[:PROFILS_A_LIRE]:
        vid = it.get("vendeur_id")
        if not vid:
            print("    ⚠️ pas d'identifiant vendeur dans la réponse de recherche")
            continue
        try:
            info = vc.get_user_feedback(vid)
            print(f"    → vendeur {info.get('pseudo')} : {info['avis']} avis, note "
                  f"{info['note']}/5 (+{info.get('positifs')} / -{info.get('negatifs')})")
            lus += 1
        except VintedError as e:
            print(f"    ❌ profil illisible : {reseau.masquer(e)[:120]}")
    return lus > 0


def tester_leboncoin(sortie):
    try:
        import lbc
    except ImportError:
        print("    ❌ module lbc absent (pip install -r requirements.txt)")
        return False
    try:
        client = reseau.client_lbc(sortie, max_retries=1)
        res = client.search(text=RECHERCHE, page=1, limit=10, sort=lbc.Sort.NEWEST,
                            ad_type=lbc.AdType.OFFER,
                            category=lbc.Category.ELECTRONIQUE_ACCESSOIRES_INFORMATIQUE)
        annonces = res.ads or []
    except Exception as e:
        motif = ("bloqué par Datadome" if "Datadome" in type(e).__name__
                 else f"{type(e).__name__}: {reseau.masquer(e)[:120]}")
        print(f"    ❌ recherche : {motif}")
        return False
    print(f"    ✅ recherche « {RECHERCHE} » : {len(annonces)} annonce(s)")
    if not annonces:
        return True
    lus = 0
    for ad in annonces[:PROFILS_A_LIRE]:
        uid = getattr(ad, "_user_id", None)
        if not uid:
            print("    ⚠️ pas d'identifiant vendeur")
            continue
        try:
            u = client.get_user(uid)
            print(f"    → vendeur {u.name} : avis reçus={u.feedback.received_count} "
                  f"note={u.feedback.score} compte={u.account_type}")
            print(f"      brut : {u.raw.get('feedback')}")
            lus += 1
        except Exception as e:
            print(f"    ❌ profil illisible : {type(e).__name__}: {reseau.masquer(e)[:120]}")
    return lus > 0


TESTS = {"vinted": ("Vinted", tester_vinted), "leboncoin": ("Leboncoin", tester_leboncoin)}


def main():
    cible = sys.argv[1].lower() if len(sys.argv) > 1 else "tout"
    if cible == "lbc":
        cible = "leboncoin"
    sources = [s for s in TESTS if cible in ("tout", s)]
    sorties = reseau.sorties()
    print(f"Sorties testées : {', '.join(s.nom for s in sorties)}")

    resultats = {}
    for source in sources:
        nom, tester = TESTS[source]
        print(f"\n══ {nom.upper()} " + "═" * (50 - len(nom)))
        for sortie in sorties:
            print(f"\n  ▸ sortie « {sortie.nom} »")
            resultats[(source, sortie.nom)] = tester(sortie)

    print("\n══ VERDICT " + "═" * 46)
    largeur = max(len(s.nom) for s in sorties)
    print("  " + " " * 11 + "  ".join(s.nom.ljust(largeur) for s in sorties))
    tout_ok = True
    for source in sources:
        cases = ["✅".ljust(largeur) if resultats[(source, s.nom)] else "❌".ljust(largeur)
                 for s in sorties]
        ok = any(resultats[(source, s.nom)] for s in sorties)
        tout_ok &= ok
        print(f"  {TESTS[source][0]:<10} " + "  ".join(cases)
              + ("" if ok else "   ← AUCUNE sortie ne passe"))

    if tout_ok:
        print("\n  Chaque site a au moins une sortie qui fonctionne : le bot basculera"
              "\n  automatiquement sur celle-là si les autres sont bloquées.")
        return 0
    print("\n  Au moins un site est bloqué sur toutes les sorties testées."
          "\n  → Ajoute la sortie maison (Tailscale, gratuit) : README §10.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
