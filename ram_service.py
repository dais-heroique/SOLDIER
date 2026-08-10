"""
ram_service.py — Faire tourner le RAM SNIPER en continu, en tâche de fond
═══════════════════════════════════════════════════════════════════════════
Installe le bot comme service système : il démarre à l'ouverture de session,
redémarre tout seul s'il s'arrête, et survit à un redémarrage de la machine.

    python3 ram_service.py install     # installe et démarre
    python3 ram_service.py status      # tourne-t-il ? depuis quand ?
    python3 ram_service.py logs        # les 50 dernières lignes
    python3 ram_service.py restart
    python3 ram_service.py uninstall

macOS  → agent launchd (~/Library/LaunchAgents)
Linux  → unité systemd utilisateur (~/.config/systemd/user)

── Pourquoi sur TA machine et pas sur un serveur gratuit ──
Vinted est protégé par Datadome, qui bloque massivement les adresses IP de
centres de données (AWS, Google Cloud, Oracle, Azure…). Le scraper fonctionne
depuis une connexion résidentielle ; depuis un VPS gratuit il se prend des
HTTP 403 en quelques minutes. Ce n'est pas un défaut du code : c'est le
principe même de Datadome. Voir README_RAM_SNIPER.md § « Tourner en continu ».
"""

import os
import platform
import shutil
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_OUT = os.path.join(LOG_DIR, "ram_sniper.log")
LOG_ERR = os.path.join(LOG_DIR, "ram_sniper.err.log")

LABEL = "com.soldier.ramsniper"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
UNITE = os.path.expanduser("~/.config/systemd/user/ram-sniper.service")

MACOS = platform.system() == "Darwin"
LINUX = platform.system() == "Linux"


def python_venv():
    """L'interpréteur du venv du projet, sinon celui qui exécute ce script."""
    candidat = os.path.join(BASE_DIR, "venv", "bin", "python3")
    return candidat if os.path.exists(candidat) else sys.executable


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ─────────────────────── macOS (launchd) ───────────────────────
def _plist_contenu():
    py = python_venv()
    # caffeinate empêche la mise en veille : un Mac endormi ne scanne rien.
    #   -i : pas de veille par inactivité
    #   -s : pas de veille système tant que la machine est sur secteur
    # Sur batterie, macOS finit par s'endormir malgré tout — c'est voulu, on ne
    # va pas vider la batterie pour surveiller des barrettes de RAM.
    args = ["/usr/bin/caffeinate", "-i", "-s", py,
            os.path.join(BASE_DIR, "ram_sniper.py")]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>

    <key>WorkingDirectory</key>
    <string>{BASE_DIR}</string>

    <!-- Redémarre le bot s'il s'arrête, quelle qu'en soit la raison -->
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>

    <!-- Laisse 30 s entre deux tentatives : si le disque externe n'est pas
         monté, inutile de boucler à pleine vitesse -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>{LOG_OUT}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_ERR}</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _launchctl(*args):
    return _run(["launchctl", *args])


def install_macos():
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PLIST, "w", encoding="utf-8") as f:
        f.write(_plist_contenu())
    print(f"✅ agent écrit : {PLIST}")

    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{LABEL}")          # au cas où il existait
    r = _launchctl("bootstrap", f"gui/{uid}", PLIST)
    if r.returncode != 0:
        # Anciennes versions de macOS
        r = _launchctl("load", "-w", PLIST)
    if r.returncode != 0:
        print(f"❌ démarrage impossible : {r.stderr.strip() or r.stdout.strip()}")
        return False
    _launchctl("kickstart", f"gui/{uid}/{LABEL}")
    print("✅ service démarré (et relancé automatiquement au besoin)")
    return True


def uninstall_macos():
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/{LABEL}")
    _launchctl("unload", "-w", PLIST)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("✅ service arrêté et désinstallé")
    return True


def status_macos():
    uid = os.getuid()
    r = _launchctl("print", f"gui/{uid}/{LABEL}")
    if r.returncode != 0:
        return {"installe": os.path.exists(PLIST), "actif": False,
                "detail": "service non chargé"}
    pid = etat = None
    for ligne in r.stdout.splitlines():
        ligne = ligne.strip()
        if ligne.startswith("pid = "):
            pid = ligne.split("=", 1)[1].strip()
        elif ligne.startswith("state = "):
            etat = ligne.split("=", 1)[1].strip()
    return {"installe": True, "actif": bool(pid), "pid": pid, "detail": etat}


# ─────────────────────── Linux (systemd utilisateur) ───────────────────────
def _unite_contenu():
    py = python_venv()
    return f"""[Unit]
Description=RAM SNIPER — veille DDR4 Vinted/Leboncoin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={BASE_DIR}
ExecStart={py} {os.path.join(BASE_DIR, "ram_sniper.py")}
Restart=always
RestartSec=30
StandardOutput=append:{LOG_OUT}
StandardError=append:{LOG_ERR}

[Install]
WantedBy=default.target
"""


def install_linux():
    os.makedirs(os.path.dirname(UNITE), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(UNITE, "w", encoding="utf-8") as f:
        f.write(_unite_contenu())
    print(f"✅ unité écrite : {UNITE}")
    _run(["systemctl", "--user", "daemon-reload"])
    r = _run(["systemctl", "--user", "enable", "--now", "ram-sniper.service"])
    if r.returncode != 0:
        print(f"❌ démarrage impossible : {r.stderr.strip()}")
        return False
    print("✅ service démarré")
    # Sans « linger », systemd arrête les services utilisateur à la déconnexion.
    lingerer = _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    if lingerer.returncode == 0:
        print("✅ linger activé : le service survit à la déconnexion")
    else:
        print("⚠️  active le linger pour qu'il tourne hors session :")
        print(f"     sudo loginctl enable-linger {os.environ.get('USER', '')}")
    return True


def uninstall_linux():
    _run(["systemctl", "--user", "disable", "--now", "ram-sniper.service"])
    if os.path.exists(UNITE):
        os.remove(UNITE)
    _run(["systemctl", "--user", "daemon-reload"])
    print("✅ service arrêté et désinstallé")
    return True


def status_linux():
    r = _run(["systemctl", "--user", "is-active", "ram-sniper.service"])
    actif = r.stdout.strip() == "active"
    d = _run(["systemctl", "--user", "show", "ram-sniper.service",
              "--property=MainPID,NRestarts,ActiveEnterTimestamp"])
    detail = {}
    for ligne in d.stdout.splitlines():
        if "=" in ligne:
            k, _, v = ligne.partition("=")
            detail[k] = v
    return {"installe": os.path.exists(UNITE), "actif": actif,
            "pid": detail.get("MainPID"), "redemarrages": detail.get("NRestarts"),
            "detail": detail.get("ActiveEnterTimestamp")}


# ─────────────────────── COMMUN ───────────────────────
def verifier_prerequis():
    """Ce qui doit être en place avant d'installer un service : rien de pire
    qu'un service qui redémarre en boucle sans qu'on sache pourquoi."""
    problemes = []
    if not os.path.exists(os.path.join(BASE_DIR, "ram_sniper.py")):
        problemes.append("ram_sniper.py introuvable")
    if not os.path.exists(os.path.join(BASE_DIR, ".env")):
        problemes.append(".env absent — lance d'abord : python3 ram_setup.py")
    py = python_venv()
    if not os.path.exists(py):
        problemes.append(f"interpréteur introuvable : {py}")

    # Le projet vit souvent sur un disque externe : s'il n'est pas monté au
    # démarrage, le service tournera à vide.
    if BASE_DIR.startswith("/Volumes/"):
        volume = "/".join(BASE_DIR.split("/")[:3])
        problemes.append(f"ℹ️  projet sur le volume externe {volume} : le service "
                         f"ne démarrera que si ce disque est monté")
    return problemes


def afficher_logs(lignes=50):
    for chemin, libelle in ((LOG_OUT, "sortie"), (LOG_ERR, "erreurs")):
        if not os.path.exists(chemin):
            continue
        taille = os.path.getsize(chemin)
        if not taille:
            continue
        print(f"\n── {libelle} ({chemin}, {taille // 1024} Ko) ──")
        with open(chemin, encoding="utf-8", errors="replace") as f:
            for ligne in f.readlines()[-lignes:]:
                print("  " + ligne.rstrip())


def purger_logs(max_mo=20):
    """launchd et systemd n'assurent aucune rotation : sans ça, le fichier
    grossit indéfiniment sur un disque qui n'est pas forcément grand."""
    for chemin in (LOG_OUT, LOG_ERR):
        if os.path.exists(chemin) and os.path.getsize(chemin) > max_mo * 1024 * 1024:
            archive = chemin + ".1"
            shutil.move(chemin, archive)
            print(f"  log archivé : {archive}")


def status():
    fn = status_macos if MACOS else status_linux
    etat = fn()
    print(f"\n── Service RAM SNIPER ({platform.system()}) ──")
    print(f"  installé : {'oui' if etat.get('installe') else 'non'}")
    print(f"  actif    : {'✅ oui' if etat.get('actif') else '❌ non'}")
    if etat.get("pid"):
        print(f"  pid      : {etat['pid']}")
    if etat.get("redemarrages"):
        print(f"  redémarrages : {etat['redemarrages']}")
    if etat.get("detail"):
        print(f"  détail   : {etat['detail']}")

    # Battement écrit par les workers : la preuve que le bot travaille vraiment,
    # et pas seulement que le processus existe.
    sante = os.path.join(BASE_DIR, ".ram_sante.json")
    if os.path.exists(sante):
        import json
        try:
            with open(sante, encoding="utf-8") as f:
                d = json.load(f)
            age = time.time() - float(d.get("maj_le") or 0)
            print(f"  battement: il y a {int(age)} s "
                  f"{'✅' if age < 120 else '⚠️ (bot bloqué ?)'}")
            for nom, w in (d.get("workers") or {}).items():
                marque = "✅" if w.get("etat") == "actif" else "⚠️"
                extra = f" · {w['redemarrages']} redémarrage(s)" if w.get("redemarrages") else ""
                print(f"     {marque} {nom}{extra}")
            s = d.get("stats") or {}
            print(f"  activité : {s.get('annonces_nouvelles', 0)} annonce(s), "
                  f"{s.get('notifiees', 0)} notification(s)")
        except (ValueError, OSError):
            pass
    print(f"\n  logs : {LOG_OUT}")
    return etat


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if not (MACOS or LINUX):
        print(f"❌ {platform.system()} non pris en charge.")
        print("   Lance simplement : python3 ram_sniper.py")
        return 1

    if cmd == "install":
        problemes = verifier_prerequis()
        bloquants = [p for p in problemes if not p.startswith("ℹ️")]
        for p in problemes:
            print(("  " if p.startswith("ℹ️") else "  ❌ ") + p)
        if bloquants:
            print("\nCorrige ces points avant d'installer le service.")
            return 1
        purger_logs()
        ok = install_macos() if MACOS else install_linux()
        if ok:
            print("\nLe bot tourne maintenant en fond, et redémarrera tout seul.")
            print("  État  : python3 ram_service.py status")
            print("  Logs  : python3 ram_service.py logs")
            if MACOS:
                print("\n⚠️  Sur batterie, macOS finira par se mettre en veille et")
                print("    le scan s'interrompra. Laisse le Mac branché pour une")
                print("    surveillance réellement continue.")
        return 0 if ok else 1

    if cmd == "uninstall":
        return 0 if (uninstall_macos() if MACOS else uninstall_linux()) else 1

    if cmd == "restart":
        if MACOS:
            _launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
        else:
            _run(["systemctl", "--user", "restart", "ram-sniper.service"])
        print("✅ service redémarré")
        return 0

    if cmd == "logs":
        afficher_logs(int(sys.argv[2]) if len(sys.argv) > 2 else 50)
        return 0

    if cmd == "status":
        status()
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
