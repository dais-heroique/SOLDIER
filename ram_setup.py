"""
ram_setup.py — Configuration guidée du RAM SNIPER
═══════════════════════════════════════════════════════════════════════════
Récupère le chat ID Telegram automatiquement, écrit le .env, et envoie un
message de test pour confirmer que la chaîne fonctionne de bout en bout.

    python3 ram_setup.py

Rien à chercher à la main : tu colles le token du bot, tu envoies « salut » à
ton bot depuis Telegram, et le script trouve le reste.

L'API Gemini est FACULTATIVE. Sans elle, le module tourne en mode texte seul :
détection, scoring et notifications fonctionnent, seule la vérification par
l'image est absente (les messages le disent explicitement au lieu d'annoncer
une analyse qui n'arriverait jamais).
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
CONFIG_FILE = os.path.join(BASE_DIR, "ram_config.yaml")
LOCAL_FILE = os.path.join(BASE_DIR, "ram_config.local.yaml")

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

try:
    import ram_config
    _HAS_RAM_CONFIG = True
except Exception:
    _HAS_RAM_CONFIG = False

API = "https://api.telegram.org/bot{token}/{methode}"

# Renseigné après validation du token : sert à afficher la bonne commande
# « /start@monbot » quand la configuration vise un groupe.
_bot_username = None


# ─────────────────────── AFFICHAGE ───────────────────────
def titre(texte):
    print(f"\n\033[1m{'─' * 4} {texte} {'─' * max(4, 58 - len(texte))}\033[0m")


def ok(texte):
    print(f"  \033[32m✅\033[0m {texte}")


def ko(texte):
    print(f"  \033[31m❌\033[0m {texte}")


def info(texte):
    print(f"     \033[90m{texte}\033[0m")


def demander(question, defaut=None):
    suffixe = f" [{defaut}]" if defaut else ""
    try:
        reponse = input(f"  \033[1m?\033[0m {question}{suffixe} : ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\nAbandon.")
        sys.exit(1)
    return reponse or defaut or ""


def oui_non(question, defaut=True):
    d = "O/n" if defaut else "o/N"
    reponse = demander(f"{question} ({d})", "")
    if not reponse:
        return defaut
    return reponse.lower().startswith(("o", "y"))


# ─────────────────────── TELEGRAM ───────────────────────
def appel(token, methode, params=None, timeout=20):
    url = API.format(token=token, methode=methode)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            detail = f"HTTP {e.code}"
        raise RuntimeError(detail)
    except urllib.error.URLError as e:
        raise RuntimeError(f"réseau : {e.reason}")
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "erreur inconnue"))
    return data.get("result")


def envoyer(token, chat_id, texte):
    donnees = json.dumps({"chat_id": chat_id, "text": texte,
                          "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, methode="sendMessage"),
                                 data=donnees,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def valider_token(token):
    """Retourne les infos du bot, ou lève avec un message clair."""
    return appel(token, "getMe")


def chercher_chat_ids(token):
    """Extrait tous les chats connus du bot depuis getUpdates.

    Fonctionne pour un chat privé comme pour un groupe. Un groupe a un id
    négatif — c'est normal, ce n'est pas une erreur de saisie.
    """
    updates = appel(token, "getUpdates", {"timeout": 0, "limit": 100}) or []
    chats = {}
    for u in updates:
        for cle in ("message", "edited_message", "channel_post", "my_chat_member",
                    "callback_query"):
            bloc = u.get(cle)
            if not bloc:
                continue
            chat = bloc.get("chat") or (bloc.get("message") or {}).get("chat")
            if not chat or not chat.get("id"):
                continue
            cid = str(chat["id"])
            nom = (chat.get("title")
                   or " ".join(filter(None, [chat.get("first_name"),
                                             chat.get("last_name")]))
                   or chat.get("username") or "sans nom")
            chats[cid] = {"id": cid, "nom": nom, "type": chat.get("type", "?")}
    return list(chats.values())


def attendre_chat_id(token, tentatives=30, intervalle=2.0):
    """Boucle d'attente : l'utilisateur écrit au bot, on détecte le chat.

    Telegram ne donne AUCUN moyen de connaître un chat id avant que le chat
    n'existe : un bot ne peut pas écrire le premier. D'où cette attente.
    """
    print()
    info("Ouvre Telegram et écris au bot :")
    info("  • chat privé  → n'importe quel message")
    info(f"  • GROUPE      → écris « /start@{_bot_username or 'tonbot'} »")
    info("    Un bot en groupe ne reçoit par défaut QUE les messages qui le")
    info("    mentionnent : un simple « test » resterait invisible pour lui.")
    print()
    for i in range(tentatives):
        try:
            chats = chercher_chat_ids(token)
        except RuntimeError as e:
            ko(f"lecture des messages impossible : {e}")
            return None
        if chats:
            print()
            return chats
        reste = (tentatives - i) * intervalle
        print(f"\r  ⏳ en attente d'un message… ({reste:.0f}s restantes)  ",
              end="", flush=True)
        time.sleep(intervalle)
    print()
    return []


# ─────────────────────── FICHIERS ───────────────────────
def lire_env():
    valeurs = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, _, val = ligne.partition("=")
                    valeurs[cle.strip()] = val.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return valeurs


def ecrire_env(nouvelles):
    """Réécrit .env en préservant les clés existantes non concernées (le
    fichier peut contenir des secrets d'autres modules SOLDIER)."""
    existantes = lire_env()
    existantes.update({k: v for k, v in nouvelles.items() if v})

    lignes = ["# Secrets RAM SNIPER — ne jamais commiter ce fichier.",
              f"# Généré par ram_setup.py le {time.strftime('%Y-%m-%d %H:%M')}",
              ""]
    for cle in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY"):
        valeur = existantes.pop(cle, "")
        lignes.append(f"{cle}={valeur}")
    if existantes:
        lignes += ["", "# Autres secrets préservés"]
        lignes += [f"{k}={v}" for k, v in sorted(existantes.items())]

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")
    os.chmod(ENV_FILE, 0o600)      # lisible par toi seul


def basculer_vision(actif):
    """Active ou coupe la couche vision, via ram_config.local.yaml.

    ⚠️ On n'écrit JAMAIS dans ram_config.yaml : ce fichier est suivi par git, et
    le modifier ferait échouer chaque « git pull » avec « your local changes
    would be overwritten by merge ». Les réglages propres à cette machine vont
    dans ram_config.local.yaml, qui est ignoré par git et surcharge le fichier
    versionné.
    """
    local = ram_config.charger_local() if _HAS_RAM_CONFIG else {}
    local.setdefault("vision", {})["actif"] = bool(actif)

    entete = ("# Réglages propres à CETTE machine — surchargent ram_config.yaml.\n"
              "# Fichier ignoré par git : il ne bloquera jamais un « git pull ».\n"
              "# Écrit par ram_setup.py, éditable à la main sans risque.\n\n")
    try:
        if _HAS_YAML:
            corps = yaml.safe_dump(local, allow_unicode=True, sort_keys=False)
        else:
            corps = "vision:\n  actif: %s\n" % str(bool(actif)).lower()
        with open(LOCAL_FILE, "w", encoding="utf-8") as f:
            f.write(entete + corps)
        return True
    except OSError:
        return False


def nettoyer_ancienne_modif():
    """Répare le cas des installations où une version précédente de ce script
    avait modifié ram_config.yaml directement : tant que ce fichier diverge du
    dépôt, tous les « git pull » échouent."""
    import subprocess
    try:
        r = subprocess.run(["git", "diff", "--name-only", "--", "ram_config.yaml"],
                           cwd=BASE_DIR, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0 or "ram_config.yaml" not in r.stdout:
        return None

    info("ram_config.yaml a été modifié localement (par une ancienne version")
    info("de ce script). Tant qu'il diverge, « git pull » échoue.")
    if not oui_non("Restaurer la version du dépôt ? "
                   "(tes réglages passent dans ram_config.local.yaml)", True):
        return False
    try:
        subprocess.run(["git", "checkout", "--", "ram_config.yaml"],
                       cwd=BASE_DIR, capture_output=True, timeout=10, check=True)
        ok("ram_config.yaml restauré — « git pull » refonctionnera")
        return True
    except Exception as e:
        ko(f"restauration impossible : {e}")
        info("À faire à la main : git checkout -- ram_config.yaml")
        return False


# ─────────────────────── PARCOURS ───────────────────────
def main():
    print("\n\033[1m╔══════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1m║        🎯 RAM SNIPER — configuration guidée               ║\033[0m")
    print("\033[1m╚══════════════════════════════════════════════════════════╝\033[0m")

    env = lire_env()
    if os.path.exists(os.path.join(BASE_DIR, ".ram_sniper.lock")):
        ko("Un RAM SNIPER tourne déjà.")
        info("Il consomme les messages Telegram au fur et à mesure : ce script")
        info("ne verrait jamais le tien et ne pourrait pas trouver le chat ID.")
        info("Arrête-le (Ctrl+C dans son terminal) puis relance ce script.")
        if not oui_non("Continuer quand même ?", False):
            return 1
    nettoyer_ancienne_modif()

    # ── 1. Token ──
    titre("1/4 · Token du bot Telegram")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if token:
        info(f"token déjà présent : {token[:12]}…")
        if not oui_non("Le remplacer ?", False):
            pass
        else:
            token = ""
    if not token:
        info("Sur Telegram : @BotFather → /newbot → copie le token")
        info("Il ressemble à : 8123456789:AAH-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        token = demander("Colle le token ici")

    token = token.strip()
    if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", token):
        ko("Ce token n'a pas le bon format (chiffres, deux-points, puis ~35 caractères).")
        info("Vérifie que tu as copié la ligne entière donnée par @BotFather.")
        return 1

    try:
        bot = valider_token(token)
    except RuntimeError as e:
        ko(f"Token refusé par Telegram : {e}")
        info("Si c'est « Unauthorized », le token est faux ou le bot a été supprimé.")
        return 1
    global _bot_username
    _bot_username = bot.get("username")
    ok(f"Bot reconnu : @{_bot_username} ({bot.get('first_name')})")

    # ── 2. Chat ID ──
    titre("2/4 · Chat ID (là où tu recevras les alertes)")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if chat_id and not oui_non(f"Chat ID déjà configuré ({chat_id}). Le changer ?", False):
        pass
    else:
        chats = chercher_chat_ids(token)
        if not chats:
            chats = attendre_chat_id(token) or []

        if not chats:
            ko("Aucun message reçu : impossible de déterminer le chat ID.")
            info("Un bot ne peut pas écrire en premier — il faut lui parler d'abord.")
            info(f"Va sur https://t.me/{bot.get('username')} , clique DÉMARRER,")
            info("puis relance ce script.")
            return 1

        if len(chats) == 1:
            chat_id = chats[0]["id"]
            ok(f"Chat trouvé : {chats[0]['nom']} ({chats[0]['type']}) → {chat_id}")
        else:
            print("  Plusieurs chats trouvés :")
            for i, c in enumerate(chats, 1):
                print(f"     {i}. {c['nom']} ({c['type']}) — id {c['id']}")
            choix = demander(f"Lequel utiliser ? (1-{len(chats)})", "1")
            try:
                chat_id = chats[int(choix) - 1]["id"]
            except (ValueError, IndexError):
                ko("Choix invalide.")
                return 1
            ok(f"Chat retenu : {chat_id}")

    # ── 3. Gemini (facultatif) ──
    titre("3/4 · Analyse d'image Gemini (facultatif)")
    cle_gemini = env.get("GEMINI_API_KEY", "")
    if cle_gemini:
        info(f"clé déjà présente : {cle_gemini[:10]}…")
        activer_vision = True
        if oui_non("La supprimer et tourner en texte seul ?", False):
            cle_gemini, activer_vision = "", False
    else:
        info("Sans Gemini, le module tourne très bien en TEXTE SEUL :")
        info("  • détection, scoring, notifications, radar kits : identiques")
        info("  • en moins : la vérification par l'image (encoche DDR3, ECC,")
        info("    lecture du sticker) — à faire toi-même sur les photos")
        info("Tu pourras l'ajouter plus tard en relançant ce script.")
        print()
        if oui_non("Configurer Gemini maintenant ?", False):
            info("Clé gratuite : https://aistudio.google.com/app/apikey")
            cle_gemini = demander("Colle la clé Gemini").strip()
            activer_vision = bool(cle_gemini)
        else:
            activer_vision = False

    if basculer_vision(activer_vision):
        ok(f"ram_config.local.yaml → vision.actif: {str(activer_vision).lower()}")
        info("(fichier local, ignoré par git : il ne bloquera pas tes mises à jour)")
    if not activer_vision:
        info("Les notifications diront « vérifie les photos toi-même »")
        info("au lieu d'annoncer une analyse qui n'arriverait jamais.")

    # ── 4. Écriture + test réel ──
    titre("4/4 · Écriture et test")
    ecrire_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id,
                "GEMINI_API_KEY": cle_gemini})
    ok(f".env écrit ({ENV_FILE}, permissions 600)")

    try:
        envoyer(token, chat_id,
                "🎯 <b>RAM SNIPER connecté</b>\n\n"
                "Si tu lis ce message, la chaîne fonctionne.\n"
                + ("Mode complet : texte + analyse d'image."
                   if activer_vision else
                   "Mode texte seul : pas d'analyse d'image (Gemini non configuré)."))
        ok("Message de test envoyé — vérifie ton Telegram")
    except Exception as e:
        ko(f"Envoi impossible : {e}")
        info("Le .env est écrit, mais quelque chose bloque côté Telegram.")
        return 1

    # ── Récapitulatif ──
    print("\n\033[1m╔══════════════════════════════════════════════════════════╗\033[0m")
    print("\033[1m║                    ✅ TOUT EST PRÊT                       ║\033[0m")
    print("\033[1m╚══════════════════════════════════════════════════════════╝\033[0m")
    print(f"""
  Bot          @{bot.get('username')}
  Chat ID      {chat_id}
  Vision       {'Gemini activé' if activer_vision else 'désactivée (texte seul)'}

  Prochaine étape — lancer sans rien envoyer, pour voir ce que ça donne :

      venv/bin/python3 ram_sniper.py --dry-run

  Puis, quand les alertes te paraissent justes :

      venv/bin/python3 ram_sniper.py

  Le dashboard : venv/bin/python3 app.py → http://localhost:8000/ram
""")

    try:
        import ram_config
        manquants = ram_config.get(force=True) and ram_config.secrets_manquants()
        if manquants:
            print(f"  ⚠️  Il manque encore : {', '.join(manquants)}\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
