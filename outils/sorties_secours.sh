#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# outils/sorties_secours.sh — Prépare les sorties réseau de secours du bot
# ═══════════════════════════════════════════════════════════════════════════
# Lancé par les workflows GitHub Actions avant le scan. Chaque sortie qui
# s'installe correctement est annoncée à reseau.py via $GITHUB_ENV ; celle qui
# échoue est simplement absente de la chaîne. Ce script n'échoue JAMAIS : un
# problème de secours ne doit pas empêcher le scan direct de tourner.
#
#   maison : Tailscale en mode « userspace » + exit node chez toi.
#            Le trafic Vinted/Leboncoin sort par ta box (IP de particulier).
#            Secrets : TS_AUTHKEY, TS_EXIT_NODE   (README §10)
#   warp   : Cloudflare WARP en mode proxy, gratuit, sans compte.
#            SNIPER_WARP = auto (défaut : seulement si le direct a été bloqué
#            récemment) | toujours | jamais
#
# Les adresses IP ne sont JAMAIS affichées : les logs d'un dépôt public sont
# publics, et ton IP de box n'a rien à y faire.
# ═══════════════════════════════════════════════════════════════════════════
set -u
cd "$(dirname "$0")/.." || exit 0
SORTIE_ENV="${GITHUB_ENV:-/dev/null}"
ACTIVES=()

trace() {   # $1 = proxy SOCKS (vide = connexion directe)
  if [ -n "${1:-}" ]; then
    curl -s --max-time 15 --socks5-hostname "$1" https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null
  else
    curl -s --max-time 15 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null
  fi
}
ip_de() { trace "${1:-}" | sed -n 's/^ip=//p'; }
masquer() { sed -E 's/tskey-[A-Za-z0-9_-]+/tskey-***/g; s/[0-9]{1,3}(\.[0-9]{1,3}){3}/x.x.x.x/g'; }

IP_DIRECTE="$(ip_de)"

# ─────────────────────── 1. Sortie maison (Tailscale) ───────────────────────
if [ -n "${TS_AUTHKEY:-}" ] && [ -n "${TS_EXIT_NODE:-}" ]; then
  echo "▶ Sortie maison (Tailscale)"
  if ! command -v tailscaled >/dev/null 2>&1; then
    curl -fsSL https://tailscale.com/install.sh | sh >/tmp/ts-install.log 2>&1 \
      || echo "  ⚠️ installation de Tailscale impossible"
  fi
  if command -v tailscaled >/dev/null 2>&1; then
    # Mode userspace : seul ce qui passe par le proxy SOCKS sort par la maison.
    # pip, Telegram, GitHub restent en direct — si l'appareil maison s'éteint,
    # le reste du bot continue de fonctionner.
    sudo systemctl stop tailscaled >/dev/null 2>&1 || true
    sudo tailscaled --tun=userspace-networking --socks5-server=127.0.0.1:1055 \
      --state=mem: --socket=/tmp/tailscaled.sock >/tmp/tailscaled.log 2>&1 &
    sleep 3
    CLE="$TS_AUTHKEY"
    OPTIONS=()
    case "$CLE" in
      # Client OAuth (n'expire pas) : clé éphémère générée à la volée, avec tag.
      tskey-client-*) CLE="${CLE}?ephemeral=true&preauthorized=true"
                      OPTIONS+=("--advertise-tags=${TS_TAGS:-tag:ci}") ;;
    esac
    if sudo tailscale --socket=/tmp/tailscaled.sock up --authkey="$CLE" \
         --hostname="gh-ram-sniper-${GITHUB_RUN_ID:-local}" --exit-node="$TS_EXIT_NODE" \
         --accept-dns=false --timeout=60s "${OPTIONS[@]}" >/tmp/ts-up.log 2>&1; then
      IP_MAISON=""
      for _ in 1 2 3 4 5; do
        IP_MAISON="$(ip_de 127.0.0.1:1055)"
        [ -n "$IP_MAISON" ] && break
        sleep 3
      done
      if [ -n "$IP_MAISON" ] && [ "$IP_MAISON" != "$IP_DIRECTE" ]; then
        echo "SNIPER_PROXY_MAISON=socks5h://127.0.0.1:1055" >> "$SORTIE_ENV"
        ACTIVES+=(maison)
        echo "  ✅ active : le trafic Vinted/Leboncoin sort par ta box"
      else
        echo "  ⚠️ connecté, mais le trafic ne sort pas par l'appareil maison"
        echo "     → appareil éteint, ou « Use as exit node » non coché dans la console Tailscale"
      fi
    else
      echo "  ⚠️ connexion Tailscale refusée (clé expirée ou invalide ?) :"
      tail -n 3 /tmp/ts-up.log | masquer | sed 's/^/     /'
    fi
  fi
else
  echo "▶ Sortie maison : non configurée (secrets TS_AUTHKEY et TS_EXIT_NODE, README §10)"
fi

# ─────────────────────── 2. Sortie WARP (Cloudflare) ───────────────────────
MODE_WARP="${SNIPER_WARP:-auto}"
case "$MODE_WARP" in
  toujours|oui|1|true) MODE_WARP=oui ;;
  jamais|non|0|false)  MODE_WARP=non ;;
  *) if python3 reseau.py besoin-secours >/dev/null 2>&1; then MODE_WARP=oui; else MODE_WARP=non; fi ;;
esac
if [ "$MODE_WARP" = "oui" ]; then
  echo "▶ Sortie WARP (Cloudflare, gratuite)"
  if ! command -v warp-cli >/dev/null 2>&1; then
    {
      curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
        | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
      sudo apt-get update -qq
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflare-warp
    } >/tmp/warp-install.log 2>&1 || echo "  ⚠️ installation de WARP impossible"
  fi
  if command -v warp-cli >/dev/null 2>&1; then
    W="warp-cli --accept-tos"
    for _ in 1 2 3 4 5 6; do $W status >/dev/null 2>&1 && break; sleep 2; done
    # Deux syntaxes selon la version du client : la récente d'abord.
    $W registration new >/dev/null 2>&1 || $W register >/dev/null 2>&1
    $W mode proxy >/dev/null 2>&1 || $W set-mode proxy >/dev/null 2>&1
    $W proxy port 40000 >/dev/null 2>&1 || $W set-proxy-port 40000 >/dev/null 2>&1
    $W connect >/dev/null 2>&1
    OK=""
    for _ in $(seq 1 12); do
      REPONSE="$(trace 127.0.0.1:40000)"
      IP_WARP="$(printf '%s\n' "$REPONSE" | sed -n 's/^ip=//p')"
      if printf '%s\n' "$REPONSE" | grep -q '^warp=on' \
         || { [ -n "$IP_WARP" ] && [ "$IP_WARP" != "$IP_DIRECTE" ]; }; then
        OK=1; break
      fi
      sleep 2
    done
    if [ -n "$OK" ]; then
      echo "SNIPER_PROXY_WARP=socks5h://127.0.0.1:40000" >> "$SORTIE_ENV"
      ACTIVES+=(warp)
      echo "  ✅ active"
    else
      echo "  ⚠️ WARP installé mais ne répond pas"
    fi
  fi
else
  echo "▶ Sortie WARP : pas nécessaire pour l'instant (direct non bloqué récemment)"
fi

# ─────────────────────── 3. Proxys payants ───────────────────────
if [ -n "${SNIPER_PROXY_URLS:-}${SNIPER_PROXY_URL:-}" ]; then
  echo "▶ Proxy(s) payant(s) : configuré(s), utilisés en dernier recours"
fi

echo "Sorties de secours actives : ${ACTIVES[*]:-aucune} (+ connexion directe)"
exit 0
