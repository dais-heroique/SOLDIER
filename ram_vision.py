"""
ram_vision.py — Couche vision : VisionProvider / GeminiProvider
═══════════════════════════════════════════════════════════════════════════
L'appel au fournisseur est isolé derrière une interface `VisionProvider` :
changer de modèle ou de fournisseur ne touche à rien d'autre dans le code.

Ce que gère ce module, au-delà de l'appel :
  • File de priorité   — seules les annonces au pré-score ≥ seuil_vision sont
                         analysées, meilleurs scores d'abord (ram_vision_file)
  • Quota persisté     — compteurs minute + jour en base, réinitialisation
                         automatique par changement de fenêtre
  • Dégradation propre — quota épuisé : l'annonce reste ⚡ NON VÉRIFIÉ avec la
                         mention « quota vision épuisé ». Jamais perdue.
  • Rattrapage         — les différées repassent en file au renouvellement du
                         quota, si l'annonce est encore en ligne
  • Cache              — clé = url + hash des photos : jamais deux analyses
                         pour le même jeu de photos
  • Photos             — 3 au maximum (la première + les 2 plus grandes),
                         redimensionnées avant envoi

Le parsing de la réponse est DÉFENSIF : un modèle qui répond avec des
backticks, un préambule ou un champ manquant ne doit jamais faire tomber le
worker — au pire l'annonce ressort en 🔍 À VÉRIFIER.
"""

import base64
import io
import json
import re
import time
import urllib.error
import urllib.request

import ram_config
import ram_db

# ─────────────────────── PROMPT ───────────────────────
# Écrit pour un modèle qui voit 1 à 3 photos. Les points de contrôle sont
# ordonnés du plus discriminant au moins discriminant : la position de
# l'encoche d'abord, parce que c'est le seul signal fiable pour distinguer
# une DDR3 d'une DDR4 quand le sticker ment ou est absent.
PROMPT_GEMINI = """Tu es un expert en identification de barrettes de mémoire RAM d'occasion.
Analyse la ou les photos fournies et réponds UNIQUEMENT par un objet JSON valide.

CONTEXTE DE L'ANNONCE (peut être faux, c'est justement ce qu'on vérifie) :
{contexte}

POINTS DE CONTRÔLE, DANS CET ORDRE :

1. POSITION DE L'ENCOCHE sur le connecteur doré (LE discriminant le plus fiable) :
   - DDR4 : encoche nettement DÉCENTRÉE, plus proche du centre de la barrette,
     et le bord inférieur du PCB est légèrement incurvé (plus épais au centre).
   - DDR3 : encoche plus proche d'une extrémité, bord inférieur parfaitement droit.
   Si la photo ne montre pas le connecteur, mets generation_suspectee à "INCONNU"
   plutôt que de deviner.

2. LONGUEUR DE LA BARRETTE :
   - UDIMM desktop : longue (~133 mm), 288 contacts.
   - SO-DIMM portable : environ moitié moins longue, 260 contacts → est_sodimm = true.

3. NOMBRE DE PUCES visibles sur une face :
   - 8 ou 16 puces → non-ECC (dans le périmètre).
   - 9 ou 18 puces → ECC → est_ecc = true.
   - Une puce isolée AU CENTRE de la barrette, différente des autres (puce de
     registre) → est_registered = true (RDIMM).

4. STICKER :
   - Lis le part number EXACTEMENT tel qu'imprimé, sans rien corriger ni compléter.
   - Cherche un code de semaine de production (souvent 4 chiffres type "2134"
     ou une date) → code_semaine.
   - Signaux de relabellisation : police incohérente, sticker mal aligné,
     sticker manifestement collé PAR-DESSUS un autre, fautes d'orthographe
     → sticker_authentique = false.
   - Sticker absent ou illisible → part_number_lu = null et photo_lisible = false.

5. CONTACTS DORÉS : "propre", "oxyde", "raye" ou "brule".

6. DISSIPATEUR : "bon", "abime", "manquant" (barrette nue = "manquant").

7. COHÉRENCE GLOBALE : le part number lu correspond-il à la capacité et à la
   fréquence annoncées dans le texte de l'annonce ? Toute incohérence va dans
   "drapeaux" sous forme d'une phrase courte en français.

RÉPONDS AVEC EXACTEMENT CE SCHÉMA JSON, SANS MARKDOWN, SANS BACKTICKS,
SANS TEXTE AVANT OU APRÈS :
{{
  "est_ddr4_desktop": true,
  "generation_suspectee": "DDR4",
  "est_sodimm": false,
  "est_ecc": false,
  "est_registered": false,
  "part_number_lu": "CMK32GX4M2E3200C16",
  "marque": "Corsair",
  "nb_barrettes_visibles": 2,
  "capacite_par_barrette": "16GB",
  "nb_puces_par_face": 8,
  "code_semaine": null,
  "sticker_authentique": true,
  "etat_contacts": "propre",
  "etat_dissipateur": "bon",
  "rgb": false,
  "couleur": "noir",
  "hauteur_estimee": "low_profile",
  "photo_lisible": true,
  "drapeaux": [],
  "confiance": 0.88
}}

Règles de remplissage :
- "generation_suspectee" : "DDR4", "DDR3", "DDR5" ou "INCONNU".
- "hauteur_estimee" : "low_profile" (≤ 34 mm, pas de dissipateur haut),
  "standard" ou "haut" (dissipateur imposant type Dominator).
- "confiance" : 0.0 à 1.0 — ta certitude globale sur l'identification.
  Sois SÉVÈRE : une photo floue ou un sticker partiellement masqué doit
  descendre sous 0.5. Il vaut mieux un doute qu'une fausse certitude.
- Ne devine JAMAIS un part number : mieux vaut null."""


class VisionError(Exception):
    pass


class QuotaEpuise(VisionError):
    pass


# ─────────────────────── SCHÉMA DE SORTIE ───────────────────────
_SCHEMA = {
    "est_ddr4_desktop": bool, "generation_suspectee": str, "est_sodimm": bool,
    "est_ecc": bool, "est_registered": bool, "part_number_lu": str, "marque": str,
    "nb_barrettes_visibles": int, "capacite_par_barrette": str,
    "nb_puces_par_face": int, "code_semaine": str, "sticker_authentique": bool,
    "etat_contacts": str, "etat_dissipateur": str, "rgb": bool, "couleur": str,
    "hauteur_estimee": str, "photo_lisible": bool, "drapeaux": list, "confiance": float,
}

_BACKTICKS = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I | re.M)


def parser_reponse(brut):
    """Parsing défensif d'une réponse de modèle.

    Un LLM à qui on demande du JSON pur renvoie parfois des backticks, un
    « Voici le JSON : » en préambule, ou des virgules traînantes. Aucun de ces
    cas ne doit faire tomber le worker : on nettoie, on tente, et en dernier
    recours on extrait la première accolade équilibrée.

    Retourne (dict normalisé, erreur|None). Un dict est TOUJOURS retourné.
    """
    if not brut or not str(brut).strip():
        return _normaliser({}, complet=False), "réponse vide"

    texte = _BACKTICKS.sub("", str(brut)).strip()
    data = None
    erreur = None

    try:
        data = json.loads(texte)
    except (ValueError, TypeError):
        # Repli : première accolade équilibrée du texte.
        debut = texte.find("{")
        if debut >= 0:
            profondeur = 0
            for i, c in enumerate(texte[debut:], start=debut):
                if c == "{":
                    profondeur += 1
                elif c == "}":
                    profondeur -= 1
                    if profondeur == 0:
                        fragment = texte[debut:i + 1]
                        fragment = re.sub(r",\s*([}\]])", r"\1", fragment)  # virgules traînantes
                        try:
                            data = json.loads(fragment)
                        except (ValueError, TypeError) as e:
                            erreur = f"JSON invalide : {e}"
                        break
        if data is None and not erreur:
            erreur = "aucun objet JSON trouvé dans la réponse"

    if not isinstance(data, dict):
        return _normaliser({}, complet=False), erreur or "réponse non structurée"
    return _normaliser(data, complet=True), None


def _coerce(valeur, attendu):
    if valeur is None:
        return None
    try:
        if attendu is bool:
            if isinstance(valeur, str):
                return valeur.strip().lower() in ("true", "vrai", "oui", "yes", "1")
            return bool(valeur)
        if attendu is int:
            return int(float(valeur))
        if attendu is float:
            return float(valeur)
        if attendu is list:
            if isinstance(valeur, list):
                return [str(x) for x in valeur]
            return [str(valeur)] if valeur else []
        valeur = str(valeur).strip()
        return valeur or None
    except (TypeError, ValueError):
        return None


def _normaliser(data, complet=True):
    """Force le schéma : tout champ absent ou mal typé devient None (ou [] pour
    les listes). Le reste du code peut alors lire sans jamais tester le type."""
    out = {}
    for cle, attendu in _SCHEMA.items():
        out[cle] = _coerce(data.get(cle), attendu)
    if out["drapeaux"] is None:
        out["drapeaux"] = []
    if out["confiance"] is not None:
        out["confiance"] = max(0.0, min(1.0, out["confiance"]))
    if out["generation_suspectee"]:
        out["generation_suspectee"] = out["generation_suspectee"].upper()
    # Une réponse sans aucun champ exploitable ne vaut pas mieux qu'une absence
    # de réponse : on ne laisse pas passer une confiance héritée.
    if not complet:
        out["confiance"] = None
        out["photo_lisible"] = None
    return out


# ─────────────────────── PHOTOS ───────────────────────
def choisir_photos(urls, maximum=3):
    """La première photo (celle que le vendeur a choisie comme vitrine, souvent
    la plus nette) + les suivantes. On ne peut pas connaître la taille réelle
    sans télécharger : Vinted et Leboncoin exposent la résolution dans l'URL,
    on s'en sert quand elle est présente."""
    urls = [u for u in (urls or []) if u]
    if len(urls) <= maximum:
        return urls

    def taille_devinee(u):
        tailles = [int(x) for x in re.findall(r"(?:^|[^0-9])(\d{3,4})x(?:\d{3,4})", u)]
        return max(tailles) if tailles else 0

    reste = sorted(urls[1:], key=taille_devinee, reverse=True)
    return [urls[0]] + reste[:maximum - 1]


def telecharger_photo(url, max_octets=3_500_000, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        donnees = r.read(max_octets + 1)
        if len(donnees) > max_octets:
            raise VisionError(f"photo trop lourde (> {max_octets} octets)")
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        raise VisionError(f"contenu non-image ({mime})")
    return donnees, mime


def redimensionner(donnees, mime, max_px=1024):
    """Redimensionne si Pillow est disponible. Sans Pillow on envoie l'image
    telle quelle : ça consomme plus de quota d'entrée mais ça marche — une
    dépendance optionnelle ne doit pas bloquer la chaîne."""
    try:
        from PIL import Image
    except ImportError:
        return donnees, mime
    try:
        img = Image.open(io.BytesIO(donnees))
        if max(img.size) <= max_px:
            return donnees, mime
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        tampon = io.BytesIO()
        img.save(tampon, format="JPEG", quality=85, optimize=True)
        return tampon.getvalue(), "image/jpeg"
    except Exception:
        return donnees, mime


# ─────────────────────── INTERFACE ───────────────────────
class VisionProvider:
    """Interface. Une implémentation doit fournir `analyser(photos, contexte)`
    et retourner (dict normalisé, meta) ou lever VisionError/QuotaEpuise."""

    nom = "abstrait"

    def analyser(self, photos, contexte):
        raise NotImplementedError

    def disponible(self):
        return False


class GeminiProvider(VisionProvider):
    """Appel de l'API Gemini via urllib — pas de SDK à installer.

    L'endpoint et le modèle sont configurables (ram_config.yaml → vision.modele)
    parce que la nomenclature des modèles gratuits change régulièrement.
    """

    nom = "gemini"
    ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{modele}:generateContent")

    def __init__(self, cle_api=None, modele=None, cfg=None):
        self.cfg = cfg or ram_config.get()
        self.cle_api = cle_api or ram_config.secret("GEMINI_API_KEY")
        self.modele = modele or self.cfg.val("vision.modele", "gemini-2.0-flash")
        self.timeout = int(self.cfg.val("vision.timeout_s", 25))
        self.temperature = float(self.cfg.val("vision.temperature", 0.1))

    def disponible(self):
        return bool(self.cle_api)

    def analyser(self, photos, contexte):
        """photos = [(octets, mime)]. Retourne (dict normalisé, meta)."""
        if not self.disponible():
            raise VisionError("GEMINI_API_KEY absente")
        if not photos:
            raise VisionError("aucune photo exploitable")

        parties = [{"text": PROMPT_GEMINI.format(contexte=contexte)}]
        for octets, mime in photos:
            parties.append({"inline_data": {
                "mime_type": mime,
                "data": base64.b64encode(octets).decode("ascii"),
            }})

        corps = json.dumps({
            "contents": [{"parts": parties}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": 900,
                # Force une sortie JSON côté API : première ligne de défense,
                # le parsing défensif reste la seconde.
                "responseMimeType": "application/json",
            },
        }).encode("utf-8")

        url = self.ENDPOINT.format(modele=self.modele)
        req = urllib.request.Request(
            url, data=corps,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.cle_api},
            method="POST")

        debut = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                reponse = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            if e.code == 429:
                raise QuotaEpuise(f"HTTP 429 côté fournisseur : {detail}")
            raise VisionError(f"HTTP {e.code} : {detail}")
        except urllib.error.URLError as e:
            raise VisionError(f"réseau : {e.reason}")
        except (ValueError, TypeError) as e:
            raise VisionError(f"réponse illisible : {e}")

        latence = int((time.time() - debut) * 1000)
        try:
            brut = reponse["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            bloque = (reponse.get("promptFeedback") or {}).get("blockReason")
            if bloque:
                raise VisionError(f"requête bloquée par le fournisseur ({bloque})")
            raise VisionError("réponse sans contenu exploitable")

        data, erreur = parser_reponse(brut)
        return data, {"latence_ms": latence, "brut": brut, "erreur_parsing": erreur,
                      "modele": self.modele, "provider": self.nom}


def provider_par_defaut(cfg=None):
    cfg = cfg or ram_config.get()
    nom = str(cfg.val("vision.provider", "gemini")).lower()
    if nom == "gemini":
        return GeminiProvider(cfg=cfg)
    raise VisionError(f"fournisseur vision inconnu : {nom}")


# ─────────────────────── ORCHESTRATION ───────────────────────
def contexte_annonce(annonce):
    """Le texte de l'annonce transmis au modèle. Volontairement présenté comme
    « à vérifier » : c'est le rôle de la couche vision de contredire le texte,
    pas de le confirmer par complaisance."""
    bouts = [f"Titre : {annonce.get('titre') or '(vide)'}",
             f"Prix affiché : {annonce.get('prix_affiche')} €"]
    if annonce.get("capacite_module_go") and annonce.get("nb_modules"):
        bouts.append(f"Configuration annoncée : {annonce['nb_modules']}×"
                     f"{annonce['capacite_module_go']} Go")
    if annonce.get("frequence_mhz"):
        bouts.append(f"Fréquence annoncée : {annonce['frequence_mhz']} MHz")
    if annonce.get("pn_detecte"):
        bouts.append(f"Part number lu dans le texte : {annonce['pn_detecte']}")
    desc = (annonce.get("description") or "")[:400]
    if desc:
        bouts.append(f"Description : {desc}")
    return "\n".join(bouts)


def analyser_annonce(annonce, provider=None, cfg=None, forcer=False):
    """Analyse une annonce de bout en bout : cache → quota → photos → appel →
    enregistrement.

    Retourne un dict avec au minimum `statut` :
      ok | cache | quota | photos_absentes | parse_erreur | echec
    Ne lève jamais : le worker doit pouvoir enchaîner sur l'annonce suivante.
    """
    cfg = cfg or ram_config.get()
    photos_urls = annonce.get("photos")
    if isinstance(photos_urls, str):
        try:
            photos_urls = json.loads(photos_urls)
        except (ValueError, TypeError):
            photos_urls = []
    photos_urls = photos_urls or []

    cle = ram_db.cache_key(annonce.get("url", ""), photos_urls)

    # ── Cache : jamais deux analyses pour le même jeu de photos ──
    if not forcer:
        cache = ram_db.analyse_en_cache(cle)
        if cache and cache.get("statut") == "ok":
            cache["statut_appel"] = "cache"
            cache["drapeaux"] = _jlist(cache.get("drapeaux"))
            return cache

    if not photos_urls:
        resultat = _normaliser({}, complet=False)
        resultat.update({"statut": "photos_absentes", "cache_cle": cle,
                         "annonce_id": annonce.get("id"),
                         "erreur": "annonce sans photo"})
        ram_db.enregistrer_analyse_vision(resultat)
        return resultat

    provider = provider or provider_par_defaut(cfg)
    plafond_minute, plafond_jour = cfg.quota_vision()

    ok, detail = ram_db.quota_disponible(plafond_minute, plafond_jour, provider.nom)
    if not ok:
        # Dégradation propre : rien n'est perdu, l'annonce reste ⚡ NON VÉRIFIÉ.
        return {"statut": "quota", "cache_cle": cle, "annonce_id": annonce.get("id"),
                "erreur": detail.get("motif"), "quota": detail, "drapeaux": []}

    # ── Téléchargement des photos (3 max) ──
    max_photos = int(cfg.val("vision.max_photos", 3))
    max_px = int(cfg.val("vision.photo_max_px", 1024))
    max_octets = int(cfg.val("vision.photo_max_octets", 3_500_000))
    photos = []
    for url in choisir_photos(photos_urls, max_photos):
        try:
            octets, mime = telecharger_photo(url, max_octets)
            photos.append(redimensionner(octets, mime, max_px))
        except Exception as e:
            print(f"[vision] photo ignorée ({url[:60]}…) : {e}")

    if not photos:
        resultat = _normaliser({}, complet=False)
        resultat.update({"statut": "photos_absentes", "cache_cle": cle,
                         "annonce_id": annonce.get("id"),
                         "erreur": "aucune photo téléchargeable"})
        ram_db.enregistrer_analyse_vision(resultat)
        return resultat

    # ── Appel ──
    tentatives = max(1, int(cfg.val("vision.max_tentatives", 2)))
    derniere_erreur = None
    for essai in range(tentatives):
        try:
            ram_db.consommer_quota(provider.nom, plafond_minute, plafond_jour)
            data, meta = provider.analyser(photos, contexte_annonce(annonce))
        except QuotaEpuise as e:
            return {"statut": "quota", "cache_cle": cle, "annonce_id": annonce.get("id"),
                    "erreur": str(e), "drapeaux": []}
        except VisionError as e:
            derniere_erreur = str(e)
            if essai + 1 < tentatives:
                time.sleep(1.5 * (essai + 1))
            continue

        statut = "parse_erreur" if meta.get("erreur_parsing") else "ok"
        resultat = dict(data)
        resultat.update({
            "statut": statut, "cache_cle": cle, "annonce_id": annonce.get("id"),
            "provider": meta.get("provider"), "modele": meta.get("modele"),
            "latence_ms": meta.get("latence_ms"), "photos_envoyees": len(photos),
            "reponse_brute": (meta.get("brut") or "")[:4000],
            "erreur": meta.get("erreur_parsing"),
        })
        ram_db.enregistrer_analyse_vision(resultat)
        return resultat

    resultat = _normaliser({}, complet=False)
    resultat.update({"statut": "echec", "cache_cle": cle, "annonce_id": annonce.get("id"),
                     "erreur": derniere_erreur, "photos_envoyees": len(photos)})
    ram_db.enregistrer_analyse_vision(resultat)
    return resultat


def _jlist(valeur):
    if isinstance(valeur, list):
        return valeur
    try:
        out = json.loads(valeur or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def etat_quota(cfg=None):
    """Pour le dashboard : consommation du jour, file d'attente, différées."""
    cfg = cfg or ram_config.get()
    plafond_minute, plafond_jour = cfg.quota_vision()
    provider = str(cfg.val("vision.provider", "gemini"))
    ok, detail = ram_db.quota_disponible(plafond_minute, plafond_jour, provider)
    file = ram_db.etat_file_vision()
    return {
        "provider": provider,
        "modele": cfg.val("vision.modele"),
        "disponible": ok,
        "motif": detail.get("motif"),
        "conso_minute": detail.get("minute", 0),
        "plafond_minute": plafond_minute,
        "conso_jour": detail.get("jour", 0),
        "plafond_jour": plafond_jour,
        "restant_jour": max(0, plafond_jour - detail.get("jour", 0)),
        "file_en_attente": file.get("en_attente", 0),
        "file_differees": file.get("differe", 0),
        "file_en_cours": file.get("en_cours", 0),
        "file_faites": file.get("fait", 0),
        "file_echecs": file.get("echec", 0),
    }


if __name__ == "__main__":
    # Test du parsing défensif sans appeler l'API : c'est là que se cachent
    # les plantages en production.
    cas = [
        ('{"est_ddr4_desktop": true, "confiance": 0.9, "part_number_lu": "CMK32GX4M2E3200C16"}',
         "JSON pur"),
        ('```json\n{"est_ddr4_desktop": true, "confiance": 0.8}\n```', "backticks"),
        ('Voici le résultat :\n{"est_ecc": true, "confiance": 0.7,}', "préambule + virgule traînante"),
        ('{"confiance": "0.65", "est_sodimm": "true", "drapeaux": "sticker flou"}', "types laxistes"),
        ('pas de json du tout', "réponse non structurée"),
        ('', "réponse vide"),
    ]
    for brut, libelle in cas:
        data, err = parser_reponse(brut)
        print(f"{libelle:<32} → confiance={data['confiance']} "
              f"sodimm={data['est_sodimm']} drapeaux={data['drapeaux']} "
              f"{'erreur: ' + err if err else 'ok'}")

    print("\nSélection de photos (max 3) :")
    urls = ["https://x/a_100x100.jpg", "https://x/b_800x600.jpg",
            "https://x/c_1600x1200.jpg", "https://x/d_400x300.jpg"]
    print("  ", choisir_photos(urls, 3))

    print("\nÉtat du quota :")
    for k, v in etat_quota().items():
        print(f"   {k:<18} {v}")
