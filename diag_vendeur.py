"""
diag_vendeur.py — Test réseau + lecture des avis vendeurs sur de VRAIES annonces
═══════════════════════════════════════════════════════════════════════════
Répond à deux questions, avant de laisser tourner le bot quelque part :

  1. Vinted et Leboncoin répondent-ils depuis CETTE machine / ce serveur ?
     (depuis une IP de centre de données, Datadome répond souvent 403)
  2. Les avis vendeurs sont-ils bien lus ? Le script affiche les valeurs
     brutes pour que tu puisses les comparer avec ce que montre le site.

    python3 diag_vendeur.py            # les deux
    python3 diag_vendeur.py vinted     # une seule source
    python3 diag_vendeur.py leboncoin

Code de sortie : 0 si tout fonctionne, 1 sinon.
"""

import sys

import reseau

RECHERCHE = "ddr4 16go"


def tester_vinted():
    print("\n── VINTED ──────────────────────────────────────────────")
    from vinted_client import VintedClient, VintedError
    try:
        vc = VintedClient(country="FR")
        items = vc.search(search_text=RECHERCHE)
    except VintedError as e:
        print(f"❌ recherche impossible : {e}")
        if e.status in (401, 403):
            print("   → HTTP 403 : Vinted/Datadome bloque cette adresse IP.")
        return False
    except Exception as e:
        print(f"❌ erreur inattendue : {type(e).__name__}: {e}")
        return False
    print(f"✅ recherche « {RECHERCHE} » : {len(items)} annonce(s)")
    if not items:
        return True

    lus = 0
    for it in items[:4]:
        vid = it.get("vendeur_id")
        print(f"\n  {it['subject'][:55]}  ({it['price']}€)")
        print(f"    vendeur id={vid} pseudo={it.get('vendeur_pseudo')}")
        if not vid:
            print("    ⚠️ pas d'identifiant vendeur dans la réponse de recherche")
            continue
        try:
            info = vc.get_user_feedback(vid)
            print(f"    → {info['avis']} avis, note {info['note']}/5, "
                  f"+{info.get('positifs')} / -{info.get('negatifs')}")
            lus += 1
        except VintedError as e:
            print(f"    ❌ profil illisible : {e}")
    print(f"\n  Profils lus : {lus}/{min(4, len(items))}")
    return lus > 0


def tester_leboncoin():
    print("\n── LEBONCOIN ───────────────────────────────────────────")
    try:
        import lbc
    except ImportError:
        print("❌ module lbc absent (pip install -r requirements.txt)")
        return False
    try:
        client = reseau.client_lbc()
        res = client.search(text=RECHERCHE, page=1, limit=10, sort=lbc.Sort.NEWEST,
                            ad_type=lbc.AdType.OFFER,
                            category=lbc.Category.ELECTRONIQUE_ACCESSOIRES_INFORMATIQUE)
        annonces = res.ads or []
    except Exception as e:
        print(f"❌ recherche impossible : {type(e).__name__}: {e}")
        if "Datadome" in type(e).__name__:
            print("   → Datadome bloque cette adresse IP.")
        return False
    print(f"✅ recherche « {RECHERCHE} » : {len(annonces)} annonce(s)")

    lus = 0
    for ad in annonces[:4]:
        uid = getattr(ad, "_user_id", None)
        print(f"\n  {(ad.subject or '')[:55]}  ({ad.price}€)")
        print(f"    vendeur id={uid}")
        if not uid:
            print("    ⚠️ pas d'identifiant vendeur")
            continue
        try:
            u = client.get_user(uid)
            fb = u.feedback
            print(f"    → {u.name} : avis reçus={fb.received_count} "
                  f"note={fb.score} compte={u.account_type}")
            print(f"    brut feedback : {u.raw.get('feedback')}")
            lus += 1
        except Exception as e:
            print(f"    ❌ profil illisible : {type(e).__name__}: {e}")
    print(f"\n  Profils lus : {lus}/{min(4, len(annonces))}")
    return lus > 0


def main():
    cible = sys.argv[1].lower() if len(sys.argv) > 1 else "tout"
    if reseau.proxy_url():
        print("ℹ️  proxy sortant actif (SNIPER_PROXY_URL)")
    resultats = {}
    if cible in ("tout", "vinted"):
        resultats["Vinted"] = tester_vinted()
    if cible in ("tout", "leboncoin", "lbc"):
        resultats["Leboncoin"] = tester_leboncoin()

    print("\n══ VERDICT ══════════════════════════════════════════════")
    for nom, ok in resultats.items():
        print(f"  {'✅' if ok else '❌'} {nom}")
    if all(resultats.values()):
        print("\n  Tout fonctionne depuis cette adresse : le bot peut tourner ici.")
        return 0
    print("\n  Au moins une source est bloquée ou illisible depuis cette adresse.")
    print("  Si c'est un serveur : IP de centre de données → voir README §10")
    print("  (proxy résidentiel via SNIPER_PROXY_URL, ou machine chez toi).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
