"""
ebay_client.py — Client eBay via la Browse API (REST/OAuth2)
═══════════════════════════════════════════════════════════════════════════
⚠️ IMPORTANT: l'ancienne "Finding API" (celle utilisée par ebaysdk-python,
https://github.com/timotheus/ebaysdk-python) a été FERMÉE par eBay le
5 février 2025 ("Service Unavailable" en est la conséquence directe — ce
n'est pas une erreur temporaire, l'API n'existe simplement plus). Ce module
utilise donc la remplaçante officielle: la Browse API (REST, OAuth2).

── Obtenir tes identifiants (gratuit, 5 minutes) ──
  1. Crée un compte sur https://developer.ebay.com (gratuit)
  2. "My Account" -> "Application Keys" -> crée une clé en mode PRODUCTION
  3. Tu as maintenant DEUX identifiants à copier (contrairement à l'ancienne
     API qui n'avait besoin que du premier) :
       - App ID (Client ID)
       - Cert ID (Client Secret)

── Configuration ──
  Variables d'environnement, OU fichier .env à côté de app.py:
      EBAY_APP_ID=TonAppID-xxxx-PRD-xxxx
      EBAY_CERT_ID=TonCertID-xxxx

Sans ces deux identifiants, eBay est simplement désactivé (l'app continue
de fonctionner normalement avec les autres sources).
"""

import os
import time
import base64
from datetime import datetime

try:
    from curl_cffi import requests as cffi
    _HAS_CFFI = True
except Exception:
    import requests as cffi
    _HAS_CFFI = False

from countries import get_country_config
from currency import to_eur

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"


def _load_dotenv_value(key):
    """Lit une variable depuis l'environnement ou un fichier local si absente.
    Accepte plusieurs noms de fichier: le Finder macOS refuse de créer un
    fichier commençant par un point, donc beaucoup de gens se retrouvent avec
    "env" au lieu de ".env" — on vérifie les variantes courantes."""
    if os.environ.get(key):
        return os.environ[key]
    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [".env", "env", "env.txt", ".env.txt", "ebay.env"]
    for name in candidates:
        path = os.path.join(base_dir, name)
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(key + "="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                continue
    return None


class EbayError(Exception):
    pass


class EbayClient:
    def __init__(self, country=None, app_id=None, cert_id=None, verbose=False):
        self.app_id = app_id or _load_dotenv_value("EBAY_APP_ID")
        self.cert_id = cert_id or _load_dotenv_value("EBAY_CERT_ID")
        if not self.app_id or not self.cert_id:
            raise EbayError(
                "Identifiants eBay incomplets. Il en faut DEUX depuis la migration "
                "vers la Browse API : App ID (Client ID) ET Cert ID (Client Secret). "
                "Crée un compte gratuit sur https://developer.ebay.com, récupère les "
                "deux dans 'Application Keys', et mets-les dans EBAY_APP_ID et "
                "EBAY_CERT_ID (variables d'environnement ou fichier .env)."
            )

        cfg = get_country_config(country)
        self.currency = cfg["currency"]
        # Browse API: identifiants de marketplace en underscore (EBAY_FR, EBAY_US...)
        # countries.py stocke le format historique avec un tiret (EBAY-FR) -> conversion
        self.marketplace_id = cfg["ebay_site"].replace("-", "_")
        self.verbose = verbose

        if _HAS_CFFI:
            self.session = cffi.Session(impersonate="chrome124")
        else:
            self.session = cffi.Session()

        self._token = None
        self._token_exp = 0

    def _log(self, *a):
        if self.verbose:
            print("[ebay]", *a)

    def _get_token(self):
        """Récupère (et met en cache) un jeton OAuth2 Application (client_credentials).
        Valide ~2h, rafraîchi automatiquement avant expiration."""
        if self._token and time.time() < self._token_exp - 60:
            return self._token

        credentials = base64.b64encode(f"{self.app_id}:{self.cert_id}".encode()).decode()
        headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials", "scope": OAUTH_SCOPE}
        self._log("récupération du jeton OAuth…")
        r = self.session.post(TOKEN_URL, headers=headers, data=data, timeout=15)
        if r.status_code != 200:
            raise EbayError(f"authentification eBay échouée (HTTP {r.status_code}): {r.text[:250]}")
        payload = r.json()
        self._token = payload["access_token"]
        self._token_exp = time.time() + payload.get("expires_in", 7200)
        self._log("jeton obtenu, valide", payload.get("expires_in", 7200), "s")
        return self._token

    def search(self, query, price_to=None, price_from=None, limit=20):
        """Recherche par mots-clés via la Browse API. Retourne une liste normalisée."""
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
        }
        params = {"q": query, "limit": str(limit)}
        filters = []
        if price_from or price_to:
            lo = price_from if price_from else 0
            hi = price_to if price_to else 999999
            filters.append(f"price:[{lo}..{hi}]")
            filters.append(f"priceCurrency:{self.currency}")
        if filters:
            params["filter"] = ",".join(filters)

        self._log(f"recherche '{query}' (marketplace={self.marketplace_id}, filtres={filters})")
        try:
            r = self.session.get(BROWSE_URL, headers=headers, params=params, timeout=20)
        except Exception as e:
            raise EbayError(f"connexion eBay impossible: {e}")

        if r.status_code == 401:
            # jeton expiré/rejeté -> on force un renouvellement et on retente une fois
            self._token = None
            token = self._get_token()
            headers["Authorization"] = f"Bearer {token}"
            r = self.session.get(BROWSE_URL, headers=headers, params=params, timeout=20)

        if r.status_code != 200:
            raise EbayError(f"HTTP {r.status_code}: {r.text[:250]}")

        data = r.json()
        items = data.get("itemSummaries", [])
        self._log(f"{len(items)} item(s) reçus")
        return [self._normalize(it) for it in items]

    def _normalize(self, item):
        price = 0.0
        currency = self.currency
        try:
            price = float(item["price"]["value"])
            currency = item["price"].get("currency", self.currency)
        except Exception:
            pass
        price = to_eur(price, currency)  # normalise tout en EUR, quel que soit le pays

        title = item.get("title", "") or ""
        url = item.get("itemWebUrl", "") or ""
        location = ""
        try:
            location = item.get("itemLocation", {}).get("country", "") or "eBay"
        except Exception:
            location = "eBay"

        image = ""
        try:
            image = item.get("image", {}).get("imageUrl", "")
        except Exception:
            pass

        condition = item.get("condition", "") or ""
        ships = bool(item.get("shippingOptions"))  # présence d'options de livraison

        # Date de publication réelle — la Browse API la fournit directement,
        # pas besoin d'appel supplémentaire.
        posted_ts = None
        raw_date = item.get("itemCreationDate") or item.get("itemOriginDate")
        if raw_date:
            try:
                posted_ts = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass

        desc = f"État eBay: {condition}" if condition else ""

        return {
            "subject": title,
            "price": int(round(price)),
            "url": url,
            "image": image,
            "location": location,
            "description": desc,
            "posted_ts": posted_ts,
            "source": "ebay",
            "ships": ships,
        }


# ─────────────────────────────────────────────────────────────
#  MODE DIAGNOSTIC — lance ce fichier directement pour tester ta clé
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    country = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"\n🔍 Diagnostic eBay (Browse API) — pays: {country or '(défaut env/FR)'}\n")

    try:
        ec = EbayClient(country=country, verbose=True)
    except EbayError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"Marketplace ciblé: {ec.marketplace_id}")
    print(f"App ID: {ec.app_id[:12]}...")
    print(f"Cert ID: {ec.cert_id[:8]}...\n")

    print("── Étape 1: authentification OAuth ──")
    try:
        ec._get_token()
        print("✅ Jeton obtenu avec succès\n")
    except EbayError as e:
        print(f"❌ ÉCHEC: {e}")
        print("\n   Vérifie que ton App ID ET ton Cert ID sont bien tous les deux corrects,")
        print("   et que ton Keyset n'est pas désactivé (voir la page Application Keys).")
        sys.exit(1)

    print("── Étape 2: recherche test ('carte graphique nvidia') ──")
    try:
        results = ec.search("carte graphique nvidia", price_to=300)
        print(f"✅ {len(results)} résultat(s)\n")
        for r in results[:5]:
            print(f"   {r['price']}€  {r['subject'][:60]}")
            print(f"      {r['url']}")
    except EbayError as e:
        print(f"❌ ÉCHEC: {e}")
        sys.exit(1)

    print("\n✅ eBay (Browse API) fonctionne correctement.")
