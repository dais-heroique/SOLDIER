"""
reseau.py — Proxy sortant optionnel (variable SNIPER_PROXY_URL)
═══════════════════════════════════════════════════════════════════════════
Quand le bot tourne sur un serveur (GitHub Actions…) plutôt que sur ta box,
Vinted et Leboncoin voient une IP de centre de données, que Datadome bloque
souvent. Si c'est le cas, la seule vraie parade est de faire sortir les
requêtes par une IP résidentielle : un proxy.

    SNIPER_PROXY_URL=http://utilisateur:motdepasse@hote:port

Variable absente = aucun proxy, comportement inchangé (cas du Mac).
"""

import os
from urllib.parse import urlparse


def proxy_url():
    return (os.environ.get("SNIPER_PROXY_URL") or "").strip() or None


def proxies_http():
    """Dict {"http": url, "https": url} pour requests / curl_cffi, ou None."""
    url = proxy_url()
    return {"http": url, "https": url} if url else None


def proxy_lbc():
    """Objet lbc.Proxy pour lbc.Client(proxy=...), ou None."""
    url = proxy_url()
    if not url:
        return None
    import lbc
    p = urlparse(url)
    # Identifiants gardés tels quels (déjà encodés dans l'URL) : lbc.Proxy.url
    # les recolle sans les encoder, les décoder ici casserait l'URL finale.
    return lbc.Proxy(host=p.hostname, port=p.port or 80,
                     username=p.username or None, password=p.password or None,
                     scheme=p.scheme or "http")


def client_lbc(**kwargs):
    """lbc.Client avec le proxy configuré, s'il y en a un."""
    import lbc
    proxy = proxy_lbc()
    if proxy is not None:
        kwargs["proxy"] = proxy
    return lbc.Client(**kwargs)
