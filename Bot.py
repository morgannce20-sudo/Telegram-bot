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
def get_bankroll():
    try:
        doc = collection_bankroll.find_one({"id": "bankroll"})
        if doc:
            return doc["capital"]
        collection_bankroll.insert_one({"id": "bankroll", "capital": CAPITAL_INITIAL})
        return CAPITAL_INITIAL
    except PyMongoError as e:
        log.error("get_bankroll : %s", e)
        return CAPITAL_INITIAL


def update_bankroll(nouveau_capital):
    try:
        collection_bankroll.update_one(
            {"id": "bankroll"}, {"$set": {"capital": nouveau_capital}}, upsert=True
        )
    except PyMongoError as e:
        log.error("update_bankroll : %s", e)


def calculer_mise(confiance, cote, bankroll):
    """Kelly fractionne base sur la cote reelle, plafonne entre 2% et 10%."""
    p = confiance / 100.0
    b = max(cote - 1, 0.01)
    kelly = (b * p - (1 - p)) / b
    kelly = max(0.02, min(kelly, 0.10))
    return round(bankroll * kelly, 2)


def sauvegarder_pari(match, sport, pari, confiance, mise, cote):
    try:
        collection_paris.insert_one({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "match": match,
            "sport": sport,
            "pari": pari,
            "confiance": confiance,
            "mise": mise,
            "cote": cote,
            "resultat": "en attente",
            "gain": 0,
        })
    except PyMongoError as e:
        log.error("sauvegarder_pari : %s", e)


def get_statistiques():
    try:
        paris = list(collection_paris.find({"resultat": {"$ne": "en attente"}}))
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
        f"Bankroll actuelle : {get_bankroll():.2f}"
    )


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


def analyser_match(match, sport):
    infos = recherche_web(f"{match} pronostic statistiques {sport}")
    if sport == "tennis":
        prompt = (
            f"Tu es un analyste tennis expert. Analyse ce match : {match}\n"
            f"Informations recentes trouvees : {infos}\n\n"
            "Produis une analyse detaillee et structuree EXACTEMENT dans ce format, "
            "avec les emojis, en francais :\n\n"
            "🎾 MATCH : [Joueur 1] vs [Joueur 2]\n"
            "🏆 Tournoi : [nom] | Surface : [dur/terre/gazon]\n\n"
            "📊 FORME RECENTE (5 derniers matchs) :\n"
            "→ [Joueur 1] : [bilan] | Derniers resultats : [scores]\n"
            "→ [Joueur 2] : [bilan] | Derniers resultats : [scores]\n\n"
            "🎯 SURFACE :\n"
            "→ Stats des deux joueurs sur cette surface\n\n"
            "🤕 BLESSURES & FORME PHYSIQUE :\n"
            "→ Etat physique de chaque joueur\n\n"
            "🔄 HEAD TO HEAD :\n"
            "→ Historique des confrontations + dernier match\n\n"
            "📈 PROBABILITES :\n"
            "→ [Joueur 1] : [%]\n"
            "→ [Joueur 2] : [%]\n\n"
            "💡 ANALYSE TACTIQUE :\n"
            "→ Style de jeu et points cles\n\n"
            "Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :\n"
            "PARI: [ton pronostic]\n"
            "CONFIANCE: [pourcentage entre 50 et 95]\n"
            "COTE: [cote estimee, ex 1.85]\n\n"
            "Sois precis et realiste. Si tu n'es pas sur d'une donnee, reste prudent."
        )
    else:
        prompt = (
            f"Tu es un analyste football expert. Analyse ce match : {match}\n"
            f"Informations recentes trouvees : {infos}\n\n"
            "Produis une analyse detaillee et structuree EXACTEMENT dans ce format, "
            "avec les emojis, en francais :\n\n"
            "⚽ MATCH : [Equipe 1] - [Equipe 2]\n"
            "🏆 Competition : [nom]\n\n"
            "📊 FORME ACTUELLE (5 derniers matchs) :\n"
            "→ [Equipe 1] : [V-N-D] | Buts marques : [x] | Buts encaisses : [x] | Serie : [...]\n"
            "→ [Equipe 2] : [V-N-D] | Buts marques : [x] | Buts encaisses : [x] | Serie : [...]\n\n"
            "🏠 DOMICILE / EXTERIEUR :\n"
            "→ [Equipe 1] a domicile cette saison : [V-N-D]\n"
            "→ [Equipe 2] a l'exterieur cette saison : [V-N-D]\n\n"
            "👥 JOUEURS CLES :\n"
            "→ [Equipe 1] : [joueur - stats]\n"
            "→ [Equipe 2] : [joueur - stats]\n\n"
            "🚑 BLESSURES & ABSENCES :\n"
            "→ [Equipe 1] : [liste]\n"
            "→ [Equipe 2] : [liste]\n\n"
            "🔄 HEAD TO HEAD :\n"
            "→ Historique global + dernier match\n\n"
            "📈 PROBABILITES :\n"
            "→ [Equipe 1] : [%]\n"
            "→ Match nul : [%]\n"
            "→ [Equipe 2] : [%]\n\n"
            "🎯 SCORE PROBABLE : [x-x]\n\n"
            "⚽ BUTEURS PROBABLES :\n"
            "→ [Equipe 1] : [joueur (% - stats)]\n"
            "→ [Equipe 2] : [joueur (% - stats)]\n\n"
            "🔢 NOMBRE DE BUTS :\n"
            "→ Plus de 2.5 : [%]\n"
            "→ Moins de 2.5 : [%]\n"
            "→ Les deux equipes marquent (BTTS) : [%]\n\n"
            "💡 ANALYSE TACTIQUE :\n"
            "→ Systemes de jeu et points cles\n\n"
            "Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :\n"
            "PARI: [ton pronostic]\n"
            "CONFIANCE: [pourcentage entre 50 et 95]\n"
            "COTE: [cote estimee, ex 1.85]\n\n"
            "Sois precis et realiste. Si tu n'es pas sur d'une donnee, reste prudent."
        )
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
    try:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d"),
                    "league": "39,140,135,78,61,2,3", "season": SAISON_FOOT},
            timeout=10,
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
    bot.reply_to(
        message,
        "Bot de pronostics actif.\n"
        "Envoie un match a analyser, ou utilise :\n"
        "/matchs - matchs du jour\n"
        "/stats - statistiques\n"
        "/bankroll - capital actuel",
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    bot.reply_to(message, get_statistiques())


@bot.message_handler(commands=["bankroll"])
def cmd_bankroll(message):
    bot.reply_to(message, f"Bankroll actuelle : {get_bankroll():.2f}")


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
    attente = bot.reply_to(message, "Analyse en cours...")
    analyse = analyser_match(texte, sport)
    pari, confiance, cote = parser_analyse(analyse)
    bankroll = get_bankroll()
    mise = calculer_mise(confiance, cote, bankroll)
    sauvegarder_pari(texte, sport, pari, confiance, mise, cote)

    reponse = (
        f"{analyse}\n\n"
        f"💰 Mise conseillee : {mise:.2f} (bankroll : {bankroll:.2f})\n\n"
        "⚠️ Analyse generee par IA a titre indicatif. Certaines donnees "
        "peuvent etre incertaines ou non a jour. Verifie toujours avant de parier."
    )

    # Telegram limite a 4096 caracteres : on decoupe si besoin
    for i in range(0, len(reponse), 4000):
        bot.send_message(message.chat.id, reponse[i:i + 4000])


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
    lancer_bot()
