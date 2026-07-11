#!/bin/bash
# Double-clique ce fichier pour lancer PC Flip Sniper.
# (La première fois : clic droit → Ouvrir, pour autoriser macOS à l'exécuter.)

cd "$(dirname "$0")"

# Active le venv s'il existe à côté, sinon dans ~/Downloads
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d "$HOME/Downloads/venv" ]; then
    source "$HOME/Downloads/venv/bin/activate"
fi

# Installe Flask si absent
python3 -c "import flask" 2>/dev/null || pip install flask

echo ""
echo "  🎯 Lancement de PC Flip Sniper…"
echo "  L'app va s'ouvrir dans ton navigateur sur http://localhost:8000"
echo "  Garde cette fenêtre ouverte tant que tu veux que le scan tourne."
echo "  Pour arrêter : ferme cette fenêtre ou Ctrl+C."
echo ""

python3 app.py
