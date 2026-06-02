import os
import time
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from groq import Groq
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY = os.environ.get("TENNIS_API_KEY")
MONGODB_URL = os.environ.get("MONGODB_URL")
CAPITAL_INITIAL = 50.0
SAISON_FOOT = os.environ.get("SAISON_FOOT", "2025")

# Chat ou seront envoyes les pronostics automatiques (ton chat prive).
# Peut etre surcharge par une variable d'environnement Render.
MON_CHAT_ID = os.environ.get("MON_CHAT_ID", "8233541336")
# Combien de minutes avant le coup d'envoi on envoie l'analyse.
MINUTES_AVANT = int(os.environ.get("MINUTES_AVANT", "30"))
# Marge : on envoie si le match est entre MINUTES_AVANT et MINUTES_AVANT+10 min.
FENETRE_MINUTES = 10

# --- Validation des variables d'environnement au demarrage -----------------
REQUIS = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "GROQ_API_KEY": GROQ_API_KEY,
    "MONGODB_URL": MONGODB_URL,
}
manquants = [nom for nom, valeur in REQUIS.items() if not valeur]
if manquants:
    raise RuntimeError(f"Variables d'environnement manquantes : {', '.join(manquants)}")

# ---------------------------------------------------------------------------
# Clients externes
# ---------------------------------------------------------------------------
client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000)
db = mongo_client["pronostics"]
collection_paris = db["paris"]
collection_bankroll = db["bankroll"]
collection_envois = db["envois_auto"]
collection_abonnes = db["abonnes"]


def verifier_mongo():
    """Teste la connexion Mongo au demarrage pour un diagnostic clair."""
    try:
        mongo_client.admin.command("ping")
        log.info("Connexion MongoDB OK")
        return True
    except PyMongoError as e:
        log.error("ERREUR MongoDB au demarrage : %s", e)
        return False


# ---------------------------------------------------------------------------
# Bankroll & paris
# ---------------------------------------------------------------------------
def get_bankroll(user_id):
    """Bankroll propre a chaque utilisateur (identifie par son chat id)."""
    try:
        doc = collection_bankroll.find_one({"id": str(user_id)})
        if doc:
            return doc["capital"]
        collection_bankroll.insert_one({"id": str(user_id), "capital": CAPITAL_INITIAL})
        return CAPITAL_INITIAL
    except PyMongoError as e:
        log.error("get_bankroll : %s", e)
        return CAPITAL_INITIAL


def update_bankroll(user_id, nouveau_capital):
    try:
        collection_bankroll.update_one(
            {"id": str(user_id)}, {"$set": {"capital": nouveau_capital}}, upsert=True
        )
    except PyMongoError as e:
        log.error("update_bankroll : %s", e)


def calculer_mise(user_id, confiance, cote, bankroll):
    """Calcule la mise conseillee avec gestion de bankroll securisee.

    - Kelly fractionne base sur la cote reelle, plafonne entre 2% et 10%.
    - Ajuste par le coefficient d'apprentissage (taux de reussite passe).
    - Reduit les mises si la bankroll a beaucoup baisse (mode protection).
    - Ne mise jamais plus que ce qui reste, ni moins d'un plancher.
    """
    p = confiance / 100.0
    b = max(cote - 1, 0.01)
    kelly = (b * p - (1 - p)) / b
    kelly = max(0.02, min(kelly, 0.10))

    mise = bankroll * kelly * coefficient_apprentissage(user_id)

    # Protection : si la bankroll est tombee sous 50% du capital initial,
    # on reduit encore les mises pour eviter de tout perdre.
    if bankroll < CAPITAL_INITIAL * 0.5:
        mise *= 0.5

    # Plancher : pas de mise en dessous de 0.50 (sauf si la bankroll est
    # elle-meme inferieure, auquel cas on mise ce qui reste).
    mise = max(mise, min(0.50, bankroll))

    # Plafond absolu : ne jamais miser plus que la bankroll disponible.
    mise = min(mise, bankroll)

    return round(mise, 2)


def sauvegarder_pari(user_id, match, sport, pari, confiance, mise, cote):
    try:
        pari_id = f"{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        collection_paris.insert_one({
            "_id": pari_id,
            "user_id": str(user_id),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date_jour": datetime.now().strftime("%Y-%m-%d"),
            "match": match,
            "sport": sport,
            "pari": pari,
            "confiance": confiance,
            "mise": mise,
            "cote": cote,
            "resultat": "en attente",
            "gain": 0,
        })
        return pari_id
    except PyMongoError as e:
        log.error("sauvegarder_pari : %s", e)
        return None


def get_statistiques(user_id):
    try:
        paris = list(collection_paris.find(
            {"user_id": str(user_id), "resultat": {"$in": ["gagne", "perdu"]}}))
    except PyMongoError as e:
        log.error("get_statistiques : %s", e)
        return "Erreur de connexion a la base de donnees."
    if not paris:
        return "Aucun pari termine pour le moment."
    total = len(paris)
    gagnes = len([p for p in paris if p["resultat"] == "gagne"])
    pertes = len([p for p in paris if p["resultat"] == "perdu"])
    gain_total = sum(p.get("gain", 0) for p in paris)
    taux = round(gagnes / total * 100, 1) if total else 0
    return (
        "STATISTIQUES :\n"
        f"Total paris : {total}\n"
        f"Gagnes : {gagnes} | Perdus : {pertes}\n"
        f"Taux de reussite : {taux}%\n"
        f"Gain/Perte total : {gain_total:.2f}\n"
        f"Bankroll actuelle : {get_bankroll(user_id):.2f}"
    )


def tableau_bord_bankroll(user_id):
    """Tableau de bord complet : capital, gains, pertes, ROI, etc."""
    bankroll = get_bankroll(user_id)
    try:
        regles = list(collection_paris.find(
            {"user_id": str(user_id), "resultat": {"$in": ["gagne", "perdu"]}}))
        attente = list(collection_paris.find(
            {"user_id": str(user_id), "resultat": "en attente"}))
    except PyMongoError as e:
        log.error("tableau_bord_bankroll : %s", e)
        return "Erreur de connexion a la base de donnees."

    gagnes = [p for p in regles if p["resultat"] == "gagne"]
    perdus = [p for p in regles if p["resultat"] == "perdu"]
    total_regles = len(regles)

    total_mise = sum(p.get("mise", 0) for p in regles)        # mise sur paris termines
    gains_bruts = sum(p.get("gain", 0) for p in gagnes)       # benefices des gagnes (>0)
    pertes_brutes = sum(p.get("gain", 0) for p in perdus)     # pertes des perdus (<0)
    net = gains_bruts + pertes_brutes                         # resultat net
    mise_en_cours = sum(p.get("mise", 0) for p in attente)    # argent engage non resolu

    taux = round(len(gagnes) / total_regles * 100, 1) if total_regles else 0
    roi = round(net / total_mise * 100, 1) if total_mise else 0
    evolution = round((bankroll - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100, 1) \
        if CAPITAL_INITIAL else 0

    fleche = "📈" if net >= 0 else "📉"
    coef = coefficient_apprentissage(user_id)
    mode = ({1.2: "agressif (+20%)", 1.0: "normal", 0.6: "prudent (-40%)"}
            .get(coef, "normal"))

    return (
        "💰 GESTION DE LA BANKROLL\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Capital de depart : {CAPITAL_INITIAL:.2f}\n"
        f"Capital actuel    : {bankroll:.2f}\n"
        f"Evolution         : {evolution:+.1f}%\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{fleche} Resultat net   : {net:+.2f}\n"
        f"✅ Total gagne    : {gains_bruts:+.2f}\n"
        f"❌ Total perdu    : {pertes_brutes:+.2f}\n"
        f"💵 Total mise     : {total_mise:.2f}\n"
        f"⏳ Engage en cours : {mise_en_cours:.2f} ({len(attente)} pari(s))\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Paris termines    : {total_regles}\n"
        f"Gagnes / Perdus   : {len(gagnes)} / {len(perdus)}\n"
        f"Taux de reussite  : {taux}%\n"
        f"ROI               : {roi:+.1f}%\n"
        f"Mode de mise      : {mode}"
    )


# ---------------------------------------------------------------------------
# Reglement des paris, apprentissage & verification automatique
# ---------------------------------------------------------------------------
def coefficient_apprentissage(user_id):
    """Ajuste les mises selon le taux de reussite reel passe (par utilisateur).

    - taux >= 60% : on mise un peu plus (coef 1.2)
    - taux 45-60% : mise normale (coef 1.0)
    - taux < 45%  : on mise moins pour proteger la bankroll (coef 0.6)
    Tant qu'il y a moins de 10 paris regles, on reste neutre (coef 1.0).
    """
    try:
        regles = list(collection_paris.find(
            {"user_id": str(user_id), "resultat": {"$in": ["gagne", "perdu"]}}))
    except PyMongoError as e:
        log.error("coefficient_apprentissage : %s", e)
        return 1.0
    if len(regles) < 10:
        return 1.0
    gagnes = len([p for p in regles if p["resultat"] == "gagne"])
    taux = gagnes / len(regles)
    if taux >= 0.60:
        return 1.2
    if taux >= 0.45:
        return 1.0
    return 0.6


def regler_pari(pari, gagne):
    """Met a jour un pari (gagne/perdu), recalcule la bankroll du proprietaire."""
    user_id = pari.get("user_id", MON_CHAT_ID)
    mise = pari.get("mise", 0)
    cote = pari.get("cote", 1)
    if gagne:
        gain = round(mise * (cote - 1), 2)   # benefice net
        resultat = "gagne"
    else:
        gain = round(-mise, 2)               # on perd la mise
        resultat = "perdu"
    try:
        collection_paris.update_one(
            {"_id": pari["_id"]},
            {"$set": {"resultat": resultat, "gain": gain}},
        )
        nouvelle_bankroll = round(get_bankroll(user_id) + gain, 2)
        update_bankroll(user_id, nouvelle_bankroll)
        log.info("Pari regle : %s -> %s (%.2f)", pari["match"], resultat, gain)
        return gain, nouvelle_bankroll
    except PyMongoError as e:
        log.error("regler_pari : %s", e)
        return 0, get_bankroll(user_id)


def score_match_foot(nom_match):
    """Cherche le score final d'un match de foot du jour via l'API.

    Renvoie (buts_domicile, buts_exterieur) ou None si introuvable / non termine.
    """
    if not FOOTBALL_API_KEY:
        return None
    try:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d")},
            timeout=15,
        )
        for m in r.json().get("response", []):
            dom = m["teams"]["home"]["name"]
            ext = m["teams"]["away"]["name"]
            statut = m["fixture"]["status"]["short"]
            # FT = Full Time (match termine)
            if statut == "FT" and (dom.lower() in nom_match.lower()
                                   or ext.lower() in nom_match.lower()):
                return (m["goals"]["home"], m["goals"]["away"], dom, ext)
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        log.error("score_match_foot : %s", e)
        return None


def score_live(nom_match, sport):
    """Recupere le score EN DIRECT d'un match en cours.

    Renvoie un texte decrivant l'etat du match (score, minute/sets) ou None.
    """
    try:
        if sport == "foot" and FOOTBALL_API_KEY:
            r = requests.get(
                "https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": FOOTBALL_API_KEY},
                params={"live": "all"},
                timeout=10,
            )
            for m in r.json().get("response", []):
                dom = m["teams"]["home"]["name"]
                ext = m["teams"]["away"]["name"]
                if dom.lower() in nom_match.lower() or ext.lower() in nom_match.lower():
                    minute = m["fixture"]["status"].get("elapsed", "?")
                    bd, be = m["goals"]["home"], m["goals"]["away"]
                    return f"EN DIRECT : {dom} {bd} - {be} {ext} ({minute}e minute)"
            return None

        if sport == "tennis" and TENNIS_API_KEY:
            r = requests.get(
                "https://v1.tennis.api-sports.io/games",
                headers={"x-apisports-key": TENNIS_API_KEY},
                params={"date": datetime.now().strftime("%Y-%m-%d")},
                timeout=10,
            )
            for m in r.json().get("response", []):
                try:
                    j1 = m["players"]["home"]["name"]
                    j2 = m["players"]["away"]["name"]
                    if j1.lower() in nom_match.lower() or j2.lower() in nom_match.lower():
                        statut = m.get("status", {}).get("long", "en cours")
                        score = m.get("scores", m.get("score", "score indisponible"))
                        return f"EN DIRECT : {j1} vs {j2} | {statut} | {score}"
                except (KeyError, TypeError):
                    continue
            return None
    except (requests.RequestException, ValueError, KeyError) as e:
        log.error("score_live : %s", e)
        return None
    return None


def analyser_live(match, sport):
    """Analyse un match EN COURS : recupere le score live + recommande
    les paris les plus probables a l'instant present."""
    etat = score_live(match, sport)
    if not etat:
        etat = ("Score live indisponible pour ce match (peut-etre pas encore "
                "commence, deja fini, ou hors des ligues suivies).")

    if sport == "tennis":
        infos = recherche_web(f"{match} tennis score live en direct 2026")
        infos += recherche_web(f"{match} tennis momentum set en cours")
    else:
        infos = recherche_web(f"{match} football score live en direct 2026")
        infos += recherche_web(f"{match} football momentum cartons occasions")

    prompt = f"""Tu es un expert en paris sportifs LIVE (en direct).
Le match est DEJA EN COURS. Voici son etat actuel :
{etat}

Infos web complementaires :
{infos}

MATCH : {match}

Donne une mise a jour LIVE en francais, structuree ainsi :

⚡ MISE A JOUR LIVE : {match}
📍 Etat actuel : [score et temps ecoule]

📊 SITUATION :
→ [Qui domine, dynamique du match, ce qui a change]

🎯 PARIS LES PLUS PROBABLES MAINTENANT :
→ [Pari 1] : [probabilite %] | [justification courte]
→ [Pari 2] : [probabilite %] | [justification courte]
→ [Pari 3] : [probabilite %] | [justification courte]

💡 RECOMMANDATION LIVE :
PARI: [le pari le plus sur a l'instant T]
CONFIANCE: [0-100]
COTE: [cote estimee]

Base-toi sur l'etat REEL du match. Si tu n'as pas le score exact, dis-le
clairement et reste prudent."""

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=2000,
        )
        return completion.choices[0].message.content
    except Exception as e:
        log.error("analyser_live : %s", e)
        return "Erreur lors de l'analyse live. Reessaie dans un instant."



    """Verifie les paris foot 1N2 en attente et les regle si le match est fini."""
    try:
        en_attente = list(collection_paris.find({
            "resultat": "en attente", "sport": "foot",
        }))
    except PyMongoError as e:
        log.error("verifier_resultats_auto : %s", e)
        return

    for p in en_attente:
        infos = score_match_foot(p["match"])
        if not infos:
            continue
        buts_dom, buts_ext, dom, ext = infos
        pari_txt = (p.get("pari") or "").lower()

        # On ne gere automatiquement que les paris simples 1N2.
        gagne = None
        if "domicile" in pari_txt or dom.lower() in pari_txt or "victoire 1" in pari_txt:
            gagne = buts_dom > buts_ext
        elif "exterieur" in pari_txt or ext.lower() in pari_txt or "victoire 2" in pari_txt:
            gagne = buts_ext > buts_dom
        elif "nul" in pari_txt or "match nul" in pari_txt:
            gagne = buts_dom == buts_ext

        if gagne is None:
            # Pari trop complexe pour l'auto : on laisse pour reglement manuel.
            continue

        gain, bankroll = regler_pari(p, gagne)
        try:
            statut = "GAGNE ✅" if gagne else "PERDU ❌"
            destinataire = p.get("user_id", MON_CHAT_ID)
            bot.send_message(
                destinataire,
                f"Resultat automatique\n{p['match']} : {buts_dom}-{buts_ext}\n"
                f"Pari : {p['pari']}\n{statut} | Gain : {gain:+.2f}\n"
                f"Nouvelle bankroll : {bankroll:.2f}",
            )
        except Exception as e:
            log.error("Notif resultat auto : %s", e)


# ---------------------------------------------------------------------------
# Recherche web & analyse IA
# ---------------------------------------------------------------------------
def recherche_web(query):
    if not SEARCH_API_KEY:
        return ""
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SEARCH_API_KEY, "num": 5, "hl": "fr"},
            timeout=10,
        )
        data = r.json()
        return "\n".join(
            f"{res.get('title', '')} - {res.get('snippet', '')}"
            for res in data.get("organic_results", [])[:5]
        )
    except (requests.RequestException, ValueError) as e:
        log.error("recherche_web : %s", e)
        return ""


def detecter_sport(texte):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content":
                f"Ce texte parle de tennis ou de football ? Reponds uniquement 'tennis' ou 'foot' : {texte}"}],
            model="llama-3.3-70b-versatile",
        )
        if "tennis" in chat.choices[0].message.content.lower():
            return "tennis"
        return "foot"
    except Exception as e:
        log.error("detecter_sport (fallback) : %s", e)
        t = texte.lower()
        if any(m in t for m in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"


def stats_reelles_foot(nom_match):
    """Recupere de VRAIES donnees API pour un match de foot du jour :
    forme recente des 2 equipes, confrontations directes (H2H), classements.
    Renvoie un texte pret a injecter dans le prompt, ou "" si indisponible.
    """
    if not FOOTBALL_API_KEY:
        return ""
    head = {"x-apisports-key": FOOTBALL_API_KEY}
    base = "https://v3.football.api-sports.io"
    try:
        # 1. Identifier les deux equipes par leur nom (clubs OU selections).
        #    On cherche le fixture du jour SANS filtre de ligue, pour couvrir
        #    aussi les matchs internationaux (amicaux, qualifs, Coupe du monde).
        aujourd_hui = datetime.now().strftime("%Y-%m-%d")
        fixture = None

        # a) On essaie d'abord tous les matchs du jour, toutes competitions.
        r = requests.get(
            f"{base}/fixtures", headers=head,
            params={"date": aujourd_hui}, timeout=15,
        )
        for m in r.json().get("response", []):
            dom = m["teams"]["home"]["name"]
            ext = m["teams"]["away"]["name"]
            if dom.lower() in nom_match.lower() and ext.lower() in nom_match.lower():
                fixture = m
                break
        # b) Sinon, on accepte un match ou une seule des deux equipes correspond.
        if not fixture:
            for m in r.json().get("response", []):
                dom = m["teams"]["home"]["name"]
                ext = m["teams"]["away"]["name"]
                if dom.lower() in nom_match.lower() or ext.lower() in nom_match.lower():
                    fixture = m
                    break
        if not fixture:
            return ""

        id_dom = fixture["teams"]["home"]["id"]
        id_ext = fixture["teams"]["away"]["id"]
        nom_dom = fixture["teams"]["home"]["name"]
        nom_ext = fixture["teams"]["away"]["name"]
        id_ligue = fixture["league"]["id"]
        saison_match = fixture["league"].get("season", SAISON_FOOT)
        id_fixture = fixture["fixture"]["id"]

        lignes = ["=== DONNEES REELLES API (fiables) ==="]
        lignes.append(f'Competition : {fixture["league"].get("name", "?")}')

        # 2. Forme recente : 5 derniers matchs de chaque equipe.
        def forme(team_id, team_nom):
            rr = requests.get(
                f"{base}/fixtures", headers=head,
                params={"team": team_id, "last": 5}, timeout=10,
            )
            res = []
            for f in rr.json().get("response", []):
                try:
                    h = f["teams"]["home"]["name"]
                    a = f["teams"]["away"]["name"]
                    bh = f["goals"]["home"]
                    ba = f["goals"]["away"]
                    res.append(f"{h} {bh}-{ba} {a}")
                except (KeyError, TypeError):
                    continue
            return f"{team_nom} (5 derniers) : " + " | ".join(res) if res else ""

        f_dom = forme(id_dom, nom_dom)
        f_ext = forme(id_ext, nom_ext)
        if f_dom:
            lignes.append(f_dom)
        if f_ext:
            lignes.append(f_ext)

        # 3. Head to head : 5 dernieres confrontations directes.
        rh = requests.get(
            f"{base}/fixtures/headtohead", headers=head,
            params={"h2h": f"{id_dom}-{id_ext}", "last": 5}, timeout=10,
        )
        h2h = []
        for f in rh.json().get("response", []):
            try:
                h = f["teams"]["home"]["name"]
                a = f["teams"]["away"]["name"]
                bh = f["goals"]["home"]
                ba = f["goals"]["away"]
                d = f["fixture"]["date"][:10]
                h2h.append(f"{d} : {h} {bh}-{ba} {a}")
            except (KeyError, TypeError):
                continue
        if h2h:
            lignes.append("Confrontations directes : " + " | ".join(h2h))

        # 4. Classement des 2 equipes dans la ligue (si c'est un championnat).
        try:
            rs = requests.get(
                f"{base}/standings", headers=head,
                params={"league": id_ligue, "season": saison_match}, timeout=10,
            )
            classement = rs.json()["response"][0]["league"]["standings"][0]
            for equipe in classement:
                if equipe["team"]["id"] in (id_dom, id_ext):
                    lignes.append(
                        f'{equipe["team"]["name"]} : {equipe["rank"]}e, '
                        f'{equipe["points"]} pts, forme {equipe.get("form", "?")}'
                    )
        except (KeyError, IndexError, TypeError, requests.RequestException):
            # Pas de classement pour un amical / match international : normal.
            pass

        # 5. Blessures et absences confirmees pour ce match.
        try:
            ri = requests.get(
                f"{base}/injuries", headers=head,
                params={"fixture": id_fixture}, timeout=10,
            )
            blesses = {nom_dom: [], nom_ext: []}
            for inj in ri.json().get("response", []):
                try:
                    equipe = inj["team"]["name"]
                    joueur = inj["player"]["name"]
                    raison = inj["player"].get("reason", "indisponible")
                    if equipe in blesses:
                        blesses[equipe].append(f"{joueur} ({raison})")
                except (KeyError, TypeError):
                    continue
            for equipe, liste in blesses.items():
                if liste:
                    lignes.append(f"Absents/blesses {equipe} : " + ", ".join(liste))
                else:
                    lignes.append(f"Absents/blesses {equipe} : aucun confirme par l'API")
        except (requests.RequestException, ValueError, KeyError):
            pass

        # 6. Compositions probables / officielles (dispo ~1h avant le match).
        try:
            rl = requests.get(
                f"{base}/fixtures/lineups", headers=head,
                params={"fixture": id_fixture}, timeout=10,
            )
            data_l = rl.json().get("response", [])
            if data_l:
                for equipe in data_l:
                    try:
                        nom_eq = equipe["team"]["name"]
                        formation = equipe.get("formation", "?")
                        titulaires = ", ".join(
                            j["player"]["name"] for j in equipe.get("startXI", [])
                        )
                        lignes.append(
                            f"Compo {nom_eq} ({formation}) : {titulaires}"
                            if titulaires else f"Compo {nom_eq} : non disponible")
                    except (KeyError, TypeError):
                        continue
            else:
                lignes.append("Compositions : pas encore publiees "
                              "(elles sortent ~1h avant le coup d'envoi)")
        except (requests.RequestException, ValueError, KeyError):
            pass

        return "\n".join(lignes) + "\n"
    except (requests.RequestException, ValueError, KeyError) as e:
        log.error("stats_reelles_foot : %s", e)
        return ""


def analyser_match(match, sport):
    if sport == "tennis":
        infos = recherche_web(f"{match} tennis stats forme recente 2026")
        infos += recherche_web(f"{match} head to head historique surface")
        infos += recherche_web(f"{match} blessure actualite 2026")
        infos += recherche_web(f"{match} classement ATP WTA 2026")
        infos += recherche_web(f"{match} pronostic cote bookmaker 2026")
        prompt = f"""Tu es un analyste TENNIS rigoureux et HONNETE.
Ces personnes sont des JOUEURS DE TENNIS professionnels. Ne parle JAMAIS de football.

REGLES ABSOLUES :
1. Si tu n'as PAS une information, ecris "Donnee non disponible".
2. N'INVENTE JAMAIS un score, un classement, une blessure ou une stat.
3. Mieux vaut admettre "non disponible" que donner une fausse information.
4. MISE EN PAGE : saute une ligne vide entre chaque section pour aerer.

Infos collectees sur le web (a recouper, pas toujours fiables) :
{infos}

MATCH : {match}

Fournis une analyse complete et structuree exactement comme ceci, en francais :

🎾 MATCH : {match}
🏆 Tournoi : [nom] | Surface : [surface]

📊 FORME RECENTE (5 derniers matchs) :
→ [Joueur1] : [W-L] | Derniers resultats avec scores exacts
→ [Joueur2] : [W-L] | Derniers resultats avec scores exacts

🎯 SURFACE :
→ Stats sur cette surface cette saison pour chaque joueur (victoires/defaites)
→ Avantage : [Joueur qui a l'avantage sur cette surface et pourquoi]

🔄 HEAD TO HEAD :
→ Historique global : [X-Y]
→ Dernier match : [date + score]

🤕 BLESSURES & FORME PHYSIQUE :
→ [Joueur1] : [etat physique confirme ou RAS]
→ [Joueur2] : [etat physique confirme ou RAS]

📰 NEWS IMPORTANTES :
→ [Infos recentes importantes pour ce match]

📈 PROBABILITES :
→ [Joueur1] : XX%
→ [Joueur2] : XX%

🎯 SCORE PREDIT : X-X sets

💡 ANALYSE TACTIQUE :
→ Style de jeu de chaque joueur et comment ils s'affrontent sur cette surface

Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :
PARI: [ton pronostic - le plus SUR, pas le plus spectaculaire]
CONFIANCE: [pourcentage HONNETE entre 50 et 95 - peu de donnees = confiance basse]
COTE: [cote estimee realiste, ex 1.85]

RAPPEL : si les donnees sont minces, baisse la confiance et reste prudent.
Ne gonfle jamais la confiance pour faire plaisir."""
    else:
        donnees_api = stats_reelles_foot(match)
        infos = recherche_web(f"{match} compo probable composition equipe 2026")
        infos += recherche_web(f"{match} blessures absents suspendus derniere minute 2026")
        infos += recherche_web(f"{match} cotes bookmakers pronostic 2026")
        infos += recherche_web(f"{match} actualite news avant match 2026")
        infos += recherche_web(f"{match} enjeu motivation contexte classement 2026")
        infos += recherche_web(f"{match} declaration entraineur conference presse 2026")
        infos += recherche_web(f"{match} buteurs forme recente 2026")
        prompt = f"""Tu es un analyste FOOTBALL rigoureux et HONNETE.

REGLES ABSOLUES :
1. Les DONNEES REELLES API ci-dessous sont fiables : utilise-les en PRIORITE.
2. Pour tout le reste, si tu n'as PAS l'information, ecris "Donnee non disponible".
   N'INVENTE JAMAIS un chiffre, un score, une blessure ou une stat.
3. Mieux vaut admettre "non disponible" que donner une fausse information.
4. Utilise uniquement les joueurs actuellement dans ces clubs.
5. MISE EN PAGE : saute une ligne vide entre chaque section pour aerer.

{donnees_api}
Infos web complementaires (moins fiables, a recouper) :
{infos}

MATCH : {match}

Fournis une analyse structuree exactement comme ceci, en francais.
Quand tu utilises une donnee API fiable, ajoute (API) a la fin de la ligne.
Quand une info manque, ecris "Donnee non disponible" :

⚽ MATCH : {match}
🏆 Competition : [nom]

📊 FORME ACTUELLE (5 derniers matchs) :
→ [Equipe1] : [resultats reels] | Serie actuelle
→ [Equipe2] : [resultats reels] | Serie actuelle

🏠 DOMICILE / EXTERIEUR :
→ [Equipe1] a domicile cette saison : [W-D-L ou non disponible]
→ [Equipe2] a l'exterieur cette saison : [W-D-L ou non disponible]

👥 JOUEURS CLES (noms reels) :
→ [Equipe1] : [Nom joueur] - [Stat ou non disponible]
→ [Equipe2] : [Nom joueur] - [Stat ou non disponible]

🚑 BLESSURES & ABSENCES :
→ [Equipe1] : [Noms confirmes ou Aucune absence confirmee]
→ [Equipe2] : [Noms confirmes ou Aucune absence confirmee]

🔄 HEAD TO HEAD :
→ Historique global : [X victoires E1 / X nuls / X victoires E2]
→ Dernier match : [date + score]

📰 NEWS IMPORTANTES :
→ [Infos recentes qui impactent le match : motivation, contexte, etc.]

📈 PROBABILITES :
→ [Equipe1] : XX%
→ Match nul : XX%
→ [Equipe2] : XX%

🎯 SCORE PROBABLE : X - X

⚽ BUTEURS PROBABLES :
→ [Equipe1] : [Nom] (XX% de chances - X buts cette saison)
→ [Equipe2] : [Nom] (XX% de chances - X buts cette saison)

🔢 NOMBRE DE BUTS :
→ Plus de 2.5 : XX%
→ Moins de 2.5 : XX%
→ Les deux equipes marquent (BTTS) : XX%

💡 ANALYSE TACTIQUE :
→ Systeme de jeu de chaque equipe et avantages/faiblesses

Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :
PARI: [ton pronostic - choisis le pari le plus SUR, pas le plus spectaculaire]
CONFIANCE: [pourcentage HONNETE entre 50 et 95 - base-le sur la quantite de
donnees fiables dont tu disposes : peu de donnees = confiance basse]
COTE: [cote estimee realiste, ex 1.85]

RAPPEL : appuie ta CONFIANCE et ton PARI sur les DONNEES API reelles en priorite.
Si les donnees sont minces, baisse la confiance et choisis un pari prudent.
Ne gonfle jamais la confiance pour faire plaisir."""
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        return chat.choices[0].message.content
    except Exception as e:
        log.error("analyser_match : %s", e)
        return f"Erreur analyse IA : {e}"


def parser_analyse(texte):
    pari, confiance, cote = "Non determine", 50.0, 1.5
    for ligne in texte.split("\n"):
        l = ligne.strip()
        u = l.upper()
        if u.startswith("PARI:"):
            pari = l.split(":", 1)[1].strip()
        elif u.startswith("CONFIANCE:"):
            try:
                confiance = float("".join(c for c in l.split(":", 1)[1] if c.isdigit() or c == "."))
            except ValueError:
                pass
        elif u.startswith("COTE:"):
            try:
                cote = float("".join(c for c in l.split(":", 1)[1] if c.isdigit() or c == "."))
            except ValueError:
                pass
    confiance = max(50.0, min(confiance, 95.0))
    cote = max(1.01, cote)
    return pari, confiance, cote


# ---------------------------------------------------------------------------
# Recuperation des matchs
# ---------------------------------------------------------------------------
def get_matchs_foot():
    if not FOOTBALL_API_KEY:
        return []
    # Ligues de clubs : PL(39) Liga(140) SerieA(135) Bundesliga(78) L1(61)
    # ChampionsL(2) EuropaL(3). Internationaux : CdM(1) Euro(4) Nations(5)
    # amicaux(10) + qualifs CdM par confederation (29,30,31,32,33,34).
    ligues = "1,2,3,4,5,10,29,30,31,32,33,34,39,61,78,135,140"
    try:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d"),
                    "league": ligues, "season": SAISON_FOOT},
            timeout=15,
        )
        data = r.json()
        matchs = []
        for m in data.get("response", []):
            matchs.append({
                "heure": m["fixture"]["date"][11:16],
                "match": f'{m["teams"]["home"]["name"]} vs {m["teams"]["away"]["name"]}',
                "ligue": m["league"]["name"],
            })
        return matchs
    except (requests.RequestException, ValueError, KeyError) as e:
        log.error("get_matchs_foot : %s", e)
        return []


def get_matchs_tennis():
    if not TENNIS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://v1.tennis.api-sports.io/games",
            headers={"x-apisports-key": TENNIS_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d")},
            timeout=10,
        )
        data = r.json()
        matchs = []
        for m in data.get("response", []):
            try:
                matchs.append({
                    "heure": m["date"][11:16],
                    "match": f'{m["players"]["home"]["name"]} vs {m["players"]["away"]["name"]}',
                    "tournoi": m.get("tournament", {}).get("name", "Tournoi inconnu"),
                    "surface": m.get("surface", "Inconnue"),
                })
            except (KeyError, TypeError):
                continue
        return matchs
    except (requests.RequestException, ValueError) as e:
        log.error("get_matchs_tennis : %s", e)
        return []


# ---------------------------------------------------------------------------
# Handlers Telegram
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    # On enregistre l'utilisateur comme abonne aux pronostics automatiques.
    try:
        collection_abonnes.update_one(
            {"_id": str(message.chat.id)},
            {"$set": {"actif": True,
                      "depuis": datetime.now().isoformat()}},
            upsert=True,
        )
    except PyMongoError as e:
        log.error("inscription abonne : %s", e)
    bot.reply_to(
        message,
        "Bot de pronostics actif. Tu es abonne aux pronostics automatiques "
        "(30 min avant chaque match).\n\n"
        "Envoie un match a analyser, ou utilise :\n"
        "/matchs - matchs du jour\n"
        "/stats - statistiques\n"
        "/bankroll - capital actuel\n"
        "/historique - derniers paris\n"
        "/enattente - paris a regler\n"
        "/gagne <numero> - marquer un pari gagne\n"
        "/perdu <numero> - marquer un pari perdu\n"
        "/stop - ne plus recevoir les pronostics automatiques\n\n"
        "Sous chaque pronostic : boutons Gagne / Perdu / Non pris / "
        "Mise a jour live.\n\n"
        "Chaque utilisateur a sa propre bankroll et ses propres stats.",
    )


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    try:
        collection_abonnes.update_one(
            {"_id": str(message.chat.id)},
            {"$set": {"actif": False}},
            upsert=True,
        )
    except PyMongoError:
        pass
    bot.reply_to(message, "Tu ne recevras plus les pronostics automatiques. "
                          "Tape /start pour te reabonner.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    bot.reply_to(message, get_statistiques(message.chat.id))


@bot.message_handler(commands=["bankroll"])
def cmd_bankroll(message):
    bot.reply_to(message, tableau_bord_bankroll(message.chat.id))


@bot.message_handler(commands=["historique"])
def cmd_historique(message):
    try:
        paris = list(collection_paris.find(
            {"user_id": str(message.chat.id)}).sort("_id", -1).limit(10))
    except PyMongoError:
        bot.reply_to(message, "Erreur base de donnees.")
        return
    if not paris:
        bot.reply_to(message, "Aucun pari enregistre.")
        return
    lignes = ["DERNIERS PARIS :\n"]
    for p in paris:
        icone = {"gagne": "✅", "perdu": "❌", "non pris": "🚫"}.get(p["resultat"], "⏳")
        lignes.append(
            f'{icone} {p["match"]}\n'
            f'   {p.get("pari", "?")} | cote {p.get("cote", "?")} | '
            f'mise {p.get("mise", 0):.2f} | gain {p.get("gain", 0):+.2f}'
        )
    bot.reply_to(message, "\n".join(lignes))


@bot.message_handler(commands=["enattente"])
def cmd_enattente(message):
    """Liste les paris non regles avec leur numero, pour /gagne et /perdu."""
    try:
        paris = list(collection_paris.find(
            {"user_id": str(message.chat.id), "resultat": "en attente"}).sort("_id", -1))
    except PyMongoError:
        bot.reply_to(message, "Erreur base de donnees.")
        return
    if not paris:
        bot.reply_to(message, "Aucun pari en attente.")
        return
    lignes = ["PARIS EN ATTENTE :\n",
              "Pour regler : /gagne <numero> ou /perdu <numero>\n"]
    for i, p in enumerate(paris, start=1):
        lignes.append(f'{i}. {p["match"]} - {p.get("pari", "?")} (cote {p.get("cote", "?")})')
    bot.reply_to(message, "\n".join(lignes))


def _regler_par_numero(message, gagne):
    """Logique commune a /gagne et /perdu : retrouve le pari par son numero."""
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage : /gagne <numero>\nVois les numeros avec /enattente")
        return
    numero = int(parts[1])
    try:
        paris = list(collection_paris.find(
            {"user_id": str(message.chat.id), "resultat": "en attente"}).sort("_id", -1))
    except PyMongoError:
        bot.reply_to(message, "Erreur base de donnees.")
        return
    if numero < 1 or numero > len(paris):
        bot.reply_to(message, f"Numero invalide. Il y a {len(paris)} pari(s) en attente.")
        return
    pari = paris[numero - 1]
    gain, bankroll = regler_pari(pari, gagne)
    statut = "GAGNE ✅" if gagne else "PERDU ❌"
    bot.reply_to(
        message,
        f"{pari['match']}\n{statut} | Gain : {gain:+.2f}\n"
        f"Nouvelle bankroll : {bankroll:.2f}",
    )


@bot.message_handler(commands=["gagne"])
def cmd_gagne(message):
    _regler_par_numero(message, gagne=True)


@bot.message_handler(commands=["perdu"])
def cmd_perdu(message):
    _regler_par_numero(message, gagne=False)


@bot.callback_query_handler(func=lambda c: c.data and c.data[:2] in ("g:", "p:", "n:", "l:"))
def clic_bouton(call):
    """Reagit aux boutons sous un pronostic : gagne, perdu, non pris, live."""
    action, pari_id = call.data.split(":", 1)
    try:
        pari = collection_paris.find_one({"_id": pari_id})
    except PyMongoError:
        bot.answer_callback_query(call.id, "Erreur base de donnees.")
        return
    if not pari:
        bot.answer_callback_query(call.id, "Pari introuvable.")
        return

    # --- Bouton Mise a jour live : relance une analyse du match en cours ---
    if action == "l":
        bot.answer_callback_query(call.id, "Analyse live en cours...")
        bot.send_message(call.message.chat.id, "🔄 Mise a jour live en cours...")
        analyse = analyser_live(pari["match"], pari.get("sport", "foot"))
        for i in range(0, len(analyse), 4000):
            bot.send_message(call.message.chat.id, analyse[i:i + 4000])
        return

    # Pour les autres actions, le pari ne doit pas etre deja regle.
    if pari.get("resultat") in ("gagne", "perdu", "non pris"):
        bot.answer_callback_query(call.id, f"Deja regle : {pari['resultat']}.")
        return

    # --- Bouton Non pris : on sort le pari des stats sans toucher la bankroll ---
    if action == "n":
        try:
            collection_paris.update_one(
                {"_id": pari_id},
                {"$set": {"resultat": "non pris", "gain": 0}},
            )
        except PyMongoError:
            bot.answer_callback_query(call.id, "Erreur base de donnees.")
            return
        bot.answer_callback_query(call.id, "Marque comme non pris.")
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(
                call.message.chat.id,
                f"🚫 {pari['match']}\nPari NON PRIS (exclu des stats et de la bankroll).",
            )
        except Exception as e:
            log.error("clic non pris : %s", e)
        return

    # --- Boutons Gagne / Perdu ---
    gagne = (action == "g")
    gain, bankroll = regler_pari(pari, gagne)
    statut = "GAGNE ✅" if gagne else "PERDU ❌"
    bot.answer_callback_query(call.id, f"{statut} | Gain {gain:+.2f}")
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(
            call.message.chat.id,
            f"{pari['match']}\n{statut} | Gain : {gain:+.2f}\n"
            f"Nouvelle bankroll : {bankroll:.2f}",
        )
    except Exception as e:
        log.error("clic_bouton : %s", e)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("bk:"))
def clic_bankroll(call):
    """Affiche le tableau de bord de la bankroll quand on clique le bouton."""
    bot.answer_callback_query(call.id, "Bankroll")
    try:
        bot.send_message(call.message.chat.id,
                         tableau_bord_bankroll(call.message.chat.id))
    except Exception as e:
        log.error("clic_bankroll : %s", e)


@bot.message_handler(commands=["matchs"])
def cmd_matchs(message):
    foot = get_matchs_foot()
    tennis = get_matchs_tennis()
    lignes = ["MATCHS DU JOUR\n", "FOOT :"]
    lignes += [f'{m["heure"]} - {m["match"]} ({m["ligue"]})' for m in foot] or ["  aucun"]
    lignes.append("\nTENNIS :")
    lignes += [f'{m["heure"]} - {m["match"]} ({m["tournoi"]})' for m in tennis] or ["  aucun"]
    bot.reply_to(message, "\n".join(lignes))


@bot.message_handler(func=lambda m: True)
def traiter_message(message):
    texte = message.text or ""
    sport = detecter_sport(texte)
    bot.reply_to(message, "Analyse en cours...")
    envoyer_analyse(message.chat.id, texte, sport)


def pastille_confiance(confiance):
    """Pastille couleur selon le niveau de confiance.
    🟢 elevee (>=70), 🟡 moyenne (55-69), 🔴 faible (<55)."""
    try:
        c = float(confiance)
    except (TypeError, ValueError):
        c = 0
    if c >= 70:
        return "🟢", "elevee"
    if c >= 55:
        return "🟡", "moyenne"
    return "🔴", "faible"


def envoyer_analyse(chat_id, texte, sport):
    """Analyse un match et envoie le resultat au chat. La bankroll, la mise
    et le pari sont propres a l'utilisateur (chat_id)."""
    analyse = analyser_match(texte, sport)
    pari, confiance, cote = parser_analyse(analyse)
    bankroll = get_bankroll(chat_id)
    mise = calculer_mise(chat_id, confiance, cote, bankroll)
    pari_id = sauvegarder_pari(chat_id, texte, sport, pari, confiance, mise, cote)

    icone_sport = "🎾" if sport == "tennis" else "⚽"
    gain_potentiel = round(mise * (cote - 1), 2) if cote else 0
    pastille, niveau = pastille_confiance(confiance)

    # Resume dans un bloc de code monospace : Telegram l'encadre proprement
    # et l'aligne parfaitement (comme les alertes trading). On retire les
    # accents graves eventuels du contenu pour ne pas casser le bloc.
    def _safe(v):
        return str(v).replace("`", "'")

    resume_brut = (
        f"{icone_sport} PRONOSTIC\n"
        f"────────────────\n"
        f"Pari      : {_safe(pari)}\n"
        f"Confiance : {confiance}% {pastille} ({niveau})\n"
        f"Cote      : {cote}\n"
        f"Mise      : {mise:.2f}\n"
        f"Bankroll  : {bankroll:.2f}\n"
        f"Gain pot. : +{gain_potentiel:.2f}"
    )
    resume = f"```\n{resume_brut}\n```\n\n📋 *ANALYSE DETAILLEE*\n"

    footer = (
        "\n\n⚠️ _Analyse generee par IA, a titre indicatif._\n"
        "_Ne parie que ce que tu peux te permettre de perdre._"
    )

    # L'analyse IA reste en texte normal (caracteres speciaux non maitrises).
    corps = analyse.strip() + footer

    # Boutons de suivi attaches au dernier morceau du message.
    clavier = telebot.types.InlineKeyboardMarkup()
    if pari_id:
        clavier.add(
            telebot.types.InlineKeyboardButton("✅ Gagne", callback_data=f"g:{pari_id}"),
            telebot.types.InlineKeyboardButton("❌ Perdu", callback_data=f"p:{pari_id}"),
        )
        clavier.add(
            telebot.types.InlineKeyboardButton("🚫 Non pris", callback_data=f"n:{pari_id}"),
            telebot.types.InlineKeyboardButton("🔄 Mise a jour live", callback_data=f"l:{pari_id}"),
        )
        clavier.add(
            telebot.types.InlineKeyboardButton("💰 Voir bankroll", callback_data="bk:0"),
        )

    # 1) Le resume encadre, en Markdown (sous notre controle, donc sans risque).
    try:
        bot.send_message(chat_id, resume, parse_mode="Markdown")
    except Exception as e:
        # Si jamais le Markdown echoue, on renvoie en texte brut.
        log.error("envoi resume markdown : %s", e)
        bot.send_message(chat_id, resume.replace("```", "").replace("*", "").replace("_", ""))

    # 2) Le corps de l'analyse en texte brut, decoupe si > 4000 caracteres.
    #    Les boutons sont attaches au tout dernier morceau.
    morceaux = [corps[i:i + 4000] for i in range(0, len(corps), 4000)] or [""]
    for idx, morceau in enumerate(morceaux):
        dernier = (idx == len(morceaux) - 1)
        bot.send_message(
            chat_id, morceau,
            reply_markup=clavier if (dernier and pari_id) else None,
        )


# ---------------------------------------------------------------------------
# Planificateur : pronostics automatiques 30 min avant chaque match
# ---------------------------------------------------------------------------
def deja_envoye(cle):
    """Verifie si un pronostic a deja ete envoye pour ce match aujourd'hui."""
    try:
        return collection_envois.find_one({"_id": cle}) is not None
    except PyMongoError as e:
        log.error("deja_envoye : %s", e)
        return False


def marquer_envoye(cle):
    try:
        collection_envois.update_one(
            {"_id": cle},
            {"$set": {"envoye_le": datetime.now().isoformat()}},
            upsert=True,
        )
    except PyMongoError as e:
        log.error("marquer_envoye : %s", e)


def minutes_avant_match(heure_str):
    """Retourne le nombre de minutes entre maintenant et l'heure du match.

    heure_str est au format 'HH:MM' (UTC, tel que renvoye par l'API).
    """
    try:
        maintenant = datetime.utcnow()
        h, m = heure_str.split(":")
        debut = maintenant.replace(hour=int(h), minute=int(m),
                                   second=0, microsecond=0)
        delta = (debut - maintenant).total_seconds() / 60.0
        return delta
    except (ValueError, AttributeError):
        return None


def liste_abonnes():
    """Renvoie la liste des chat ids abonnes actifs."""
    try:
        docs = list(collection_abonnes.find({"actif": True}))
        ids = [d["_id"] for d in docs]
        # Garantit que le proprietaire principal recoit toujours les pronostics.
        if MON_CHAT_ID not in ids:
            ids.append(MON_CHAT_ID)
        return ids
    except PyMongoError as e:
        log.error("liste_abonnes : %s", e)
        return [MON_CHAT_ID]


def verifier_et_envoyer():
    """Parcourt les matchs du jour et envoie ceux qui demarrent bientot
    a TOUS les abonnes actifs (chacun avec sa propre bankroll)."""
    aujourd_hui = datetime.utcnow().strftime("%Y-%m-%d")

    foot = [{**m, "sport": "foot"} for m in get_matchs_foot()]
    tennis = [{**m, "sport": "tennis"} for m in get_matchs_tennis()]

    for m in foot + tennis:
        minutes = minutes_avant_match(m["heure"])
        if minutes is None:
            continue
        # On envoie si le match commence dans MINUTES_AVANT a MINUTES_AVANT+FENETRE.
        if MINUTES_AVANT <= minutes < MINUTES_AVANT + FENETRE_MINUTES:
            for abonne in liste_abonnes():
                # Cle unique par abonne ET par match : chacun le recoit une fois.
                cle = f'{aujourd_hui}_{abonne}_{m["sport"]}_{m["match"]}'
                if deja_envoye(cle):
                    continue
                try:
                    bot.send_message(
                        abonne,
                        f'⏰ Match dans ~{int(minutes)} min\n{m["match"]} '
                        f'({m["heure"]} UTC)\nAnalyse en cours...',
                    )
                    envoyer_analyse(abonne, m["match"], m["sport"])
                    marquer_envoye(cle)
                    log.info("Pronostic auto envoye a %s : %s", abonne, m["match"])
                except Exception as e:
                    log.error("Echec envoi auto (%s) %s : %s", abonne, m["match"], e)


def lancer_planificateur():
    """Boucle infinie : verifie les matchs toutes les 5 minutes."""
    log.info("Planificateur de pronostics automatiques demarre")
    while True:
        try:
            verifier_et_envoyer()
            verifier_resultats_auto()
        except Exception as e:
            log.error("Erreur planificateur : %s", e)
        time.sleep(300)  # 5 minutes


# ---------------------------------------------------------------------------
# Serveur HTTP (health check Render) + lancement
# ---------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def lancer_serveur():
    port = int(os.environ.get("PORT", 8080))
    log.info("Serveur health check sur le port %s", port)
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def lancer_bot():
    while True:
        try:
            log.info("Demarrage du polling Telegram")
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            log.error("Crash du polling, redemarrage dans 15s : %s", e)
            time.sleep(15)


if __name__ == "__main__":
    verifier_mongo()
    threading.Thread(target=lancer_serveur, daemon=True).start()
    threading.Thread(target=lancer_planificateur, daemon=True).start()
    lancer_bot()
