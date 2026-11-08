"""
vinted_client.py — Client Vinted (multi-pays, diagnostic intégré)
═══════════════════════════════════════════════════════════════════════════
Interroge l'API interne de Vinted (/api/v2/catalog/items). Points durcis
par rapport à la version précédente, suite à un diagnostic (aucune offre
Vinted ne remontait jamais) :

  1. Fingerprint TLS précis (curl_cffi impersonate="chrome124", pas "chrome"
     générique) — Datadome fingerprinte le JA3 exact du client TLS.
  2. Accept-Language qui correspond au pays ciblé (un mauvais couple
     domaine/langue déclenche la détection bot).
  3. Rafraîchissement de cookie plus robuste : retry avec backoff, et un
     VRAI signal d'erreur remonté (avant: les erreurs étaient avalées en
     silence dans app.py, donc "zéro résultat" ne s'expliquait jamais).
  4. Support multi-pays via countries.py (domaine Vinted par pays).
  5. Mode diagnostic autonome: lance ce fichier directement pour voir
     précisément ce qui bloque (code HTTP, cookie obtenu ou non, etc.)

── Diagnostic rapide ──
    python3 vinted_client.py
Affiche étape par étape ce qui se passe (récupération du cookie, requête
de recherche, nombre de résultats) avec le détail de toute erreur.
"""

import time
import random
import re
from urllib.parse import urlparse, parse_qs

from countries import get_country_config
from currency import to_eur
import reseau

try:
    from curl_cffi import requests as cffi
    _HAS_CFFI = True
except Exception:
    import requests as cffi
    _HAS_CFFI = False

# Empreintes TLS de navigateur essayées dans l'ordre. chrome124 d'abord (celle
# qui a fonctionné jusqu'ici), puis les versions les plus récentes que la
# version installée de curl_cffi sait imiter — Chrome, Safari, Firefox — pour
# que l'empreinte ne vieillisse pas avec le code. Une version inconnue de
# curl_cffi est simplement écartée (pas d'erreur sur une vieille install).
_ANCIENNES = ["chrome124", "chrome123", "chrome120", "chrome119"]


def _candidats_impersonation():
    try:
        from curl_cffi.requests import BrowserType
        dispo = {b.value for b in BrowserType}
    except Exception:
        return list(_ANCIENNES)

    def plus_recent(motif, n=1):
        versions = []
        for v in dispo:
            m = re.fullmatch(motif, v)
            if m:
                versions.append((int(m.group(1)), v))
        return [v for _, v in sorted(versions, reverse=True)[:n]]

    liste = (["chrome124"] + plus_recent(r"chrome(\d+)a?", 2)
             + plus_recent(r"safari(\d{3})") + plus_recent(r"firefox(\d+)") + ["chrome120"])
    vus = []
    for v in liste:
        if v in dispo and v not in vus:
            vus.append(v)
    return vus or list(_ANCIENNES)


IMPERSONATE_CANDIDATES = _candidats_impersonation()


class VintedError(Exception):
    """Erreur Vinted avec contexte (code HTTP, corps de réponse tronqué)."""
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class VintedClient:
    def __init__(self, country=None, impersonate=None, verbose=False, proxy=reseau.DEFAUT):
        """
        country: code pays (ex "FR", "DE"...) — défaut: SNIPER_COUNTRY ou FR.
        impersonate: force une version curl_cffi précise (sinon essaie la liste).
        verbose: affiche le détail de chaque requête (utile pour diagnostiquer).
        proxy: URL de proxy, None pour la connexion directe ; non fourni =
               ancien réglage SNIPER_PROXY_URL (voir reseau.py).
        """
        self._proxy = reseau.proxy_url() if proxy is reseau.DEFAUT else proxy
        cfg = get_country_config(country)
        self.country = cfg["code"]
        self.domain = cfg["vinted_domain"]
        self.lang = cfg["lang"]
        self.currency = cfg["currency"]
        self.host = f"https://www.{self.domain}"
        self.items_api = self.host + "/api/v2/catalog/items"
        self.verbose = verbose
        self._impersonate = impersonate
        self._cookie_ts = 0
        self._cookie_ok = False
        self.session = None
        self._init_session()

    def _log(self, *a):
        if self.verbose:
            print("[vinted]", *a)

    def _init_session(self):
        imp = self._impersonate or IMPERSONATE_CANDIDATES[0]
        if _HAS_CFFI:
            self.session = cffi.Session(impersonate=imp)
        else:
            self.session = cffi.Session()
        self.session.headers.update({
            "Accept-Language": self.lang,
            "Accept": "application/json, text/plain, */*",
            "Referer": self.host + "/",
        })
        self._appliquer_proxy()

    def _appliquer_proxy(self):
        """Proxy sortant de cette instance (voir reseau.py)."""
        px = reseau.proxies_http(self._proxy)
        if px:
            self.session.proxies = px

    def _ensure_session(self, force=False):
        """Récupère/rafraîchit le cookie de session Datadome. Essaie plusieurs
        versions d'impersonation si la première échoue."""
        if not force and self._cookie_ok and (time.time() - self._cookie_ts) < 240:
            return

        versions = [self._impersonate] if self._impersonate else IMPERSONATE_CANDIDATES
        last_err = None
        for imp in versions:
            try:
                if _HAS_CFFI:
                    self.session = cffi.Session(impersonate=imp)
                    self.session.headers.update({
                        "Accept-Language": self.lang,
                        "Accept": "application/json, text/plain, */*",
                        "Referer": self.host + "/",
                    })
                    self._appliquer_proxy()
                r = self.session.get(self.host, timeout=15)
                self._log(f"init cookie via {imp}: HTTP {r.status_code}, "
                          f"{len(r.cookies)} cookie(s)")
                if r.status_code == 200:
                    self._cookie_ok = True
                    self._cookie_ts = time.time()
                    self._impersonate = imp  # garde la version qui a marché
                    return
                last_err = VintedError(f"page d'accueil {self.domain}: HTTP {r.status_code}",
                                       status=r.status_code, body=r.text[:300])
            except Exception as e:
                last_err = VintedError(f"connexion échouée ({imp}): {e}")
                self._log(f"init cookie via {imp}: ÉCHEC — {e}")
            time.sleep(0.8)

        self._cookie_ok = False
        if last_err:
            raise last_err

    @staticmethod
    def parse_query_url(url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        params = {}
        for key in ["search_text", "price_to", "price_from", "currency",
                    "order", "catalog_ids", "brand_ids", "status_ids"]:
            if key in qs:
                params[key] = qs[key][0]
        for raw_key, vals in qs.items():
            base = raw_key.replace("[]", "")
            if base in ("brand_ids", "catalog", "catalog_ids") and raw_key.endswith("[]"):
                params[base if base != "catalog" else "catalog_ids"] = ",".join(vals)
        params.setdefault("order", "newest_first")
        params.setdefault("per_page", "20")
        return params

    def search(self, query_url=None, search_text=None, price_to=None,
               max_retries=3):
        """Recherche par URL Vinted OU par texte simple. Lève VintedError
        avec un message clair en cas d'échec (plus de silence)."""
        if query_url:
            params = self.parse_query_url(query_url)
        else:
            params = {"search_text": search_text or "", "order": "newest_first",
                      "per_page": "20", "currency": self.currency}
            if price_to:
                params["price_to"] = str(price_to)

        last_err = None
        for attempt in range(max_retries):
            try:
                self._ensure_session(force=(attempt > 0))
            except VintedError as e:
                last_err = e
                self._log(f"tentative {attempt+1}/{max_retries}: cookie KO ({e})")
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                r = self.session.get(self.items_api, params=params, timeout=20)
                self._log(f"GET {self.items_api} params={params} -> HTTP {r.status_code}")
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception as e:
                        raise VintedError(f"réponse non-JSON (HTTP 200 mais parsing échoué: {e})",
                                          status=200, body=r.text[:300])
                    items = data.get("items", [])
                    self._log(f"{len(items)} item(s) reçus")
                    return [self._normalize(it) for it in items]
                elif r.status_code in (401, 403):
                    last_err = VintedError(f"bloqué par Vinted/Datadome (HTTP {r.status_code}) "
                                          f"— cookie rejeté, nouvelle tentative avec cookie frais",
                                          status=r.status_code, body=r.text[:300])
                    self._cookie_ok = False
                    time.sleep(1.5 * (attempt + 1))
                    continue
                elif r.status_code == 429:
                    last_err = VintedError("trop de requêtes (HTTP 429) — pause plus longue nécessaire",
                                          status=429)
                    time.sleep(5 * (attempt + 1))
                    continue
                else:
                    last_err = VintedError(f"HTTP {r.status_code} inattendu",
                                          status=r.status_code, body=r.text[:300])
                    time.sleep(1.5)
            except VintedError:
                raise
            except Exception as e:
                last_err = VintedError(f"erreur réseau: {e}")
                self._log(f"tentative {attempt+1}/{max_retries}: {e}")
                time.sleep(1.5 * (attempt + 1))

        raise last_err or VintedError("échec de recherche sans détail")

    # ─────────────── Profil vendeur (nombre d'avis) ───────────────
    @staticmethod
    def _parse_feedback(user):
        """Extrait le nombre d'avis d'un profil /api/v2/users/{id}.

        Champs documentés par les scrapers publics : feedback_count (total),
        positive/neutral/negative_feedback_count, feedback_reputation (0 à 1).
        Si le total manque, on additionne les trois compteurs ; si rien n'est
        lisible on lève une erreur plutôt que d'inventer « 0 avis ».
        """
        count = user.get("feedback_count")
        if count is None:
            parts = [user.get(k) for k in ("positive_feedback_count",
                                           "neutral_feedback_count",
                                           "negative_feedback_count")]
            if any(p is not None for p in parts):
                count = sum(int(p or 0) for p in parts)
        if count is None:
            raise VintedError("profil vendeur sans compteur d'avis", status=200)
        rep = user.get("feedback_reputation")
        try:
            note = round(float(rep) * 5, 2) if rep is not None else None
        except (TypeError, ValueError):
            note = None
        return {"avis": int(count), "note": note, "pseudo": user.get("login"),
                "positifs": user.get("positive_feedback_count"),
                "negatifs": user.get("negative_feedback_count")}

    def get_user_feedback(self, user_id, max_retries=2):
        """Nombre d'avis d'un vendeur. Même discipline que search() : cookie
        rafraîchi sur 401/403, pause sur 429, erreur explicite sinon."""
        url = f"{self.host}/api/v2/users/{user_id}"
        last_err = None
        for attempt in range(max_retries):
            try:
                self._ensure_session(force=(attempt > 0))
            except VintedError as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                r = self.session.get(url, timeout=20)
                self._log(f"GET {url} -> HTTP {r.status_code}")
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception as e:
                        raise VintedError(f"profil non-JSON ({e})", status=200,
                                          body=r.text[:300])
                    return self._parse_feedback((data or {}).get("user") or {})
                if r.status_code in (401, 403):
                    last_err = VintedError(f"profil vendeur bloqué (HTTP {r.status_code})",
                                           status=r.status_code, body=r.text[:300])
                    self._cookie_ok = False
                elif r.status_code == 429:
                    last_err = VintedError("trop de requêtes (HTTP 429)", status=429)
                    time.sleep(5 * (attempt + 1))
                    continue
                elif r.status_code == 404:
                    raise VintedError("profil vendeur introuvable", status=404)
                else:
                    last_err = VintedError(f"profil vendeur : HTTP {r.status_code}",
                                           status=r.status_code, body=r.text[:300])
                time.sleep(1.5 * (attempt + 1))
            except VintedError as e:
                if e.status in (200, 404):
                    raise
                last_err = e
            except Exception as e:
                last_err = VintedError(f"erreur réseau: {e}")
                time.sleep(1.5 * (attempt + 1))
        raise last_err or VintedError("lecture du profil vendeur sans détail")

    def _normalize(self, item):
        price = 0
        p = item.get("price")
        if isinstance(p, dict):
            try:
                price = float(p.get("amount", 0))
            except (TypeError, ValueError):
                price = 0
        elif isinstance(p, (int, float)):
            price = float(p)
        elif isinstance(p, str):
            m = re.search(r'[\d.]+', p.replace(",", "."))
            price = float(m.group()) if m else 0

        tip = item.get("total_item_price")
        if isinstance(tip, dict):
            try:
                price = float(tip.get("amount", price)) or price
            except (TypeError, ValueError):
                pass

        price = to_eur(price, self.currency)  # normalise tout en EUR, quel que soit le pays

        photo = ""
        ph = item.get("photo")
        if isinstance(ph, dict):
            photo = ph.get("url") or ph.get("full_size_url") or ""

        # Date de publication: Vinted ne renvoie pas de champ dédié dans cette
        # API, mais le timestamp de la photo principale (uploadée à la création
        # de l'annonce) est un excellent proxy fiable.
        posted_ts = None
        try:
            posted_ts = float(ph["high_resolution"]["timestamp"])
        except Exception:
            pass

        brand = item.get("brand_title") or ""
        url = item.get("url") or ""
        title = item.get("title") or ""
        user = item.get("user") if isinstance(item.get("user"), dict) else {}

        return {
            "id": item.get("id"),
            "vendeur_id": user.get("id"),
            "vendeur_pseudo": user.get("login"),
            "subject": title,
            "price": int(round(price)),
            "url": url,
            "image": photo,
            "location": "Vinted",
            "description": brand,
            "source": "vinted",
            "posted_ts": posted_ts,
            "ships": True,  # Vinted intègre systématiquement l'envoi (étiquette prépayée)
        }


# ─────────────────────────────────────────────────────────────
#  MODE DIAGNOSTIC — lance ce fichier directement pour voir ce qui bloque
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    country = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"\n🔍 Diagnostic Vinted — pays: {country or '(défaut env/FR)'}\n")

    vc = VintedClient(country=country, verbose=True)
    print(f"Domaine ciblé: {vc.host}")
    print(f"Accept-Language: {vc.lang}\n")

    print("── Étape 1: récupération du cookie de session ──")
    try:
        vc._ensure_session(force=True)
        print(f"✅ Cookie obtenu (impersonation: {vc._impersonate})\n")
    except VintedError as e:
        print(f"❌ ÉCHEC: {e}")
        if e.status:
            print(f"   Code HTTP: {e.status}")
        if e.body:
            print(f"   Début de réponse: {e.body}")
        print("\n   Causes possibles: IP bloquée par Datadome, connexion internet, "
              "ou curl_cffi qui a besoin d'une mise à jour (pip install -U curl_cffi).")
        sys.exit(1)

    print("── Étape 2: recherche test ('carte graphique') ──")
    try:
        results = vc.search(search_text="carte graphique", price_to=300)
        print(f"✅ {len(results)} résultat(s) reçus\n")
        for r in results[:5]:
            print(f"   {r['price']}€  {r['subject'][:60]}")
            print(f"      {r['url']}")
        if not results:
            print("   ⚠️ 0 résultat: la requête a réussi mais Vinted n'a rien retourné "
                  "pour cette recherche précise (essaie un autre terme).")
    except VintedError as e:
        print(f"❌ ÉCHEC: {e}")
        if e.status:
            print(f"   Code HTTP: {e.status}")
        if e.body:
            print(f"   Début de réponse: {e.body}")
        sys.exit(1)

    print("\n✅ Vinted fonctionne correctement depuis cette machine.")
