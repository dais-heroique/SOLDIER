"""
facebook_client.py — Client Facebook Marketplace (GraphQL non officiel)
═══════════════════════════════════════════════════════════════════════════
Basé sur la technique de https://github.com/kyleronayne/marketplace-api
(pas un vrai paquet pip — juste deux scripts — donc on réimplémente la même
technique directement ici plutôt que de dépendre d'un repo non empaqueté).

Facebook Marketplace n'a pas d'API officielle. Ce module imite les requêtes
GraphQL internes que le site web utilise lui-même — PAS besoin de compte/
connexion Facebook. Testé et confirmé fonctionnel (résolution de lieu OK,
recherche syntaxiquement valide) au moment de l'écriture.

⚠️ Contrainte propre à Facebook (contrairement à Leboncoin/Vinted/eBay) :
une recherche nécessite une LOCALISATION (latitude/longitude) — Facebook ne
permet pas de chercher "partout dans un pays", seulement autour d'un point
avec un rayon (16km par défaut ici). On résout automatiquement une ville
par défaut selon le pays choisi (voir countries.py).

⚠️ Les identifiants "doc_id" utilisés sont des identifiants internes de
requêtes GraphQL Facebook — ils peuvent changer quand Facebook met à jour
son application web (arrive occasionnellement). Si ça casse un jour, lance
le diagnostic (`python3 facebook_client.py`) pour voir l'erreur exacte.
"""

import json
import re
import time

from currency import to_eur
from countries import get_country_config

try:
    from curl_cffi import requests as cffi
    _HAS_CFFI = True
except Exception:
    import requests as cffi
    _HAS_CFFI = False

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
GRAPHQL_HEADERS = {
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

DOC_ID_LOCATIONS = "5585904654783609"
DOC_ID_SEARCH = "7111939778879383"

# Ville par défaut pour résoudre une position quand aucune n'est précisée,
# par code pays (les mêmes codes que countries.py)
DEFAULT_LOCATIONS = {
    "FR": "Paris", "DE": "Berlin", "IT": "Roma", "ES": "Madrid", "NL": "Amsterdam",
    "BE": "Bruxelles", "AT": "Wien", "PT": "Lisboa", "PL": "Warszawa", "GB": "London",
    "US": "New York", "CA": "Toronto", "IE": "Dublin", "SE": "Stockholm",
    "DK": "Copenhagen", "FI": "Helsinki", "GR": "Athens", "CH": "Zurich",
    "AU": "Sydney", "CZ": "Prague", "SK": "Bratislava", "LT": "Vilnius",
    "RO": "Bucharest", "LU": "Luxembourg", "HU": "Budapest", "IN": "Mumbai",
    "SG": "Singapore", "JP": "Tokyo", "BR": "Sao Paulo", "MX": "Mexico City",
    "XX": "Paris", "default": "Paris",
}

RADIUS_KM = 16


class FacebookError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class FacebookClient:
    def __init__(self, country_code=None, location_query=None, radius_km=None, verbose=False):
        self.verbose = verbose
        if _HAS_CFFI:
            self.session = cffi.Session(impersonate="chrome124")
        else:
            self.session = cffi.Session()
        self.session.headers.update(GRAPHQL_HEADERS)

        self.location_query = location_query or DEFAULT_LOCATIONS.get(
            (country_code or "FR").upper(), DEFAULT_LOCATIONS["default"])
        self.default_radius_km = radius_km or RADIUS_KM
        try:
            self._fallback_currency = get_country_config(country_code)["currency"]
        except Exception:
            self._fallback_currency = "EUR"
        self._lat = None
        self._lng = None

    def _log(self, *a):
        if self.verbose:
            print("[facebook]", *a)

    def _post(self, payload, max_retries=3):
        last_err = None
        for attempt in range(max_retries):
            try:
                r = self.session.post(GRAPHQL_URL, data=payload, timeout=20)
                if r.status_code != 200:
                    last_err = FacebookError(f"HTTP {r.status_code}", status=r.status_code,
                                             body=r.text[:300])
                    time.sleep(2 * (attempt + 1))
                    continue
                data = r.json()
                if data.get("errors"):
                    msg = data["errors"][0].get("message", "erreur inconnue")
                    if "rate limit" in msg.lower():
                        self._log(f"rate limité, pause {3*(attempt+1)}s…")
                        time.sleep(3 * (attempt + 1))
                        last_err = FacebookError(f"rate limit Facebook: {msg}")
                        continue
                    raise FacebookError(f"erreur Facebook: {msg}")
                return data
            except FacebookError:
                raise
            except Exception as e:
                last_err = FacebookError(f"erreur réseau: {e}")
                time.sleep(1.5 * (attempt + 1))
        raise last_err or FacebookError("échec sans détail")

    def resolve_location(self, query=None):
        """Résout un nom de lieu en (latitude, longitude). Mis en cache après le 1er appel."""
        if self._lat is not None and query is None:
            return self._lat, self._lng
        q = query or self.location_query
        payload = {
            "variables": json.dumps({"params": {"caller": "MARKETPLACE",
                                                "page_category": ["CITY", "SUBCITY", "NEIGHBORHOOD", "POSTAL_CODE"],
                                                "query": q}}),
            "doc_id": DOC_ID_LOCATIONS,
        }
        self._log(f"résolution du lieu '{q}'…")
        data = self._post(payload)
        edges = data.get("data", {}).get("city_street_search", {}).get("street_results", {}).get("edges", [])
        if not edges:
            raise FacebookError(f"aucun lieu trouvé pour '{q}'")
        node = edges[0]["node"]
        lat = node["location"]["latitude"]
        lng = node["location"]["longitude"]
        self._log(f"-> {node.get('single_line_address', q)}: {lat}, {lng}")
        if query is None:
            self._lat, self._lng = lat, lng
        return lat, lng

    def search(self, query, price_to=None, price_from=None, radius_km=None):
        """Recherche par mots-clés autour de la position résolue. Retourne une liste normalisée."""
        lat, lng = self.resolve_location()
        radius = radius_km or self.default_radius_km
        price_lower = int(price_from) if price_from else 0
        price_upper = int(price_to) * 100 if price_to else 214748364700  # centimes, valeur max par défaut

        variables = {
            "count": 24,
            "params": {
                "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": query},
                "browse_request_params": {
                    "commerce_enable_local_pickup": True,
                    "commerce_enable_shipping": True,
                    "commerce_search_and_rp_available": True,
                    "commerce_search_and_rp_condition": None,
                    "commerce_search_and_rp_ctime_days": None,
                    "filter_location_latitude": lat,
                    "filter_location_longitude": lng,
                    "filter_price_lower_bound": price_lower * 100 if price_from else 0,
                    "filter_price_upper_bound": price_upper,
                    "filter_radius_km": radius,
                },
                "custom_request_params": {"surface": "SEARCH"},
            },
        }
        payload = {"variables": json.dumps(variables), "doc_id": DOC_ID_SEARCH}
        self._log(f"recherche '{query}' autour de ({lat},{lng}), rayon {radius}km…")
        data = self._post(payload)

        listings = []
        edges = data.get("data", {}).get("marketplace_search", {}).get("feed_units", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            if node.get("__typename") != "MarketplaceFeedListingStoryObject":
                continue
            listing = node.get("listing", {})
            listings.append(self._normalize(listing))
        self._log(f"{len(listings)} annonce(s) reçue(s)")
        return listings

    # Symboles/codes courants -> code devise ISO (pour la conversion vers EUR)
    _SYMBOL_TO_CURRENCY = {
        "€": "EUR", "$": "USD", "£": "GBP", "zł": "PLN", "Kč": "CZK",
        "lei": "RON", "Ft": "HUF", "kr": "SEK", "CHF": "CHF", "A$": "AUD",
        "₹": "INR", "S$": "SGD", "¥": "JPY", "R$": "BRL", "MX$": "MXN",
    }

    @staticmethod
    def _parse_price(formatted):
        """Convertit '$150', '150 €', '1 200,50 €'... en nombre."""
        if not formatted:
            return 0
        cleaned = re.sub(r"[^\d,.\s]", "", formatted).strip()
        cleaned = cleaned.replace(" ", "").replace(",", ".")
        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        try:
            return float(cleaned)
        except ValueError:
            return 0

    @classmethod
    def _detect_currency(cls, formatted, fallback):
        """Devine la devise depuis le symbole dans le texte formaté par Facebook."""
        if not formatted:
            return fallback
        for symbol, code in cls._SYMBOL_TO_CURRENCY.items():
            if symbol in formatted:
                return code
        return fallback

    def _normalize(self, listing):
        listing_id = listing.get("id", "")
        title = listing.get("marketplace_listing_title", "")
        formatted = listing.get("listing_price", {}).get("formatted_amount", "")
        price = self._parse_price(formatted)
        currency = self._detect_currency(formatted, self._fallback_currency)
        price = to_eur(price, currency)  # normalise tout en EUR, quel que soit le pays

        image = ""
        try:
            image = listing["primary_listing_photo"]["image"]["uri"]
        except Exception:
            pass

        location = ""
        try:
            location = listing["location"]["reverse_geocode"]["city_page"]["display_name"]
        except Exception:
            pass

        url = f"https://www.facebook.com/marketplace/item/{listing_id}/" if listing_id else ""

        # Date de publication: pas de champ confirmé et stable dans cette API
        # non-officielle — on tente les noms de champs les plus plausibles,
        # sans planter si absents (repli propre sur l'heure de scan ailleurs).
        posted_ts = None
        for field in ("creation_time", "listing_creation_time", "creation_timestamp"):
            v = listing.get(field)
            if v:
                try:
                    posted_ts = float(v)
                    break
                except (TypeError, ValueError):
                    pass

        return {
            "subject": title,
            "price": int(round(price)),
            "url": url,
            "image": image,
            "location": location or "Facebook Marketplace",
            "description": "",
            "source": "facebook",
            "ships": None,  # Facebook Marketplace: info fiable non disponible via cette API
            "posted_ts": posted_ts,
        }


# ─────────────────────────────────────────────────────────────
#  MODE DIAGNOSTIC
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    country = sys.argv[1] if len(sys.argv) > 1 else "FR"
    print(f"\n🔍 Diagnostic Facebook Marketplace — pays: {country}\n")

    fc = FacebookClient(country_code=country, verbose=True)

    print("── Étape 1: résolution de la position ──")
    try:
        lat, lng = fc.resolve_location()
        print(f"✅ Position résolue: {lat}, {lng}\n")
    except FacebookError as e:
        print(f"❌ ÉCHEC: {e}")
        if e.status:
            print(f"   Code HTTP: {e.status}")
        if e.body:
            print(f"   Début de réponse: {e.body}")
        sys.exit(1)

    print("── Étape 2: recherche test ('carte graphique') ──")
    try:
        results = fc.search("carte graphique", price_to=300)
        print(f"✅ {len(results)} résultat(s)\n")
        for r in results[:5]:
            print(f"   {r['price']}€  {r['subject'][:60]}")
            print(f"      {r['url']}")
        if not results:
            print("   ⚠️ 0 résultat — requête réussie mais rien retourné pour cette recherche.")
    except FacebookError as e:
        print(f"❌ ÉCHEC: {e}")
        if e.status:
            print(f"   Code HTTP: {e.status}")
        if e.body:
            print(f"   Début de réponse: {e.body}")
        print("\n   Si l'erreur mentionne 'rate limit', réessaie dans quelques minutes —")
        print("   Facebook limite fortement le nombre de requêtes par IP.")
        print("   Si l'erreur est différente (schéma GraphQL, doc_id invalide), c'est que")
        print("   Facebook a changé son application web — les doc_id doivent être mis à jour.")
        sys.exit(1)

    print("\n✅ Facebook Marketplace fonctionne correctement depuis cette machine.")
