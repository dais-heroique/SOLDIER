"""
setup.py — Assistant d'installation interactif pour PC Flip Sniper
═══════════════════════════════════════════════════════════════════════════
Lance ce script après avoir cloné le repo pour tout configurer avec des
menus à flèches (pays, langue, ville pour Facebook Marketplace, clés eBay,
plateformes actives...) — pas besoin de modifier des fichiers à la main.

    python3 setup.py          (macOS/Linux)
    python setup.py           (Windows)

Fonctionne sur macOS, Windows et Linux (questionary est multiplateforme).
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_questionary():
    """Installe questionary si absent (nécessaire pour les menus à flèches),
    avant même de pouvoir afficher le premier menu."""
    try:
        import questionary  # noqa
        return
    except ImportError:
        print("📦 Installation de la dépendance du setup (questionary)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "questionary", "-q"], check=False)
        try:
            import questionary  # noqa
        except ImportError:
            print("❌ Impossible d'installer questionary automatiquement.")
            print("   Lance manuellement: pip install questionary")
            print("   puis relance: python3 setup.py")
            sys.exit(1)


_ensure_questionary()
import questionary
from questionary import Style

CUSTOM_STYLE = Style([
    ("qmark", "fg:#00ff88 bold"),
    ("question", "bold"),
    ("answer", "fg:#00ff88 bold"),
    ("pointer", "fg:#00ff88 bold"),
    ("highlighted", "fg:#00ff88 bold"),
    ("selected", "fg:#00ff88"),
])


def banner():
    print(r"""
   ______  ______   ________      _   ____                         
  / __ \ \/ / __ \/ ____/ __ \    / | / (_)___  ___  _____
 / /_/ /\  / /_/ / /   / /_/ /   /  |/ / / __ \/ _ \/ ___/
/ ____/ / / _, _/ /___/ ____/   / /|  / / /_/ /  __/ /
\/     /_/_/ |_|\____/_/       /_/ |_/_/ .___/\___/_/
                                       /_/
""")
    print("🎯 Assistant d'installation — PC Flip Sniper\n")


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=HERE, **kwargs)


def detect_python():
    for candidate in ("python3", "python"):
        try:
            r = subprocess.run([candidate, "--version"], capture_output=True, text=True)
            if r.returncode == 0:
                return candidate
        except FileNotFoundError:
            continue
    return sys.executable


def step_venv():
    venv_dir = os.path.join(HERE, "venv")
    if os.path.isdir(venv_dir):
        print("✅ Environnement virtuel déjà présent (venv/) — étape ignorée.\n")
        return
    if not questionary.confirm(
        "Créer l'environnement virtuel Python maintenant ?", default=True, style=CUSTOM_STYLE
    ).ask():
        print("⚠️  Ignoré — crée-le toi-même avec: python3 -m venv venv\n")
        return
    py = detect_python()
    print(f"📦 Création du venv avec {py}...")
    run([py, "-m", "venv", "venv"])
    print("✅ Environnement virtuel créé.\n")


def venv_python():
    if sys.platform == "win32":
        p = os.path.join(HERE, "venv", "Scripts", "python.exe")
    else:
        p = os.path.join(HERE, "venv", "bin", "python3")
    return p if os.path.exists(p) else sys.executable


def step_base_deps():
    if not questionary.confirm(
        "Installer les dépendances de base (flask, lbc, curl-cffi) ?", default=True, style=CUSTOM_STYLE
    ).ask():
        return
    print("📦 Installation en cours (peut prendre une minute)...")
    run([venv_python(), "-m", "pip", "install", "-q", "lbc", "flask", "curl-cffi"])
    print("✅ Dépendances de base installées.\n")


def step_optional_deps():
    choices = questionary.checkbox(
        "Fonctionnalités optionnelles à installer (espace pour cocher, entrée pour valider) :",
        choices=[
            questionary.Choice("PCPartPicker (prix neufs réels)", value="pcpp", checked=True),
            questionary.Choice("Claude Code / MCP (piloter le sniper depuis Claude Code)", value="mcp", checked=True),
        ],
        style=CUSTOM_STYLE,
    ).ask() or []

    if "pcpp" in choices:
        print("📦 Installation de PCPartPicker_API...")
        run([venv_python(), "-m", "pip", "install", "-q",
             "git+https://github.com/nynhex/PCPartPicker-API.git"])
    if "mcp" in choices:
        print("📦 Installation de mcp...")
        run([venv_python(), "-m", "pip", "install", "-q", "mcp"])
    print()
    return choices


def step_country():
    from countries import list_countries
    countries = list_countries()
    choice = questionary.select(
        "Dans quel pays cherches-tu des annonces ?",
        choices=[questionary.Choice(f"{c['label']} ({c['code']})", value=c["code"]) for c in countries],
        style=CUSTOM_STYLE,
    ).ask()
    return choice or "FR"


def step_location(country_code):
    print("\nℹ️  Facebook Marketplace cherche uniquement autour d'un point précis")
    print("   (pas dans tout le pays) — indique ta ville pour de bons résultats.\n")
    loc = questionary.text(
        "Ta ville (ou code postal) pour Facebook Marketplace :",
        default="",
        style=CUSTOM_STYLE,
    ).ask()
    radius = questionary.select(
        "Rayon de recherche autour de cette position :",
        choices=["8 km", "16 km", "32 km", "50 km", "80 km", "160 km"],
        default="16 km",
        style=CUSTOM_STYLE,
    ).ask()
    radius_km = int((radius or "16 km").split()[0])
    return (loc or "").strip(), radius_km


def step_lang():
    choice = questionary.select(
        "Langue du dashboard :",
        choices=[questionary.Choice("Français", value="fr"), questionary.Choice("English", value="en")],
        style=CUSTOM_STYLE,
    ).ask()
    return choice or "fr"


def step_platforms(country_code):
    from countries import get_country_config
    cfg = get_country_config(country_code)
    default_checked = {
        "lbc": cfg.get("has_lbc", False),
        "vinted": cfg.get("has_vinted", False),
        "ebay": True,
        "facebook": True,
    }
    labels = {
        "lbc": "Leboncoin" + ("" if cfg.get("has_lbc") else "  (indisponible hors France)"),
        "vinted": "Vinted" + ("" if cfg.get("has_vinted") else "  (indisponible dans ce pays)"),
        "ebay": "eBay",
        "facebook": "Facebook Marketplace",
    }
    choices = questionary.checkbox(
        "Plateformes à scanner (espace pour cocher/décocher) :",
        choices=[questionary.Choice(labels[k], value=k, checked=default_checked[k]) for k in
                 ("lbc", "vinted", "ebay", "facebook")],
        style=CUSTOM_STYLE,
    ).ask() or []
    return {k: (k in choices) for k in ("lbc", "vinted", "ebay", "facebook")}


def step_ebay_keys(sources):
    if not sources.get("ebay"):
        return None, None
    print("\n🔑 eBay utilise sa vraie API officielle (Browse API) — il faut deux")
    print("   identifiants gratuits depuis https://developer.ebay.com :")
    print("   'My Account' → 'Application Keys' → clé en mode PRODUCTION\n")

    if not questionary.confirm(
        "As-tu déjà (ou veux-tu créer maintenant) un compte eBay Developer ?",
        default=True, style=CUSTOM_STYLE,
    ).ask():
        print("⚠️  eBay restera désactivé — tu pourras ajouter tes clés plus tard")
        print("   dans un fichier .env, ou relancer ce setup.\n")
        return None, None

    app_id = questionary.text("App ID (Client ID) — laisse vide pour passer :", style=CUSTOM_STYLE).ask()
    if not app_id:
        return None, None
    cert_id = questionary.password("Cert ID (Client Secret) :", style=CUSTOM_STYLE).ask()
    return app_id.strip(), (cert_id or "").strip()


def write_settings(country, lang, sources, location, radius_km):
    settings = {
        "country": country, "lang": lang, "sources": sources,
        "location": location, "radius_km": radius_km,
    }
    path = os.path.join(HERE, "sniper_settings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    print(f"✅ Paramètres enregistrés dans {os.path.basename(path)}")


def write_env(app_id, cert_id):
    if not app_id:
        return
    path = os.path.join(HERE, ".env")
    lines = [f"EBAY_APP_ID={app_id}\n"]
    if cert_id:
        lines.append(f"EBAY_CERT_ID={cert_id}\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"✅ Identifiants eBay enregistrés dans .env")
    except OSError:
        # Windows/Finder bloquent parfois la création directe de ".env" —
        # on retombe sur "env" (sans point), automatiquement reconnu par
        # ebay_client.py au démarrage.
        path2 = os.path.join(HERE, "env")
        with open(path2, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"✅ Identifiants eBay enregistrés dans env")


def final_instructions(mcp_installed):
    print("\n" + "═" * 60)
    print("🎉 Configuration terminée !")
    print("═" * 60)
    if sys.platform == "win32":
        print("\nPour lancer l'app :")
        print("  venv\\Scripts\\activate.bat")
        print("  python app.py")
        print("\nOu double-clique sur 'Lancer PC Sniper.bat'")
    else:
        print("\nPour lancer l'app :")
        print("  source venv/bin/activate")
        print("  python3 app.py")
        print("\nOu double-clique sur 'Lancer PC Sniper.command'")
    print("\nL'app s'ouvrira automatiquement sur http://localhost:8000")

    if mcp_installed and "mcp" in mcp_installed:
        py = venv_python()
        print("\nPour connecter Claude Code :")
        print(f"  claude mcp add pc-sniper -- {py} {os.path.join(HERE, 'mcp_server.py')}")

    print("\nTu peux tout reconfigurer plus tard depuis le dashboard (bouton ⚙️)")
    print("ou en relançant: python3 setup.py\n")


def main():
    banner()
    step_venv()
    step_base_deps()
    optional = step_optional_deps()

    country = step_country()
    location, radius_km = step_location(country)
    lang = step_lang()
    sources = step_platforms(country)
    app_id, cert_id = step_ebay_keys(sources)

    write_settings(country, lang, sources, location, radius_km)
    write_env(app_id, cert_id)

    final_instructions(optional)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n\n⚠️  Setup interrompu — relance 'python3 setup.py' quand tu veux.")
        sys.exit(1)
