# ═══════════════════════════════════════════════════════════════════════════
# install_windows.ps1 — Installation complète de SOLDIER / RAM SNIPER
# ═══════════════════════════════════════════════════════════════════════════
# Repart d'une base propre : supprime une éventuelle installation précédente,
# reclone le dépôt, crée l'environnement Python DANS le projet, installe les
# dépendances, prépare la base et lance la configuration Telegram.
#
# Usage — clic droit sur le fichier → « Exécuter avec PowerShell », ou :
#     powershell -ExecutionPolicy Bypass -File install_windows.ps1
#
# L'erreur la plus fréquente est de lancer les commandes depuis
# C:\Users\<nom> au lieu du dossier du projet : Python ne trouve alors aucun
# fichier. Ce script se place systématiquement au bon endroit.
# ═══════════════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"
$Projet = Join-Path $HOME "SOLDIER"
$Depot  = "https://github.com/dais-heroique/SOLDIER.git"

function Etape($n, $texte) { Write-Host "`n[$n] $texte" -ForegroundColor Cyan }
function Bon($texte)       { Write-Host "  OK  $texte" -ForegroundColor Green }
function Mauvais($texte)   { Write-Host "  KO  $texte" -ForegroundColor Red }

Write-Host "===========================================================" -ForegroundColor White
Write-Host "   RAM SNIPER - installation Windows" -ForegroundColor White
Write-Host "===========================================================" -ForegroundColor White

# ── Prérequis ──
Etape 1 "Vérification de Python et Git"

$python = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $v = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0) { $python = $cmd; Bon "$cmd → $v"; break }
    } catch { }
}
if (-not $python) {
    Mauvais "Python introuvable."
    Write-Host "     Installe-le depuis https://www.python.org/downloads/"
    Write-Host "     IMPORTANT : coche « Add python.exe to PATH » pendant l'installation,"
    Write-Host "     puis ferme et rouvre PowerShell avant de relancer ce script."
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}

try {
    $gitv = git --version 2>&1
    Bon "git → $gitv"
} catch {
    Mauvais "Git introuvable."
    Write-Host "     Installe-le depuis https://git-scm.com/download/win"
    Write-Host "     puis ferme et rouvre PowerShell avant de relancer ce script."
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}

# ── Nettoyage ──
Etape 2 "Nettoyage de l'ancienne installation"

# Un venv créé par erreur dans le dossier personnel : c'est la trace de
# commandes lancées hors du projet, il ne sert à rien et sème la confusion.
$venvEgare = Join-Path $HOME "venv"
if (Test-Path $venvEgare) {
    Remove-Item -Recurse -Force $venvEgare
    Bon "environnement Python égaré supprimé : $venvEgare"
}

if (Test-Path $Projet) {
    Write-Host "  Un dossier existe déjà : $Projet" -ForegroundColor Yellow
    $rep = Read-Host "  Le supprimer et repartir de zéro ? (o/N)"
    if ($rep -match '^[oOyY]') {
        Remove-Item -Recurse -Force $Projet
        Bon "ancien dossier supprimé"
    } else {
        Write-Host "  Installation annulée." -ForegroundColor Yellow
        Read-Host "`nAppuie sur Entrée pour fermer"
        exit 0
    }
}

# ── Clone ──
Etape 3 "Téléchargement du projet"
Set-Location $HOME
git clone $Depot SOLDIER
if (-not (Test-Path (Join-Path $Projet "ram_sniper.py"))) {
    Mauvais "le téléchargement a échoué (dépôt privé ? pas d'accès GitHub ?)"
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}
Set-Location $Projet
Bon "projet installé dans $Projet"

# ── Environnement Python ──
Etape 4 "Création de l'environnement Python"
& $python -m venv venv
$Py = Join-Path $Projet "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Mauvais "environnement Python non créé"
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}
Bon "environnement créé dans le projet (et non dans le dossier personnel)"

# ── Dépendances ──
Etape 5 "Installation des dépendances (1 à 3 minutes)"
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install --quiet pyyaml pillow flask curl-cffi lbc
if ($LASTEXITCODE -ne 0) {
    Mauvais "installation des dépendances échouée"
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}
Bon "dépendances installées"

# ── Base de données ──
Etape 6 "Préparation de la base de références"
& $Py ram_db.py seed

# ── Tests ──
Etape 7 "Vérification"
& $Py test_ram_sniper.py
if ($LASTEXITCODE -ne 0) {
    Mauvais "des tests ont échoué — envoie cette sortie à Thibault avant de continuer"
    Read-Host "`nAppuie sur Entrée pour fermer"
    exit 1
}

# ── Configuration Telegram ──
Etape 8 "Configuration Telegram"
Write-Host ""
Write-Host "  Prépare le token du bot (Thibault te l'a envoyé)." -ForegroundColor Yellow
Write-Host "  Quand le script demandera d'écrire au bot :" -ForegroundColor Yellow
Write-Host "  va dans le GROUPE Telegram et écris  /start@Ramddr4bot" -ForegroundColor Yellow
Write-Host "  (un simple « test » ne suffit pas dans un groupe)" -ForegroundColor Yellow
Write-Host ""
& $Py ram_setup.py

# ── Fin ──
Write-Host "`n===========================================================" -ForegroundColor White
Write-Host "   Terminé" -ForegroundColor Green
Write-Host "===========================================================" -ForegroundColor White
Write-Host ""
Write-Host "  Lancer le bot :" -ForegroundColor White
Write-Host "      cd $Projet"
Write-Host "      .\venv\Scripts\python.exe ram_sniper.py"
Write-Host ""
Write-Host "  Le faire démarrer tout seul avec Windows :" -ForegroundColor White
Write-Host "      .\venv\Scripts\python.exe ram_service.py install"
Write-Host ""
Read-Host "Appuie sur Entrée pour fermer"
