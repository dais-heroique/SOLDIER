# 🎯 Stratégie Achat-Revente PC par Composant

Guide complet pour faire du flip de pièces PC sur **Leboncoin + Vinted** avec le PC Flip Sniper.

---

## 1. Installation sur ton disque Data3

Tu as un disque externe **Data3** (6 To, vide) — l'endroit idéal pour tout faire tourner, puisque le sniper garde maintenant **tout pour toujours** (voir section 11).

**1. Trouve le chemin exact du disque.** Sur macOS, un disque externe se monte sous `/Volumes/` :
```bash
ls /Volumes/
```
Tu dois voir `Data3` dans la liste — son chemin complet est `/Volumes/Data3`.

**2. Crée le dossier du projet dessus** et mets-y tous les fichiers (liste en section 16) :
```bash
mkdir -p /Volumes/Data3/PC-Sniper
```
Glisse tous les fichiers `.py` + `Lancer PC Sniper.command` + ce guide dedans via le Finder, ou déplace ton dossier existant :
```bash
mv ~/Downloads/PC-Sniper/*.py /Volumes/Data3/PC-Sniper/
mv "~/Downloads/PC-Sniper/Lancer PC Sniper.command" /Volumes/Data3/PC-Sniper/
```

**3. Recrée l'environnement Python DIRECTEMENT sur Data3** (un `venv` copié depuis ailleurs ne fonctionne pas — ses chemins internes sont figés à l'endroit où il a été créé) :
```bash
cd /Volumes/Data3/PC-Sniper
/opt/homebrew/opt/python@3.11/bin/python3.11 -m venv venv
source venv/bin/activate
pip install lbc flask curl-cffi
```

**4. Lance l'app depuis Data3 :**
```bash
cd /Volumes/Data3/PC-Sniper
source venv/bin/activate
python3 app.py
```
Tous les fichiers de données (`deals_found.json`, `price_history.json`, les archives permanentes) se créent automatiquement **dans le dossier courant** — donc sur Data3, puisque c'est de là que tu lances l'app. Rien d'autre à configurer.

`Lancer PC Sniper.command` fonctionne aussi tel quel depuis Data3 (double-clic) — il se place tout seul dans le dossier où il se trouve, peu importe où c'est.

⚠️ **Le disque doit être branché et monté avant de lancer l'app.** S'il se débranche pendant que le scan tourne, l'app plantera en essayant d'écrire — reconnecte-le et relance.

---

## 2. Le principe

Tu n'achètes plus au hasard. Le sniper surveille **255 modèles précis** (14 catégories : GPU, CPU, cartes mères, RAM, stockage, alims, refroidissement, boîtiers, écrans, claviers, souris, casques, PC portables, sièges) en continu sur **Leboncoin ET Vinted**, et te prévient **uniquement** quand une annonce passe sous un seuil calculé sur de vrais prix de marché (section 5).

Tout est dans **un seul dashboard** avec deux onglets :
- **🔥 Deals en direct** — les bonnes affaires détectées, avec filtres et tri (section 4)
- **📊 Évaluateur de prix** — les 255 modèles avec leur prix de référence (barres or/deal/juste), et pour chacun un **graphique d'évolution du marché** + stats (min/médian/max vus)

Une **barre de recherche** filtre les deux onglets en même temps. Clique un modèle pour ouvrir sa **fiche détail** complète (jamais de redirection directe — section 7).

Trois niveaux de prix par modèle :

| Niveau | Signification | Action |
|--------|---------------|--------|
| **fair** | Prix juste (ce que ça vaut vraiment) | C'est ton prix de **revente** |
| **good** 🟢 | Bon deal — sous le marché | Tu achètes, marge correcte |
| **steal** 💎 | Affaire en or — nettement sous le marché | Tu fonces immédiatement |

**Marge = prix de revente (fair) − prix d'achat.** Affichée directement sur chaque carte.

---

## 3. Quels composants flippent le mieux

Classés par liquidité (vitesse de revente) et marge typique :

1. **Cartes graphiques** — reine du flip. Marge 40-100€ sur le milieu de gamme (RTX 3060/3070, RX 6600/6700). Liquidité maximale.
2. **Processeurs** — petits, faciles à expédier, AM4 (Ryzen 5 5600X, 5600) part en quelques heures.
3. **Kits RAM DDR4/DDR5** — prix volatils depuis la pénurie DRAM 2025-2026.
4. **Cartes mères B550 / B450** — se revendent bien en bundle avec un CPU.
5. **Watercooling AIO** (Corsair, NZXT, Arctic) — bonne marge, peu de concurrence.
6. **Alimentations 650W+** modulaires de marque.
7. **Boîtiers premium** (Lian Li O11, NZXT, Fractal) — encombrants mais bonne marge en main propre.

---

## 4. Le dashboard — filtres et tri

L'onglet **Deals** a une vraie barre d'outils :

- **Filtre catégorie** — limite à GPU, CPU, RAM, etc.
- **Filtre source** — Tout / Leboncoin / Vinted / Affaires en or
- **Prix min / max** — fourchette de prix qui t'intéresse
- **Tri** — Marge décroissante (défaut), Prix croissant, Prix décroissant, Score global décroissant, Plus récent

Tout se combine ; le compteur "X/Y deals affichés" indique combien passent le filtre.

---

## 5. D'où viennent vraiment les prix

Les prix de référence étaient au départ une estimation — utile pour démarrer, mais pas vérifiée modèle par modèle. Le sniper résout maintenant chaque prix "fair" **en couches**, la source la plus fiable disponible gagnant automatiquement :

1. 🥇 **Marché réel observé** — dès qu'au moins 15 annonces ont été vues pour un modèle, le prix médian **réellement constaté** sur Leboncoin/Vinted devient la référence.
2. 🥈 **PCPartPicker** — prix **neuf** réel récupéré via `pcpp_refresh.py` (section 6), converti en estimation occasion par une courbe de décote selon l'âge du modèle.
3. 🥉 **Estimation de départ** — utilisée seulement si rien de mieux n'est disponible.

**La source est toujours visible** : badge 🟢 marché réel / 🟡 PCPartPicker / ⚪ estimation, avec une note expliquant le calcul, dans l'évaluateur et la fiche détail.

---

## 6. Rafraîchir les prix neufs (PCPartPicker)

`pcpp_refresh.py` récupère les prix neufs actuels sur PCPartPicker et les convertit en estimation occasion.

**Installation (une fois) :**
```bash
pip install "git+https://github.com/nynhex/PCPartPicker-API.git"
```

**Lancement :**
```bash
python3 pcpp_refresh.py
```

⚠️ **Comme Leboncoin/Vinted, PCPartPicker bloque les IP de datacenter** — lance ce script depuis ton Mac, pas un serveur cloud.

Ça écrit `live_prices.json`, relu automatiquement par l'app au cycle suivant. Relance ce script une fois par semaine environ, pas besoin de le laisser tourner en continu.

---

## 7. Fiche produit dédiée

**Clique sur n'importe quel composant** → jamais de redirection directe vers l'annonce. Ça ouvre une fiche complète : badge de source, barres de référence, rapport perf/prix + revendabilité, simulateur de prix, graphique d'historique, et la liste de toutes les annonces actuelles correspondant à ce modèle (chacune avec un bouton "Ouvrir ↗" individuel).

Un bouton **"Ouvrir l'annonce ↗"** reste aussi disponible directement sur chaque carte deal, sans passer par la fiche.

---

## 8. Filtre de pertinence + anti-carton/câble/HS

Chaque annonce est vérifiée **token par token** contre le nom exact du modèle (`relevance.py`) avant d'être acceptée — une recherche "RTX 5090" ne peut plus renvoyer un laptop ou une alimentation.

Le filtre d'état (`listing_filter.py`) exclut aussi : cartons/boîtes seules, câbles/adaptateurs, waterblocks, annonces "je recherche", matériel HS/pour pièces.

Ce qui passe reçoit un score de confiance d'état (badge visible) : 🟢 bon état confirmé, 🟡 à vérifier, 🟠 peu d'info.

Chaque catégorie a un **prix plancher** (GPU 25€, CPU 10€, etc. — `MIN_PRICE` dans `market_db.py`) : en dessous, c'est presque toujours un carton, donc exclu d'office.

---

## 9. Le Rapport Perf/Prix + Revendabilité

Chaque deal est noté **/100** avec un verdict : 💎 À SAISIR · ✅ BON DEAL · 🟡 CORRECT · 🟠 MOYEN. Combine :

1. **Perf/prix** — indice de performance gaming (calibré sur les hiérarchies 2026) ÷ prix.
2. **Jouable en AAA 2026 ?** — résolution (4K/1440p/1080p) + avertissement VRAM.
3. **Revendabilité (7 paramètres)** — demande marché, marge absolue, ratio de marge, fraîcheur techno, pertinence 2026, bonus VRAM, confiance fonctionnelle.

**Simulateur de prix** (fiche détail) : glisse un curseur, le verdict se recalcule en direct.

---

## 10. MCP — utiliser Claude Code

`mcp_server.py` expose les données du sniper à **Claude Code**.

**Installation :**
```bash
pip install mcp
```

**Enregistrement :**
```bash
claude mcp add pc-sniper -- python3 /Volumes/Data3/PC-Sniper/mcp_server.py
```
Vérifie avec `claude mcp list` — tu dois voir `pc-sniper` connecté.

**Outils sur le dashboard actif :**
- `list_deals`, `search_deals`, `get_catalog`, `get_model_report`, `get_price_history`, `compare_models`, `get_stats`

**Outils sur l'archive permanente** (section 11, tout ce qui a jamais été trouvé, jamais purgé) :
- `get_archive_stats` — nombre total de deals archivés, période couverte, répartition par catégorie
- `search_archive` — cherche dans TOUT l'historique (pas que le dashboard actif)
- `get_raw_price_log` — chaque prix observé individuellement avec horodatage exact, pour une analyse fine

**Exemples de demandes dans Claude Code :**
> "Compare la RTX 3060, la RTX 3070 et la RX 6700 XT en ce moment"
> "Dans l'archive, combien de RTX 3070 sous 200€ ont été vues depuis le début ?"
> "Trace l'évolution du prix médian de la 5600X sur les 30 derniers jours à partir du log brut"

---

## 11. Stockage permanent (grâce à Data3)

Avec 6 To disponibles, l'espace disque n'est plus une contrainte — le sniper garde maintenant **tout, pour toujours**, en plus de garder le dashboard actif propre et pertinent :

- **`deals_archive.jsonl`** — CHAQUE deal jamais détecté, archivé en continu, jamais supprimé. Le dashboard "live" continue de retirer les annonces trop vieilles (probablement vendues) pour rester utile, mais rien n'est perdu : tout reste consultable dans cette archive.
- **`price_observations.jsonl`** — CHAQUE prix observé individuellement (pas juste l'agrégat quotidien), avec horodatage exact. Permet plus tard une analyse fine (volatilité, tendances par heure, saisonnalité) que l'agrégat seul ne permettait pas.
- **`price_history.json`** — agrégats quotidiens (léger, pour le graphique), rétention étendue à ~10 ans.
- **`seen_ads.json`** — mémoire anti-doublon étendue à 90 jours (avant : 30).

Le dashboard actif reste volontairement curé (`DEAL_MAX_AGE_DAYS = 45`, `DEAL_MAX_COUNT = 3000`) pour ne pas réafficher du bruit — une annonce de plusieurs mois est presque sûrement vendue. Mais elle reste pour toujours dans `deals_archive.jsonl`, interrogeable via Claude Code (section 10).

---

## 12. eBay — la troisième source

`ebay_client.py` utilise la vraie API officielle eBay (via [ebaysdk-python](https://github.com/timotheus/ebaysdk-python)) — pas de scraping, pas de blocage possible, juste une clé gratuite.

**Obtenir une clé (5 minutes) :**
1. Crée un compte sur [developer.ebay.com](https://developer.ebay.com) (gratuit)
2. "My Account" → "Application Keys" → crée une clé en mode **PRODUCTION**
3. Copie ton "App ID (Client ID)"

**Configuration :**
```bash
export EBAY_APP_ID="TonAppID-xxxx-PRD-xxxx"
```
ou crée un fichier `.env` à côté de `app.py` avec `EBAY_APP_ID=TonAppID-xxxx-PRD-xxxx`

Sans clé, eBay est simplement désactivé — le reste continue de fonctionner normalement.

**Test rapide de ta clé :**
```bash
python3 ebay_client.py
```

---

## 13. Vinted réparé — ce qui bloquait

Le problème était réel : le fingerprint TLS générique (`impersonate="chrome"`) ne correspondait à aucune version précise que Datadome reconnaît. Résultat : zéro résultat, silencieusement. Deux corrections :

1. **Fingerprint précis** — `chrome124` (avec repli automatique sur d'autres versions si Datadome évolue)
2. **Erreurs visibles** — avant, un échec Vinted était avalé en silence ; maintenant chaque erreur s'affiche dans le terminal avec le code HTTP exact

**Si Vinted ne remonte toujours rien**, lance le diagnostic autonome qui te dit précisément où ça bloque :
```bash
python3 vinted_client.py
```

---

## 14. Toutes les langues, tous les pays

**Sélecteur ⚙️ Paramètres** (bouton en haut à droite du dashboard) :
- **Langue** — Français / English, traduit intégralement (verdicts, filtres, messages d'état, tout)
- **Pays** — 31 marchés disponibles (tous les pays Vinted + eBay). Change le pays et le scan **redémarre automatiquement avec la nouvelle configuration en quelques secondes**, sans relancer l'app
- **Plateformes actives** — active/désactive Leboncoin / Vinted / eBay indépendamment (Leboncoin se désactive tout seul hors de France)

Pour un déploiement automatisé (ex: si tu distribues l'outil), la variable d'environnement `SNIPER_COUNTRY` reste prioritaire sur les paramètres sauvegardés :
```bash
SNIPER_COUNTRY=DE python3 app.py
```

---

## 15. Filtres avancés du dashboard

Onglet **Deals**, en plus du prix min/max et du tri déjà là :

- **Plateformes** (cases à cocher) — combine Leboncoin/Vinted/eBay comme tu veux, pas juste un choix exclusif
- **Affaires en or uniquement** — case à cocher séparée
- **Livraison** — Peu importe / Livraison possible / Remise en main propre uniquement
  - eBay : info exacte via l'API (fiable à 100%)
  - Vinted : toujours "livraison possible" (intégré à la plateforme)
  - Leboncoin : détecté par mots-clés dans l'annonce (moins fiable, indéterminé si rien n'est précisé)

---

## 16. Facebook Marketplace — la 4ᵉ source

`facebook_client.py` imite les requêtes internes que le site facebook.com utilise lui-même pour Marketplace — **aucun compte Facebook nécessaire**.

⚠️ Contrainte propre à Facebook : une recherche a besoin d'une **position** (Facebook ne permet pas de chercher "dans tout un pays", seulement autour d'un point, rayon 16km). Une ville par défaut est choisie automatiquement selon le pays actif (Paris pour la France, Berlin pour l'Allemagne, etc. — les 31 pays sont couverts).

**Test rapide :**
```bash
python3 facebook_client.py        # ou: python3 facebook_client.py DE pour tester l'Allemagne
```

⚠️ Facebook limite très fort le nombre de requêtes par IP (plus que Vinted/eBay). Si le diagnostic affiche "rate limit", c'est normal — patiente quelques minutes, ou lance-le simplement depuis ton Mac plutôt qu'un serveur partagé.

Comme pour Vinted, les identifiants de requête internes ("doc_id") peuvent changer si Facebook met à jour son site — le diagnostic te dira clairement si c'est le cas.

---

## 17. Ça marche dans n'importe quel pays — même les devises

31 pays couverts (section 14), et chaque source s'adapte automatiquement :

| Source | Disponibilité |
|---|---|
| Leboncoin | France uniquement (c'est un site français) |
| Vinted | ~26 marchés (se désactive tout seul là où Vinted n'existe pas, ex: Japon, Inde, Brésil) |
| eBay | Partout (site le plus proche du pays, sinon EBAY-US par défaut) |
| Facebook Marketplace | Partout (ville par défaut selon le pays) |

**Conversion de devise automatique** (`currency.py`) : si tu choisis un pays hors zone euro (Royaume-Uni, Pologne, USA...), les annonces reviennent en livres/zlotys/dollars — mais tous les seuils de référence du sniper sont calibrés en euros. Sans conversion, un "bon deal" en devise étrangère pourrait sembler complètement différent une fois comparé au seuil. Le sniper convertit maintenant **tout vers l'euro dès la réception de l'annonce**, pour que le classement (bon deal / affaire en or) reste cohérent peu importe le pays choisi.

⚠️ Les taux de change sont approximatifs et statiques (pas de connexion à une API de change en direct) — suffisant pour bien classer les annonces, mais modifie `currency.py` si tu veux affiner.

---

## 18. Workflow concret

### Lancer l'app
Double-clic sur `Lancer PC Sniper.command`, ou :
```bash
cd /Volumes/Data3/PC-Sniper
source venv/bin/activate
python3 app.py
```
App web sur **http://localhost:8000**, mise à jour en direct toutes les 4s, aucun son.

**Options :**
```bash
SNIPER_STEAL=1 python3 app.py        # alerte que sur affaires en or
SNIPER_NO_VINTED=1 python3 app.py    # Leboncoin seul
SNIPER_NO_LBC=1 python3 app.py       # Vinted seul
SNIPER_PORT=8001 python3 app.py      # change le port
```

### Réagir vite
1. Ouvre la fiche ou clique "Ouvrir l'annonce ↗"
2. Vérifie les photos
3. Message type (section 13)
4. Remise en main propre si proche, sinon paiement sécurisé

### Tester avant de revendre
- **GPU** : FurMark/jeu 20 min, températures, artefacts
- **CPU** : boot + Cinebench/Prime95 court
- **RAM** : MemTest86 un passage
- **PSU** : test sous charge

### Revendre
Photos propres, "testé, fonctionnel, garantie 7 jours", prix **fair** ou -5% pour vendre vite.

---

## 19. Messages types

**Premier contact :**
> Bonjour, votre [modèle] m'intéresse au prix affiché. Je peux venir le chercher rapidement / régler tout de suite. Toujours disponible ?

**Négociation légère :**
> Bonjour, intéressé par votre [modèle]. Seriez-vous d'accord pour [prix] en paiement immédiat et enlèvement rapide ?

**Vérification :**
> La carte a-t-elle été testée récemment ? Avez-vous une photo de l'écran en jeu / la facture d'origine ?

---

## 20. Rentabilité réaliste

GPU type RTX 3060/3070 : **40-80€** de marge. CPU AM4 : **20-40€**. Bundle CM+CPU+RAM : **60-120€**. En traitant 3-5 pièces/semaine, complément régulier. La clé : vitesse de réaction + discipline sur les prix (jamais au-dessus de `good`).

---

## 21. Signaux d'alerte à surveiller toi-même

Le sniper filtre déjà HS/pour pièces/artefacts/cartons/câbles et les patterns d'arnaque (WhatsApp, Telegram, PayPal famille, mandat cash). Reste vigilant en plus :
- Prix trop beau + vendeur récent sans historique = méfiance
- Refus de remise en main propre + insistance sur un mode de paiement précis = arnaque classique
- Photo unique tirée d'internet (recherche d'image inversée)

---

## 22. Installation (rappel des fichiers)

Tous ces fichiers dans le même dossier (`/Volumes/Data3/PC-Sniper`) :

| Fichier | Rôle |
|---|---|
| `app.py` | **L'app (à lancer)** — serveur Flask + scan en fond, i18n FR/EN, pays/plateformes en direct |
| `Lancer PC Sniper.command` / `.bat` | Lanceur double-clic (macOS / Windows) |
| `market_db.py` | Base de 255 modèles + prix planchers |
| `perf_db.py` | Perf gaming 2026 (GPU/CPU) + demande marché |
| `scoring.py` | Moteur perf/prix + revendabilité |
| `relevance.py` | Vérifie que le titre correspond vraiment au modèle |
| `listing_filter.py` | Filtre anti-carton/câble/HS + confiance (i18n) |
| `price_resolver.py` | Résolution en couches du prix (réel > PCPartPicker > estimation) |
| `pcpp_refresh.py` | Rafraîchit les prix neufs PCPartPicker |
| `price_history.py` | Agrégats quotidiens + déclenche l'archive brute permanente |
| `vinted_client.py` | Client Vinted (multi-pays, multi-devise, diagnostic intégré) |
| `ebay_client.py` | Client eBay (API officielle, multi-pays, multi-devise) |
| `facebook_client.py` | Client Facebook Marketplace (multi-pays, multi-devise, diagnostic intégré) |
| `currency.py` | Conversion de toutes les devises vers l'euro |
| `countries.py` | Configuration des 31 marchés (Vinted/eBay/Leboncoin/Facebook) |
| `settings_store.py` | Paramètres persistants (pays/langue/plateformes) |
| `mcp_server.py` | Serveur MCP pour Claude Code (optionnel) |
| `STRATEGIE_FLIP.md` / `README.md` / `README_EN.md` | Guides (FR détaillé, installation FR/EN) |

Fichiers générés automatiquement (tous sur Data3 si tu lances l'app depuis là) : `deals_found.json` (dashboard actif), `deals_archive.jsonl` (archive permanente), `seen_ads.json`, `price_history.json` (agrégats), `price_observations.jsonl` (log brut permanent), `live_prices.json` (après `pcpp_refresh.py`), `sniper_settings.json` (pays/langue/plateformes choisis dans le dashboard).

**Particularité Vinted :** pas de ville (juste "Vinted"), prix hors protection acheteur + port — pense-y pour ta marge réelle.
