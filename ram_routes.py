"""
ram_routes.py — Dashboard RAM SNIPER (blueprint Flask intégré à SOLDIER)
═══════════════════════════════════════════════════════════════════════════
S'enregistre sur l'app Flask existante sous /ram :

    import ram_routes
    ram_routes.enregistrer(app)

Vues :
  /ram                      page unique, rafraîchie en AJAX
  /ram/api/feed             live feed 24 h, filtrable
  /ram/api/radar            radar kits (unitaires + candidats d'appariement)
  /ram/api/stock            stock et workflow de test
  /ram/api/pnl             P&L, capital dormant, délai de rotation
  /ram/api/vision           quota, file d'attente, annonces en attente
  /ram/api/calibrage        fraîcheur des prix de référence
  /ram/api/journal          journal des décisions
  /ram/api/references       base de référence
  /ram/api/annonce/<id>     détail + analyse vision
  /ram/api/listing/<id>     annonces de revente générées

Le blueprint ne dépend d'aucun état en mémoire : toutes les vues lisent la
base. Le dashboard reste donc juste même si les workers tournent dans un
autre processus.
"""

import json
import os
import time

from flask import Blueprint, Response, jsonify, request

import ram_calibration
import ram_config
import ram_db
import ram_listing
import ram_pairing
import ram_scrapers
import ram_vision

bp = Blueprint("ram", __name__, url_prefix="/ram")


def _jlist(valeur):
    if isinstance(valeur, list):
        return valeur
    try:
        out = json.loads(valeur or "[]")
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


# ─────────────────────── API ───────────────────────
@bp.route("/api/feed")
def api_feed():
    cfg = ram_config.get()
    annonces = ram_db.feed(
        heures=int(request.args.get("heures", cfg.val("dashboard.feed_heures", 24))),
        min_score=float(request.args.get("min_score", 0) or 0),
        tier=request.args.get("tier") or None,
        statut_verif=request.args.get("verif") or None,
        statut=request.args.get("statut") or None,
        limit=int(request.args.get("limit", cfg.val("dashboard.feed_max", 200))))
    for a in annonces:
        a["photos"] = _jlist(a.get("photos"))
        a["drapeaux"] = _jlist(a.get("drapeaux"))
        a["vision_drapeaux"] = _jlist(a.get("vision_drapeaux"))
        a.pop("brut", None)          # payload d'origine : inutile côté client
    return jsonify(annonces)


@bp.route("/api/radar")
def api_radar():
    lignes = ram_pairing.radar_kits()
    return jsonify({"lignes": lignes, "total": len(lignes),
                    "avec_candidats": sum(1 for l in lignes if l["candidats"])})


@bp.route("/api/stock")
def api_stock():
    return jsonify(ram_db.list_stock(statut=request.args.get("statut") or None,
                                     non_apparie=request.args.get("non_apparie") == "1"))


@bp.route("/api/stock", methods=["POST"])
def api_stock_creer():
    data = request.get_json(force=True) or {}
    if not data.get("prix_achat"):
        return jsonify({"error": "prix_achat obligatoire"}), 400
    stock_id = ram_db.creer_stock(data)
    return jsonify({"id": stock_id, "stock": ram_db.list_stock()[0]}), 201


@bp.route("/api/stock/<int:stock_id>", methods=["PATCH"])
def api_stock_maj(stock_id):
    data = request.get_json(force=True) or {}
    autorisees = {
        "statut", "code_semaine", "numero_serie", "test_date", "test_banc",
        "memtest_passes", "memtest_ok", "memtest_screenshot", "xmp_stable",
        "frequence_max_stable", "test_notes", "prix_vente", "frais_vente",
        "plateforme_vente", "marge_nette", "vendu_le", "liste_le", "recu_le", "notes",
    }
    champs = {k: v for k, v in data.items() if k in autorisees}
    if not champs:
        return jsonify({"error": "aucun champ modifiable fourni"}), 400
    # La marge nette se déduit du prix de vente : la laisser saisir à la main
    # est le meilleur moyen d'avoir un P&L faux.
    if champs.get("prix_vente") and "marge_nette" not in champs:
        with ram_db.get_db() as conn:
            ligne = conn.execute("SELECT prix_revient FROM ram_stock WHERE id=?",
                                 (stock_id,)).fetchone()
        if ligne:
            champs["marge_nette"] = round(float(champs["prix_vente"])
                                          - float(ligne["prix_revient"] or 0)
                                          - float(champs.get("frais_vente") or 0), 2)
    ram_db.maj_stock(stock_id, champs)
    return jsonify({"ok": True, "champs": champs})


@bp.route("/api/kit", methods=["POST"])
def api_kit_assembler():
    data = request.get_json(force=True) or {}
    stock_ids = data.get("stock_ids") or []
    try:
        return jsonify(ram_pairing.assembler_kit(stock_ids, data.get("nom")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/pnl")
def api_pnl():
    cfg = ram_config.get()
    kpis = ram_db.kpis()
    kpis["seuil_dormant_pct"] = cfg.val("dashboard.alerte_capital_dormant_pct", 40)
    return jsonify(kpis)


@bp.route("/api/vision")
def api_vision():
    etat = ram_vision.etat_quota()
    with ram_db.get_db() as conn:
        attente = [dict(r) for r in conn.execute("""
            SELECT f.priorite, f.statut, f.tentatives, a.id, a.titre, a.prix_total,
                   a.pre_score, a.url
            FROM ram_vision_file f JOIN ram_annonce a ON a.id=f.annonce_id
            WHERE f.statut IN ('en_attente','differe','en_cours')
            ORDER BY f.priorite DESC LIMIT 50
        """).fetchall()]
    etat["en_attente"] = attente
    return jsonify(etat)


@bp.route("/api/calibrage")
def api_calibrage():
    alerte = ram_calibration.alerte_calibrage()
    alerte["perimees_detail"] = ram_db.references_perimees()[:100]
    return jsonify(alerte)


@bp.route("/api/calibrage/lancer", methods=["POST"])
def api_calibrage_lancer():
    simuler = (request.get_json(silent=True) or {}).get("simuler", True)
    return jsonify({"changements": ram_calibration.recalibrer(
        verbose=False, appliquer=not simuler), "simulation": simuler})


@bp.route("/api/journal")
def api_journal():
    return jsonify(ram_db.journal(int(request.args.get("limit", 200))))


@bp.route("/api/references")
def api_references():
    return jsonify(ram_db.list_references(
        tier=request.args.get("tier") or None,
        marque=request.args.get("marque") or None,
        limit=int(request.args.get("limit", 500))))


@bp.route("/api/pn_candidats")
def api_pn_candidats():
    return jsonify(ram_db.list_pn_candidats())


@bp.route("/api/annonce/<int:annonce_id>")
def api_annonce(annonce_id):
    annonce = ram_db.get_annonce(annonce_id)
    if not annonce:
        return jsonify({"error": "annonce introuvable"}), 404
    annonce["photos"] = _jlist(annonce.get("photos"))
    annonce["drapeaux"] = _jlist(annonce.get("drapeaux"))
    annonce.pop("brut", None)
    vision = ram_db.analyse_de_annonce(annonce_id)
    if vision:
        vision["drapeaux"] = _jlist(vision.get("drapeaux"))
        vision.pop("reponse_brute", None)
    return jsonify({"annonce": annonce, "vision": vision,
                    "notification": ram_db.notification_de_annonce(annonce_id)})


@bp.route("/api/annonce/<int:annonce_id>/decision", methods=["POST"])
def api_decision(annonce_id):
    data = request.get_json(force=True) or {}
    action = data.get("action")
    if action not in ("achat", "refus", "ignore", "archive", "message"):
        return jsonify({"error": "action invalide"}), 400
    annonce = ram_db.get_annonce(annonce_id)
    if not annonce:
        return jsonify({"error": "annonce introuvable"}), 404
    ram_db.journaliser(action, annonce, motif=data.get("motif"),
                       notes=data.get("notes"))
    statuts = {"achat": "achete", "ignore": "ignore", "archive": "archive",
               "refus": "ignore"}
    if action in statuts:
        ram_db.maj_annonce(annonce_id, {"statut": statuts[action]})
    return jsonify({"ok": True})


@bp.route("/api/listing/<int:stock_id>")
def api_listing(stock_id):
    try:
        return jsonify(ram_listing.generer(stock_id=stock_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


def _sante_workers():
    """État des workers, publié par ram_sniper.py dans un fichier.

    Le bot et le dashboard sont deux processus distincts : ce fichier est le
    seul lien. Sans lui, un worker mort restait totalement invisible.
    """
    chemin = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ram_sante.json")
    try:
        with open(chemin, encoding="utf-8") as f:
            sante = json.load(f)
    except (OSError, ValueError):
        return {"bot_en_marche": False, "motif": "aucun ram_sniper.py détecté"}

    age = time.time() - float(sante.get("maj_le") or 0)
    # Le battement est écrit toutes les 30 s : au-delà de 2 minutes, le bot
    # est arrêté ou bloqué.
    sante["bot_en_marche"] = age < 120
    sante["battement_age_s"] = round(age, 1)
    if not sante["bot_en_marche"]:
        sante["motif"] = f"dernier signe de vie il y a {int(age)} s"
    incidents = {n: w for n, w in (sante.get("workers") or {}).items()
                 if w.get("redemarrages")}
    sante["incidents"] = incidents
    return sante


@bp.route("/api/etat")
def api_etat():
    cfg = ram_config.get()
    return jsonify({
        "sante": _sante_workers(),
        "sources": ram_scrapers.etat_sources(cfg),
        "vision": ram_vision.etat_quota(cfg),
        "base": ram_db.stats_base(),
        "scan": ram_db.scan_stats(),
        "config": {"notif_mode": cfg.notif_mode, "dry_run": cfg.dry_run,
                   "fichier": cfg.chemin, "erreur": cfg.erreur},
        "secrets_manquants": ram_config.secrets_manquants(),
    })


@bp.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


def enregistrer(app):
    """Branche le module sur l'app SOLDIER existante."""
    ram_db.init_db()
    app.register_blueprint(bp)
    return bp


# ─────────────────────── FRONT ───────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAM SNIPER — DDR4</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2128; --bord:#30363d;
    --txt:#e6edf3; --txt2:#8b949e; --acc:#2f81f7;
    --ok:#3fb950; --warn:#d29922; --ko:#f85149; --or:#e3b341;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{position:sticky;top:0;z-index:20;background:var(--panel);
         border-bottom:1px solid var(--bord);padding:12px 20px;
         display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;letter-spacing:.5px}
  .pastille{font-size:11px;padding:3px 9px;border-radius:20px;
            background:var(--panel2);border:1px solid var(--bord);color:var(--txt2)}
  .pastille.ok{color:var(--ok);border-color:#1f6f34}
  .pastille.warn{color:var(--warn);border-color:#7a5c11}
  .pastille.ko{color:var(--ko);border-color:#8b2c28}
  nav{display:flex;gap:4px;padding:0 20px;background:var(--panel);
      border-bottom:1px solid var(--bord);overflow-x:auto}
  nav button{background:none;border:none;color:var(--txt2);padding:11px 16px;
             cursor:pointer;font-size:13px;border-bottom:2px solid transparent;
             white-space:nowrap}
  nav button:hover{color:var(--txt)}
  nav button.actif{color:var(--acc);border-bottom-color:var(--acc)}
  main{padding:20px;max-width:1500px;margin:0 auto}
  .onglet{display:none} .onglet.actif{display:block}
  .grille{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
  .carte{background:var(--panel);border:1px solid var(--bord);border-radius:10px;padding:14px}
  .carte h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;
            letter-spacing:.8px;color:var(--txt2);font-weight:600}
  .kpi{font-size:26px;font-weight:600;line-height:1.2}
  .kpi small{font-size:12px;color:var(--txt2);font-weight:400;display:block;margin-top:2px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--txt2);font-weight:600;font-size:11px;
     text-transform:uppercase;letter-spacing:.6px;padding:8px 10px;
     border-bottom:1px solid var(--bord);position:sticky;top:0;background:var(--panel)}
  td{padding:9px 10px;border-bottom:1px solid #21262d;vertical-align:top}
  tr:hover td{background:var(--panel2)}
  a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
  .badge{display:inline-block;font-size:10px;padding:2px 7px;border-radius:4px;
         background:var(--panel2);border:1px solid var(--bord);color:var(--txt2)}
  .tier-S{color:var(--or);border-color:#6b5410} .tier-A{color:var(--ok);border-color:#1f6f34}
  .tier-B{color:var(--acc);border-color:#1a4d8f} .tier-C{color:var(--txt2)}
  .tier-D{color:var(--ko);border-color:#8b2c28}
  .v-confirme{color:var(--ok)} .v-probable{color:var(--warn)}
  .v-a_verifier{color:var(--acc)} .v-rejete{color:var(--ko)}
  .v-non_verifie,.v-quota_epuise{color:var(--txt2)}
  .score{font-weight:600;font-size:15px}
  .marge{color:var(--ok);font-weight:600}
  .barre{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:6px}
  .barre span{display:block;height:100%;background:var(--acc)}
  .filtres{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
  select,input{background:var(--panel2);border:1px solid var(--bord);color:var(--txt);
               padding:6px 10px;border-radius:6px;font-size:13px}
  .vide{color:var(--txt2);text-align:center;padding:40px;font-style:italic}
  .alerte{background:#3d1d1d;border:1px solid #8b2c28;color:#ffb3ae;
          padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}
  .alerte.jaune{background:#3a2f10;border-color:#7a5c11;color:#e8d08a}
  .scroll{overflow:auto;max-height:70vh}
  .mini{font-size:11px;color:var(--txt2)}
</style>
</head>
<body>
<header>
  <h1>🎯 RAM SNIPER</h1>
  <span class="pastille" id="p-mode">…</span>
  <span class="pastille" id="p-vision">…</span>
  <span class="pastille" id="p-sources">…</span>
  <span class="pastille" id="p-refs">…</span>
  <span class="pastille" id="p-bot">…</span>
  <span class="pastille" id="p-maj" style="margin-left:auto">…</span>
</header>
<nav>
  <button class="actif" data-o="feed">Live feed</button>
  <button data-o="radar">⚡ Radar kits</button>
  <button data-o="stock">Stock</button>
  <button data-o="pnl">P&amp;L</button>
  <button data-o="vision">Quota vision</button>
  <button data-o="calibrage">Calibrage</button>
  <button data-o="journal">Journal</button>
  <button data-o="refs">Références</button>
</nav>
<main>

<section class="onglet actif" id="o-feed">
  <div class="filtres">
    <select id="f-verif">
      <option value="">Tous les statuts</option>
      <option value="confirme">✅ Confirmé</option>
      <option value="probable">🟡 Probable</option>
      <option value="a_verifier">🔍 À vérifier</option>
      <option value="non_verifie">⚡ Non vérifié</option>
      <option value="rejete">❌ Rejeté</option>
    </select>
    <select id="f-tier">
      <option value="">Tous les tiers</option>
      <option>S</option><option>A</option><option>B</option>
      <option>C</option><option>D</option>
    </select>
    <input type="number" id="f-score" placeholder="Score min" style="width:110px">
    <select id="f-heures">
      <option value="24">24 h</option><option value="72">3 jours</option>
      <option value="168">7 jours</option>
    </select>
  </div>
  <div class="carte scroll"><table id="t-feed"><tbody></tbody></table></div>
</section>

<section class="onglet" id="o-radar">
  <p class="mini">Barrettes unitaires en stock et annonces qui les compléteraient.
     L'appariement de kits est la plus grosse source de marge du système.</p>
  <div id="c-radar"></div>
</section>

<section class="onglet" id="o-stock">
  <div class="carte scroll"><table id="t-stock"><tbody></tbody></table></div>
</section>

<section class="onglet" id="o-pnl">
  <div id="a-dormant"></div>
  <div class="grille" id="c-pnl"></div>
</section>

<section class="onglet" id="o-vision">
  <div class="grille" id="c-vision"></div>
  <h3 style="color:var(--txt2);font-size:12px;text-transform:uppercase;margin-top:20px">
    File d'attente</h3>
  <div class="carte scroll"><table id="t-vision"><tbody></tbody></table></div>
</section>

<section class="onglet" id="o-calibrage">
  <div id="a-calibrage"></div>
  <div class="carte scroll"><table id="t-calibrage"><tbody></tbody></table></div>
</section>

<section class="onglet" id="o-journal">
  <div class="carte scroll"><table id="t-journal"><tbody></tbody></table></div>
</section>

<section class="onglet" id="o-refs">
  <div class="filtres">
    <select id="f-reftier">
      <option value="">Tous les tiers</option>
      <option>S</option><option>A</option><option>B</option>
      <option>C</option><option>D</option>
    </select>
  </div>
  <div class="carte scroll"><table id="t-refs"><tbody></tbody></table></div>
</section>

</main>
<script>
const $ = s => document.querySelector(s);
const eur = v => (v == null ? "—" : Math.round(v) + "€");
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ETAT = {confirme:"✅ Confirmé", probable:"🟡 Probable", a_verifier:"🔍 À vérifier",
              rejete:"❌ Rejeté", non_verifie:"⚡ Non vérifié",
              quota_epuise:"⏸ Quota épuisé"};
// Âge de PUBLICATION, pas de détection : sur Vinted une vraie affaire part en
// minutes, une annonce encore en ligne depuis 10 jours n'en est pas une.
function age(ts) {
  if (!ts) return "—";
  const s = Date.now()/1000 - ts;
  if (s < 90)    return '<span style="color:var(--ko)">🔥 à l\'instant</span>';
  if (s < 3600)  return '<span style="color:var(--ko)">🔥 ' + Math.floor(s/60) + " min</span>";
  if (s < 86400) return Math.floor(s/3600) + " h";
  const j = Math.floor(s/86400);
  return '<span style="color:var(--txt2)">' + j + " j</span>";
}

let ongletActif = "feed";
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("actif"));
  document.querySelectorAll(".onglet").forEach(x => x.classList.remove("actif"));
  b.classList.add("actif");
  $("#o-" + b.dataset.o).classList.add("actif");
  ongletActif = b.dataset.o;
  charger();
});
["f-verif","f-tier","f-score","f-heures","f-reftier"].forEach(
  id => $("#" + id).onchange = charger);

async function api(chemin) {
  const r = await fetch("/ram/api/" + chemin);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

async function entete() {
  try {
    const e = await api("etat");
    $("#p-mode").textContent = (e.config.dry_run ? "🔕 dry-run" : "🔔 " + e.config.notif_mode);
    $("#p-mode").className = "pastille " + (e.config.dry_run ? "warn" : "ok");
    const v = e.vision;
    $("#p-vision").textContent = `vision ${v.restant_jour}/${v.plafond_jour}`;
    $("#p-vision").className = "pastille " + (v.disponible ? "ok" : "warn");
    const actives = Object.entries(e.sources)
      .filter(([, s]) => s.actif && s.client).map(([n]) => n);
    $("#p-sources").textContent = actives.length ? actives.join(" + ") : "aucune source";
    $("#p-sources").className = "pastille " + (actives.length ? "ok" : "ko");
    $("#p-refs").textContent = e.base.references + " références";
    if (e.secrets_manquants.length) {
      $("#p-refs").textContent += " · ⚠️ " + e.secrets_manquants.join(", ");
      $("#p-refs").className = "pastille warn";
    }
    const s = e.sante || {};
    if (s.bot_en_marche) {
      const inc = Object.keys(s.incidents || {}).length;
      $("#p-bot").textContent = inc ? `bot actif · ${inc} incident(s)` : "bot actif";
      $("#p-bot").className = "pastille " + (inc ? "warn" : "ok");
    } else {
      $("#p-bot").textContent = "⏹ bot arrêté";
      $("#p-bot").className = "pastille ko";
      $("#p-bot").title = s.motif || "";
    }
    $("#p-maj").textContent = new Date().toLocaleTimeString("fr-FR");
  } catch (e) { $("#p-maj").textContent = "hors ligne"; }
}

async function feed() {
  const p = new URLSearchParams({
    verif: $("#f-verif").value, tier: $("#f-tier").value,
    min_score: $("#f-score").value || 0, heures: $("#f-heures").value});
  const a = await api("feed?" + p);
  const t = $("#t-feed");
  if (!a.length) { t.innerHTML = '<tr><td class="vide">Aucune annonce</td></tr>'; return; }
  t.innerHTML = "<thead><tr><th>Score</th><th>Annonce</th><th>Prix total</th>" +
    "<th>Revente</th><th>Marge</th><th>Statut</th><th>Vu</th></tr></thead><tbody>" +
    a.map(x => {
      const score = x.score_final != null ? x.score_final : (x.pre_score || 0);
      const marge = x.marge_reelle != null ? x.marge_reelle : x.marge_estimee;
      const pct = x.marge_reelle_pct != null ? x.marge_reelle_pct : x.marge_pct;
      return `<tr>
        <td><span class="score">${score.toFixed(0)}</span>
            <div class="barre"><span style="width:${Math.min(score,100)}%"></span></div></td>
        <td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.titre)}</a>
            <div class="mini">
              <span class="badge tier-${x.tier||'C'}">${x.tier||'?'}</span>
              ${x.pn_detecte ? esc(x.pn_detecte) : "PN inconnu"}
              ${x.nb_modules&&x.capacite_module_go ? ` · ${x.nb_modules}×${x.capacite_module_go}Go` : ""}
              ${x.frequence_mhz ? ` · ${x.frequence_mhz}` : ""}
              ${x.cas_latency ? ` CL${x.cas_latency}` : ""} · ${esc(x.source)}
            </div>
            ${(x.rejet_motif ? `<div class="mini v-rejete">${esc(x.rejet_motif)}</div>` : "")}</td>
        <td>${eur(x.prix_total)}<div class="mini">${eur(x.prix_affiche)} affiché</div></td>
        <td>${eur(x.revente_estimee)}</td>
        <td class="marge">${marge!=null?eur(marge):"—"}
            <div class="mini">${pct!=null?pct.toFixed(0)+"%":""}</div></td>
        <td class="v-${x.statut_verif}">${ETAT[x.statut_verif]||x.statut_verif}</td>
        <td class="mini">${age(x.publie_le)}
            <div class="mini">vu ${new Date(x.detecte_le*1000).toLocaleTimeString("fr-FR",
              {hour:"2-digit",minute:"2-digit"})}</div></td></tr>`;
    }).join("") + "</tbody>";
}

async function radar() {
  const d = await api("radar");
  const c = $("#c-radar");
  if (!d.lignes.length) {
    c.innerHTML = '<div class="carte vide">Aucune barrette unitaire en stock. ' +
      'Le radar s\'activera dès le premier achat non apparié.</div>';
    return;
  }
  c.innerHTML = d.lignes.map(l => `
    <div class="carte" style="margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
        <div><b>${esc(l.stock.part_number || "PN inconnu")}</b>
          <span class="badge tier-${l.stock.ref_tier||'C'}">${l.stock.ref_tier||'?'}</span>
          <div class="mini">${l.stock.capacite_module_go}Go ·
            ${l.stock.frequence_mhz||"?"}MHz ·
            revient ${eur(l.stock.prix_revient)} ·
            ${l.stock.code_semaine ? "batch " + esc(l.stock.code_semaine) : "batch inconnu"} ·
            ${esc(l.stock.statut)}</div></div>
        <div style="text-align:right">
          <div class="kpi" style="font-size:19px">${eur(l.prix_cible)}
            <small>prix cible pour compléter</small></div>
          <div class="mini">kit revendable ${eur(l.revente_kit)}</div></div>
      </div>
      ${l.candidats.length ? `<table style="margin-top:12px">
        <thead><tr><th>Candidat</th><th>Prix</th><th>Type</th><th>Marge kit</th></tr></thead>
        <tbody>${l.candidats.map(k => `<tr>
          <td><a href="${esc(k.url)}" target="_blank" rel="noopener">${esc(k.titre)}</a></td>
          <td>${eur(k.prix_total)}</td>
          <td><span class="badge ${k.type_appariement==='parfait'?'tier-S':''}">
              ${k.type_appariement==='parfait'?'🎯 parfait':esc(k.type_appariement)}</span></td>
          <td class="marge">${eur(k.marge_kit_estimee)}</td></tr>`).join("")}
        </tbody></table>`
      : '<div class="mini" style="margin-top:10px">Aucun candidat détecté pour le moment.</div>'}
    </div>`).join("");
}

async function stock() {
  const s = await api("stock");
  const t = $("#t-stock");
  if (!s.length) { t.innerHTML = '<tr><td class="vide">Stock vide</td></tr>'; return; }
  t.innerHTML = "<thead><tr><th>Part number</th><th>Config</th><th>Statut</th>" +
    "<th>Revient</th><th>MemTest</th><th>Vente</th><th>Rotation</th></tr></thead><tbody>" +
    s.map(x => `<tr>
      <td><b>${esc(x.part_number||"—")}</b>
        <div class="mini">${esc(x.marque||"")} ${esc(x.ref_gamme||"")}</div></td>
      <td>${x.capacite_module_go}Go ${x.frequence_mhz||""}${x.cas_latency?" CL"+x.cas_latency:""}
        <div class="mini">${x.kit_id?"kit #"+x.kit_id:"unitaire"}</div></td>
      <td><span class="badge">${esc(x.statut)}</span></td>
      <td>${eur(x.prix_revient)}</td>
      <td>${x.memtest_ok?`✅ ${x.memtest_passes||"?"} passes`:(x.memtest_ok===0?"❌ HS":"—")}
        <div class="mini">${x.xmp_stable?"XMP stable":""}</div></td>
      <td>${x.prix_vente?eur(x.prix_vente):"—"}
        <div class="mini marge">${x.marge_nette!=null?"+"+eur(x.marge_nette):""}</div></td>
      <td>${x.delai_rotation_jours!=null?x.delai_rotation_jours+" j":"—"}</td></tr>`
    ).join("") + "</tbody>";
}

async function pnl() {
  const k = await api("pnl");
  $("#a-dormant").innerHTML = k.alerte_dormant
    ? `<div class="alerte">⚠️ Capital dormant : ${k.part_dormant_pct}% du capital engagé
       (${eur(k.capital_dormant)} sur ${k.capital_engage}€, seuil ${k.seuil_dormant_pct}%).
       ${k.articles_dormants} article(s) en stock depuis plus de 30 jours —
       baisser les prix plutôt que d'attendre.</div>` : "";
  $("#c-pnl").innerHTML = [
    ["Capital engagé", eur(k.capital_engage), k.articles_en_stock + " articles en stock"],
    ["Capital dormant", eur(k.capital_dormant), k.part_dormant_pct + "% · > 30 jours"],
    ["Marge réalisée", eur(k.marge_realisee), k.ventes + " ventes · CA " + eur(k.ca_realise)],
    ["Rotation moyenne", k.rotation_moyenne_jours + " j", "le KPI décisif"],
    ["Annonces 24 h", k.annonces_24h, k.notifications_24h + " notifications"],
    ["Références périmées", k.references_perimees, "à recalibrer"],
  ].map(([t, v, s]) => `<div class="carte"><h3>${t}</h3>
      <div class="kpi">${v}<small>${s}</small></div></div>`).join("");
}

async function vision() {
  const v = await api("vision");
  $("#c-vision").innerHTML = [
    ["Aujourd'hui", `${v.conso_jour}/${v.plafond_jour}`, v.restant_jour + " restantes"],
    ["Cette minute", `${v.conso_minute}/${v.plafond_minute}`,
     v.disponible ? "disponible" : (v.motif || "bloqué")],
    ["File d'attente", v.file_en_attente, v.file_en_cours + " en cours"],
    ["Différées (quota)", v.file_differees, "reprises automatiquement"],
    ["Analysées", v.file_faites, v.file_echecs + " échecs"],
    ["Modèle", v.modele, v.provider],
  ].map(([t, val, s]) => `<div class="carte"><h3>${t}</h3>
      <div class="kpi">${val}<small>${s}</small></div></div>`).join("");
  const t = $("#t-vision");
  t.innerHTML = v.en_attente.length
    ? "<thead><tr><th>Priorité</th><th>Annonce</th><th>Prix</th><th>Statut</th></tr></thead><tbody>" +
      v.en_attente.map(x => `<tr><td class="score">${(x.priorite||0).toFixed(0)}</td>
        <td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.titre)}</a></td>
        <td>${eur(x.prix_total)}</td>
        <td><span class="badge">${esc(x.statut)}</span></td></tr>`).join("") + "</tbody>"
    : '<tr><td class="vide">File vide</td></tr>';
}

async function calibrage() {
  const c = await api("calibrage");
  $("#a-calibrage").innerHTML = c.alerte
    ? `<div class="alerte jaune">⚠️ ${c.perimees} référence(s) sur ${c.total}
       (${c.part_pct}%) n'ont pas été recalibrées depuis plus de ${c.seuil_jours} jours.
       En pénurie DRAM les prix montent vite : un prix périmé fait rater des affaires
       correctes.</div>`
    : `<div class="carte" style="margin-bottom:14px">✅ Toutes les références ont été
       recalibrées il y a moins de ${c.seuil_jours} jours.</div>`;
  const t = $("#t-calibrage");
  t.innerHTML = (c.perimees_detail||[]).length
    ? "<thead><tr><th>Part number</th><th>Marque</th><th>Prix actuel</th>" +
      "<th>Dernier calibrage</th><th>Ancienneté</th><th>Source</th></tr></thead><tbody>" +
      c.perimees_detail.map(r => `<tr><td><b>${esc(r.part_number)}</b></td>
        <td>${esc(r.marque)} <span class="badge tier-${r.tier}">${r.tier}</span></td>
        <td>${eur(r.prix_ref_occasion_eur)}</td><td>${esc(r.prix_ref_maj_le||"jamais")}</td>
        <td>${r.jours_depuis_calibrage} j</td>
        <td class="mini">${esc(r.prix_ref_source)} (${r.prix_ref_n_ventes} ventes)</td></tr>`
      ).join("") + "</tbody>"
    : '<tr><td class="vide">Rien à recalibrer</td></tr>';
}

async function journal() {
  const j = await api("journal");
  const t = $("#t-journal");
  t.innerHTML = j.length
    ? "<thead><tr><th>Date</th><th>Action</th><th>Annonce</th><th>Score</th>" +
      "<th>Marge attendue</th><th>Motif</th></tr></thead><tbody>" +
      j.map(x => `<tr>
        <td class="mini">${new Date(x.decide_le*1000).toLocaleString("fr-FR")}</td>
        <td><span class="badge">${esc(x.action)}</span>
            <div class="mini">${esc(x.decide_par)}</div></td>
        <td>${x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.titre||"—")}</a>`
             :esc(x.titre||"—")}</td>
        <td>${x.score_final!=null?x.score_final.toFixed(0):(x.pre_score!=null?x.pre_score.toFixed(0)+" (pré)":"—")}</td>
        <td>${eur(x.marge_attendue)}</td>
        <td class="mini">${esc(x.motif||"")}</td></tr>`).join("") + "</tbody>"
    : '<tr><td class="vide">Aucune décision enregistrée</td></tr>';
}

async function refs() {
  const r = await api("references?tier=" + $("#f-reftier").value + "&limit=600");
  $("#t-refs").innerHTML = "<thead><tr><th>Part number</th><th>Marque / gamme</th>" +
    "<th>Config</th><th>Puces</th><th>Tier</th><th>Prix réf.</th><th>Liquidité</th>" +
    "<th>Rotation</th><th>Calibré</th></tr></thead><tbody>" +
    r.map(x => `<tr>
      <td><b>${esc(x.part_number)}</b>${x.pn_verifie?"":' <span class="badge warn">à confirmer</span>'}</td>
      <td>${esc(x.marque)}<div class="mini">${esc(x.gamme)}</div></td>
      <td>${x.nb_modules}×${x.capacite_module_go}Go
        <div class="mini">${x.frequence_mhz} CL${x.cas_latency||"?"}
        ${x.rgb?" · RGB":""}${x.low_profile?" · LP":""}</div></td>
      <td class="mini">${esc(x.die_type||"—")}</td>
      <td><span class="badge tier-${x.tier}">${x.tier}</span></td>
      <td>${eur(x.prix_ref_occasion_eur)}</td>
      <td>${"★".repeat(x.liquidite)}</td>
      <td>${x.delai_rotation_jours||"—"} j</td>
      <td class="mini">${esc(x.prix_ref_maj_le||"—")}</td></tr>`).join("") + "</tbody>";
}

const VUES = {feed, radar, stock, pnl, vision, calibrage, journal, refs};
async function charger() {
  entete();
  try { await VUES[ongletActif](); }
  catch (e) { console.error(e); }
}
charger();
setInterval(charger, 15000);
</script>
</body>
</html>
"""
