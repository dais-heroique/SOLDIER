# 🎯 RAM SNIPER — module DDR4 UDIMM desktop pour SOLDIER

Détection d'annonces DDR4 sous-évaluées sur Vinted et Leboncoin, notification
Telegram instantanée, puis enrichissement de cette même notification par une
analyse d'image Gemini qui confirme ou infirme l'affaire.

**Pas d'achat automatisé.** Vinted n'a pas d'API d'achat publique et
l'automatiser fait bannir le compte. Le bot détecte et prépare ; la validation
se fait à la main depuis Telegram, en quelques secondes.

---

## Sommaire

1. [Installation](#1-installation)
2. [Obtenir les clés](#2-obtenir-les-clés)
3. [Premier démarrage](#3-premier-démarrage)
4. [Calibrage initial des prix](#4-calibrage-initial-des-prix)
5. [Utilisation au quotidien](#5-utilisation-au-quotidien)
6. [Configuration (`ram_config.yaml`)](#6-configuration)
7. [Architecture](#7-architecture)
8. [Base de données](#8-base-de-données)
9. [Dépannage](#9-dépannage)

---

## 1. Installation

Le module vit dans le dossier SOLDIER existant et partage sa base
(`soldier.db`). Aucune installation séparée.

```bash
cd /Volumes/Data3/SOLDIER

# Dépendances (en plus de celles de SOLDIER)
venv/bin/pip install pyyaml pillow

# Création des tables + chargement des 113 références
venv/bin/python3 ram_db.py seed
```

`pillow` est **optionnel** : sans lui, les photos sont envoyées à Gemini sans
redimensionnement (ça marche, mais ça consomme davantage de quota d'entrée).

Vérification :

```bash
venv/bin/python3 test_ram_sniper.py     # 59 tests, aucun accès réseau
venv/bin/python3 ram_db.py stats
```

---

## 2. Obtenir les clés

Les secrets vont dans `.env` à la racine du projet — **jamais** dans
`ram_config.yaml`, qui est versionné. `.env` est déjà dans `.gitignore`.

```bash
cat >> .env <<'EOF'
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
GEMINI_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
EOF
```

### Token Telegram

1. Ouvrir Telegram, chercher **@BotFather**.
2. `/newbot`, choisir un nom puis un identifiant finissant par `bot`.
3. BotFather renvoie le token → `TELEGRAM_BOT_TOKEN`.

### Chat ID

1. Envoyer n'importe quel message à votre nouveau bot.
2. Ouvrir dans un navigateur :
   `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`
3. Relever `"chat":{"id":987654321` → `TELEGRAM_CHAT_ID`.

Pour recevoir les alertes à deux, créez un groupe, ajoutez-y le bot, et
utilisez l'id du groupe (négatif, du type `-1001234567890`).

### Clé Gemini

1. <https://aistudio.google.com/app/apikey> → **Create API key**.
2. Copier la clé → `GEMINI_API_KEY`.

Le palier gratuit suffit largement : le module n'analyse que les annonces au
pré-score ≥ 55, soit quelques dizaines par jour.

> ⚠️ **Les quotas gratuits Google évoluent.** Ils sont dans
> `ram_config.yaml → vision.quota`, jamais en dur dans le code. Si vous voyez
> des HTTP 429 dans les logs, baissez `par_minute`. Si la file d'attente ne se
> vide jamais alors que le quota le permet, montez `par_jour`.

Vérification :

```bash
venv/bin/python3 ram_config.py
# secrets manquants : aucun ✅
```

---

## 3. Premier démarrage

**Toujours commencer en `--dry-run`.** Aucune notification n'est envoyée, mais
tout le reste tourne : vous voyez dans le terminal exactement ce qui serait
parti sur Telegram.

```bash
venv/bin/python3 ram_sniper.py --dry-run
```

Laissez tourner une heure, regardez les messages produits. Si le bot vous
propose des affaires qui n'en sont pas, ajustez les seuils (§6) avant de
passer en réel.

Une fois satisfait :

```bash
venv/bin/python3 ram_sniper.py
```

Le dashboard est monté sur l'app SOLDIER existante :

```bash
venv/bin/python3 app.py      # puis http://localhost:8000/ram
```

### Commandes utiles

| Commande | Effet |
|---|---|
| `ram_sniper.py` | tout : scraping + notifications + vision + calibrage |
| `ram_sniper.py --dry-run` | idem, sans envoyer une seule notification |
| `ram_sniper.py --once` | un seul tour de scan puis sortie |
| `ram_sniper.py --replay` | rejoue les annonces archivées à travers le scoring courant |
| `ram_sniper.py --calibrer` | lance le job de calibrage immédiatement |
| `ram_sniper.py --etat` | état complet du système, en JSON |
| `ram_db.py show S` | affiche les références d'un tier |
| `ram_db.py calibrage` | liste les références à recalibrer |

---

## 4. Calibrage initial des prix

**C'est l'étape qui fait la différence entre un bot utile et un bot qui crie
au loup.** Les 113 prix livrés sont un point de départ raisonnable (marché FR
mi-2026, contexte de pénurie DRAM), pas une vérité.

### Jour 1 — collecte

Lancez le module en `--dry-run` et laissez-le tourner **24 à 48 h**. Il
enregistre toutes les annonces vues, même celles qu'il rejette. C'est votre
matière première.

```bash
venv/bin/python3 ram_sniper.py --dry-run
```

### Jour 2 — première lecture

```bash
venv/bin/python3 ram_sniper.py --replay
```

Vous obtenez la répartition des scores et des motifs de rejet. Ce qu'il faut
regarder :

- **Beaucoup de rejets `marge`** → vos prix de référence sont trop bas. Le bot
  croit que rien n'est rentable parce qu'il sous-estime la revente.
- **Beaucoup de notifiables** → prix de référence trop hauts, vous allez
  recevoir du bruit.
- **Beaucoup de `non_identifie`** → des part numbers manquent à la base ; voyez
  l'onglet « références » du dashboard et la table `ram_pn_candidat`.

### Jour 3 — ajustement manuel

Pour les 10 à 15 références que vous voyez passer le plus souvent, cherchez
vous-même sur Vinted (filtre « vendus ») et sur eBay (ventes terminées, FR et
DE) le prix réel, et corrigez :

```bash
venv/bin/python3 - <<'EOF'
import ram_db
ref = ram_db.find_reference_by_pn("CMK32GX4M2E3200C16")
ram_db.maj_prix_reference(ref["id"], 118.0, "manuel", n_ventes=9)
EOF
```

L'Allemagne compte : c'est le marché directeur de la DDR4 d'occasion en Europe
et il précède le marché français de quelques semaines.

### Ensuite — automatique

Le job quotidien (04:30 par défaut) collecte les ventes conclues et remplace
les prix par la médiane observée, dès 3 ventes.

> ⚠️ **Recalibrage hebdomadaire obligatoire.** En pénurie DRAM les prix montent
> vite et dans un seul sens. Un prix de référence vieux d'un mois fait rater
> des affaires correctes : on croit payer trop cher ce qui est devenu le prix
> du marché. Toute référence non recalibrée depuis 14 jours est signalée sur
> l'onglet « Calibrage » du dashboard.

Un garde-fou plafonne à ±40 % la variation d'un seul recalibrage : un lot de
10 barrettes bradées n'écrase pas une référence établie.

---

## 5. Utilisation au quotidien

### Le flux en deux temps

**Étape 1 — moins de 10 secondes après publication**, texte seul :

```
⚡ NON VÉRIFIÉ · pré-score 72

« Ram ddr4 32go corsair »
Prix 45€ + 4€ port + 2.95€ = 51.95€

Estimation: 2×16 · 3200 · UDIMM
Revente estimée ~110€ → marge ~58€

⏳ Analyse image en cours...

[ 🛒 VOIR ]  [ 💬 Message ]  [ ❌ Ignorer ]
```

**Étape 2 — 2 à 15 secondes plus tard**, le même message est **modifié en
place** :

```
✅ CONFIRMÉ · score 87

Corsair Vengeance LPX
CMK32GX4M2E3200C16 · 2×16 · 3200 · CL16 · Tier A
noir · low profile · non-RGB

Prix 45€ + 4€ port + 2.95€ = 51.95€
Revente 115€ → marge nette 63€ (+121%)

📷 Sticker lisible ✓ · Contacts propres ✓ · 8 puces (non-ECC) ✓
Vendeur ⭐4.9 (127 ventes)

[ 🛒 ACHETER ]  [ 💬 Message ]  [ ❌ Ignorer ]
```

Passez `telegram.notif_mode: second_message` si vous préférez un deuxième
message séparé plutôt qu'une édition.

### Les quatre verdicts

| État | Signification | Action |
|---|---|---|
| ✅ **CONFIRMÉ** | identification cohérente, score ≥ 75 | acheter si le prix tient |
| 🟡 **PROBABLE** | cohérent, photo moyenne (confiance 0,5–0,75) | regarder les photos soi-même |
| 🔍 **À VÉRIFIER** | photo illisible | bouton « Demander photo sticker » |
| ❌ **REJETÉ** | DDR3, SO-DIMM, ECC, faux sticker, dégâts | passer |

### ⚡ COMPLÉTER KIT

La notification la plus rentable du système, et la seule jamais soumise à
l'anti-spam. Elle se déclenche quand une annonce porte le **même part number**
qu'une barrette unitaire déjà en stock.

Deux barrettes 16 Go identiques achetées 35 € pièce se revendent en kit assorti
32 Go autour de 120 €. Personne ne fait ça sérieusement sur Vinted.

Hiérarchie, jamais contournée :

| Situation | Vendable comme |
|---|---|
| PN identique **+ même code de semaine** | « kit assorti », XMP stable garanti |
| PN identique, batch différent | vendable, mentionner « testé ensemble à XMP » |
| Mêmes specs, PN différent | **jamais** un kit |

### Workflow de stock

```
commandé → reçu → en test → testé_OK / testé_HS → apparié → listé → vendu
```

Une barrette testée avec **preuve MemTest** se vend plus cher et surtout plus
vite : le générateur d'annonce l'indique automatiquement et propose la capture
d'écran. Renseignez `memtest_passes` et `xmp_stable` dans l'onglet Stock.

### Générateur d'annonce

```bash
curl localhost:8000/ram/api/listing/12
```

Produit deux versions : **Vinted** (prix ~8 % plus haut, l'acheteur paie les
frais) et **Leboncoin** (prix net, retrait mis en avant). Le titre est
construit avec les mots que les acheteurs *tapent* — capacité, fréquence, CL,
marque, « DDR4 », « PC Gamer » — et le part number va en description, où il
rassure sans nuire au référencement.

En 3600 CL16-18, l'argument « FCLK 1800 en 1:1 sur Ryzen 5000 » est ajouté
automatiquement : c'est lui qui déclenche l'achat.

---

## 6. Configuration

Tout se règle dans **`ram_config.yaml`**, rechargé à chaud toutes les 30
secondes. Aucun redémarrage nécessaire.

### Les réglages qui comptent

```yaml
scoring:
  seuil_notification: 65   # trop de bruit ? monter à 70-75
  seuil_vision: 55         # quota vision qui déborde ? monter
  seuil_confirme: 75
  marge_min_eur: 20        # rejet si marge < 20 € ET < 45 %
  marge_min_pct: 45
```

La règle de marge est un **ET**, pas un OU : 50 € de marge à 20 % reste
intéressant (kit 64 Go), et 15 € à 60 % aussi (volume rapide). Seul ce qui
échoue aux deux critères est rejeté.

```yaml
telegram:
  notif_mode: edit         # ou second_message
  anti_spam_s: 60          # max 1 nouvelle notification / minute

vision:
  quota:
    par_minute: 10         # à ajuster selon le quota gratuit du moment
    par_jour: 200
    marge_securite: 0.9    # on s'arrête à 90 % du plafond

sources:
  leboncoin:
    departements: ["74", "73", "01", "69"]   # zone de retrait main propre
```

Les mots-clés de recherche, les listes d'exclusion (SO-DIMM, ECC, DDR3) et les
multiplicateurs de prix sont également dans ce fichier.

### Rythme de scraping

Les délais par défaut visent **6 mois sans blacklist**, pas la vitesse :

| Source | Délai par mot-clé | Entre requêtes | Backoff 429 |
|---|---|---|---|
| Vinted | 30–45 s | 2,5–5 s | 60 → 600 s |
| Leboncoin | 60–90 s | 5–9 s | 120 → 1800 s |

Les mots-clés sont mélangés à chaque cycle (toujours commencer par « ddr4 »
crée un motif reconnaissable côté anti-bot), et un mot-clé qui prend un 429 est
mis en quarantaine **seul** — le reste du scan continue.

---

## 7. Architecture

```
ram_schema.sql          schéma SQLite complet (14 tables + 3 vues)
ram_reference_data.py   113 références réelles, part numbers exacts
ram_db.py               couche base : init, migrations, CRUD, quota, KPI
ram_config.py           chargement YAML (à chaud) + secrets .env
ram_parser.py           identification texte, exclusions, pièges DDR3
ram_scoring.py          pré-score textuel + score final après vision
ram_vision.py           VisionProvider / GeminiProvider, quota, cache, file
ram_telegram.py         notification 2 temps, édition en place, boutons
ram_pairing.py          arbitrage d'appariement de kits
ram_listing.py          générateur d'annonces Vinted + Leboncoin
ram_calibration.py      collecte des ventes + recalibrage
ram_scrapers.py         Vinted + Leboncoin, backoff, quarantaine, replay
ram_sniper.py           orchestrateur : 4 workers découplés
ram_routes.py           dashboard Flask (blueprint /ram)
test_ram_sniper.py      59 tests, aucun accès réseau
```

### Les quatre workers

| Worker | Rôle |
|---|---|
| **scraping** | parcourt les mots-clés, écrit les annonces, calcule le pré-score |
| **notification** | envoie l'étape 1, au plus 1/minute, meilleur score d'abord |
| **vision** | dépile la file par score, appelle Gemini, édite le message |
| **planificateur** | job de calibrage quotidien + purge |

Le découplage est délibéré : si Gemini est lent ou le quota épuisé, les
notifications instantanées continuent de partir. C'est tout l'intérêt du flux
en deux temps.

### Périmètre verrouillé au niveau du schéma

`ram_reference` porte des contraintes `CHECK` sur `generation = 4`,
`form_factor = 'UDIMM'` et `ecc = 0`. Insérer une SO-DIMM, une DDR3 ou une
barrette ECC dans la base de référence est **physiquement impossible** — une
régression de code ne peut pas polluer le socle de scoring.

### Note sur la détection ECC

La règle courante « un `E` ou un `W` chez Crucial/Kingston = ECC » est vraie,
mais **uniquement à une position précise** du part number :

| Part number | Verdict | Pourquoi |
|---|---|---|
| `KVR32E22D8/16` | ECC | `E` après le code de fréquence |
| `KVR32N22D8/16` | non-ECC | `N` = unbuffered non-ECC |
| `CT16G4WFD8266` | ECC | `W` avant `FD` |
| `CT16G4DFD832A` | non-ECC | `D` |
| `CMK32GX4M2E3200C16` | **non-ECC** | le `E` est une révision Corsair |

Cherchée en simple sous-chaîne, cette règle rejetterait
`CMK32GX4M2E3200C16` — le Corsair Vengeance LPX 2×16, référence la plus liquide
du marché. Le module utilise donc des motifs **positionnels** par constructeur
(`ram_parser.ECC_MOTIFS`), et les PN Corsair/G.Skill n'y sont jamais soumis.

Même chose côté Samsung : le motif `A2K43` ne dit rien de l'ECC —
`M378A2K43CB1-CTD` est du non-ECC parfaitement ordinaire. C'est le **préfixe**
qui tranche : `M378` = UDIMM non-ECC, `M391` = UDIMM ECC, `M393` = RDIMM,
`M471` = SO-DIMM.

---

## 8. Base de données

Mêmes fichier et conventions que SOLDIER (`soldier.db`), tables préfixées
`ram_`. Aucune collision avec les tables existantes.

| Table | Rôle |
|---|---|
| `ram_reference` | **le socle** — 113 références, prix, liquidité, rotation |
| `ram_annonce` | toute annonce vue, même rejetée (matière du replay) |
| `ram_vision_analyse` | cache des analyses, clé = url + hash des photos |
| `ram_vision_file` | file de priorité du worker vision |
| `ram_vision_quota` | compteurs minute + jour, persistés |
| `ram_stock` | inventaire, **une ligne = une barrette** |
| `ram_kit` | kits assemblés, avec qualité d'appariement |
| `ram_appariement` | candidats du radar kits |
| `ram_notification` | messages Telegram (`message_id` pour l'édition) |
| `ram_prix_observation` | ventes conclues, matière du calibrage |
| `ram_journal_decision` | chaque achat/refus, pour affiner le scoring |
| `ram_pn_candidat` | part numbers vus mais inconnus, à qualifier |
| `ram_scan_stat` | télémétrie de scraping (429, 403, résultats) |

Une ligne de stock = une barrette : c'est ce qui rend l'appariement possible.
Un kit acheté d'un bloc crée deux lignes reliées au même `ram_kit`.

### Sauvegarde

`soldier.db` est déjà sauvegardé par le `backup_worker` de SOLDIER. Les tables
`ram_` en bénéficient automatiquement.

---

## 9. Dépannage

**« Module RAM SNIPER indisponible » au démarrage d'`app.py`**
`pip install pyyaml`. Le module s'importe de façon défensive : SOLDIER démarre
quand même, seul l'onglet `/ram` est désactivé.

**Aucune annonce Vinted ne remonte**
Diagnostic intégré au client existant :
```bash
venv/bin/python3 vinted_client.py FR
```
Si le cookie échoue : `pip install -U curl_cffi`.

**Leboncoin indisponible**
`pip install lbc`. Le module fonctionne parfaitement sans — Vinted reste la
priorité 1.

**Trop de notifications**
Montez `scoring.seuil_notification` à 70-75, ou `scoring.marge_min_eur`.
Vérifiez d'abord avec `--replay` combien d'annonces passeraient au nouveau
seuil.

**Le quota vision est toujours épuisé**
Normal si `seuil_vision` est bas. Montez-le à 60-65 : seules les meilleures
annonces seront analysées. Les annonces non analysées ne sont jamais perdues,
elles repassent en file au renouvellement du quota — visible sur l'onglet
« Quota vision ».

**Un message reste bloqué sur « Analyse image en cours… »**
Le worker vision n'a pas démarré (clé Gemini absente) ou le quota est épuisé.
Dans le second cas le message est automatiquement modifié pour l'indiquer.
`ram_sniper.py --etat` donne l'état exact.

**Les prix semblent tous faux**
Le calibrage n'a pas encore tourné. Voir §4 — c'est l'étape qui compte.

**Une référence manque**
Ajoutez-la dans `ram_reference_data.py` puis `ram_db.py seed`. Le seed est
idempotent et **ne réécrit jamais un prix issu d'un vrai calibrage** : seules
les caractéristiques techniques sont resynchronisées.

---

## Rappels de méthode

- **Rotation > stock dormant.** Le délai moyen de rotation est le KPI décisif,
  pas la marge unitaire. Le dashboard alerte au-delà de 40 % de capital dormant.
- **Le prix affiché ne veut rien dire.** Ce qui compte est le prix total
  d'acquisition : affiché + port + protection acheteur.
- **Toujours identifier au part number**, jamais au nom marketing. « Vengeance »
  et « Ripjaws » existent aussi en DDR3.
- **CL14 en 3200 = B-die quasi certain** — mais confirmez au part number.
- **Ne jamais vendre comme kit assorti deux barrettes de PN différents.**
  C'est exactement ce qu'on reproche aux autres vendeurs.
