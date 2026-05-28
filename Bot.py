import os
import telebot
import threading
import requests
import schedule
import time
import logging
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY   = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY   = os.environ.get("TENNIS_API_KEY")
CHAT_ID          = os.environ.get("CHAT_ID", "8449749928")

for var_name, var_value in [
    ("TELEGRAM_TOKEN",   TELEGRAM_TOKEN),
    ("GROQ_API_KEY",     GROQ_API_KEY),
    ("SEARCH_API_KEY",   SEARCH_API_KEY),
    ("FOOTBALL_API_KEY", FOOTBALL_API_KEY),
    ("TENNIS_API_KEY",   TENNIS_API_KEY),
]:
    if not var_value:
        log.warning(f"Variable d'environnement manquante : {var_name}")

client = Groq(api_key=GROQ_API_KEY)
bot    = telebot.TeleBot(TELEGRAM_TOKEN)

FOOTBALL_LEAGUES = "39,140,135,78,61,2,3"
FOOTBALL_SEASON  = "2024"


# ─────────────────────────────────────────────
#  UTILITAIRES
# ─────────────────────────────────────────────
def recherche_web(query: str) -> str:
    try:
        response = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SEARCH_API_KEY, "num": 5, "hl": "fr"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return "\n".join(
            f"{r.get('title','')} - {r.get('snippet','')}"
            for r in data.get("organic_results", [])[:5]
        )
    except requests.RequestException as e:
        log.error(f"Erreur recherche web ({query[:40]}…): {e}")
        return ""


def detecter_sport(texte: str) -> str:
    try:
        chat = client.chat.completions.create(
            messages=[{
                "role": "user",
                "content": (
                    "Est-ce que ce texte parle de tennis ou de football ? "
                    "Réponds uniquement par 'tennis' ou 'foot' : " + texte
                ),
            }],
            model="llama-3.3-70b-versatile",
            max_tokens=10,
        )
        reponse = chat.choices[0].message.content.lower().strip()
        return "tennis" if "tennis" in reponse else "foot"
    except Exception as e:
        log.error(f"Erreur détection sport: {e}")
        texte_lower = texte.lower()
        if any(m in texte_lower for m in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"


def envoyer_message(chat_id: str, texte: str) -> None:
    try:
        for i in range(0, len(texte), 4000):
            bot.send_message(chat_id, texte[i:i + 4000])
    except telebot.apihelper.ApiException as e:
        log.error(f"Erreur envoi Telegram: {e}")


# ─────────────────────────────────────────────
#  RÉCUPÉRATION DES MATCHS
# ─────────────────────────────────────────────
def get_matchs_foot() -> list:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        response = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params={"date": today, "league": FOOTBALL_LEAGUES, "season": FOOTBALL_SEASON},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        matchs = []
        for match in data.get("response", []):
            matchs.append({
                "heure": match["fixture"]["date"][11:16],
                "match": f"{match['teams']['home']['name']} vs {match['teams']['away']['name']}",
                "ligue": match["league"]["name"],
                "sport": "foot",
            })
        log.info(f"{len(matchs)} match(s) foot trouvé(s) pour aujourd'hui")
        return matchs
    except Exception as e:
        log.error(f"Erreur get_matchs_foot: {e}")
        return []


def get_matchs_tennis() -> list:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        response = requests.get(
            "https://v1.tennis.api-sports.io/games",
            headers={"x-apisports-key": TENNIS_API_KEY},
            params={"date": today},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        matchs = []
        for match in data.get("response", []):
            try:
                matchs.append({
                    "heure":   match["date"][11:16],
                    "match":   f"{match['players']['home']['name']} vs {match['players']['away']['name']}",
                    "tournoi": match.get("tournament", {}).get("name", "Tournoi inconnu"),
                    "surface": match.get("surface", "inconnu"),
                    "sport":   "tennis",
                })
            except KeyError:
                pass
        log.info(f"{len(matchs)} match(s) tennis trouvé(s) pour aujourd'hui")
        return matchs
    except Exception as e:
        log.error(f"Erreur get_matchs_tennis: {e}")
        return []


# ─────────────────────────────────────────────
#  GÉNÉRATION DES PRONOSTICS
# ─────────────────────────────────────────────
def envoyer_pronostic_tennis(match: str, tournoi: str = "", surface: str = "", chat_id: str = None) -> None:
    target = chat_id or CHAT_ID
    envoyer_message(target, f"🎾 Match tennis dans 1h !\n{match}\nTournoi : {tournoi} | Surface : {surface}\n⏳ Analyse en cours…")

    infos  = recherche_web(f"{match} tennis stats forme recente 2025")
    infos += recherche_web(f"{match} head to head historique surface {surface}")
    infos += recherche_web(f"{match} blessure actualite 2025")
    infos += recherche_web(f"{match} classement ATP WTA 2025")
    infos += recherche_web(f"{match} pronostic cote bookmaker 2025")

    prompt = f"""Tu es un expert TENNIS en 2025. Analyse ce match avec des DONNÉES RÉELLES.
Ces personnes sont des JOUEURS DE TENNIS professionnels. Ne parle JAMAIS de football.

Infos collectées sur le web :
{infos}

MATCH : {match}
TOURNOI : {tournoi}
SURFACE : {surface}

Fournis une analyse complète et structurée exactement comme ceci :

🎾 MATCH : {match}
🏆 Tournoi : {tournoi} | Surface : {surface}

📊 FORME RÉCENTE (5 derniers matchs) :
→ [Joueur1] : [W-L] | Derniers résultats avec scores exacts
→ [Joueur2] : [W-L] | Derniers résultats avec scores exacts

🌍 SURFACE :
→ Stats sur {surface} cette saison pour chaque joueur (victoires/défaites)
→ Avantage : [Joueur qui a l'avantage sur cette surface et pourquoi]

👥 HEAD TO HEAD :
→ Historique global : [X-Y]
→ Sur {surface} : [X-Y]
→ Dernier match : [date + score]

🚑 BLESSURES & FORME PHYSIQUE :
→ [Joueur1] : [état physique confirmé ou "RAS"]
→ [Joueur2] : [état physique confirmé ou "RAS"]

📰 NEWS IMPORTANTES :
→ [Infos récentes importantes pour ce match]

📈 PROBABILITÉS :
→ [Joueur1] : XX%
→ [Joueur2] : XX%

🎯 SCORE PRÉDIT : X-X sets

⚽ STATS CLÉS :
→ [Joueur1] : XX% de 1er service, XX% de break
→ [Joueur2] : XX% de 1er service, XX% de break

💡 ANALYSE TACTIQUE :
→ Style de jeu de chaque joueur et comment ils s'affrontent sur cette surface

💰 RECOMMANDATION FINALE :
→ Pari conseillé : [VAINQUEUR ou SETS ou HANDICAP]
→ Cote estimée : [X.XX]
→ Niveau de confiance : XX% ⭐⭐⭐

⚠️ Utilise UNIQUEMENT des données réelles et vérifiées. Indique si une info est incertaine."""

    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        envoyer_message(target, chat.choices[0].message.content)
    except Exception as e:
        log.error(f"Erreur Groq tennis: {e}")
        envoyer_message(target, f"❌ Erreur lors de l'analyse tennis : {e}")


def envoyer_pronostic_foot(match: str, ligue: str = "", chat_id: str = None) -> None:
    target = chat_id or CHAT_ID
    envoyer_message(target, f"⚽ Match foot dans 2h !\n{match}\nCompétition : {ligue}\n⏳ Analyse en cours…")

    infos  = recherche_web(f"{match} stats forme composition équipe 2025")
    infos += recherche_web(f"{match} blessures absents suspendus 2025")
    infos += recherche_web(f"{match} cotes bookmakers pronostic 2025")
    infos += recherche_web(f"{match} historique confrontations head to head")
    infos += recherche_web(f"{match} classement {ligue} 2025")
    infos += recherche_web(f"{match} buteurs forme récente 2025")

    prompt = f"""Tu es un expert FOOTBALL en 2025. Analyse ce match avec des DONNÉES RÉELLES.
Utilise UNIQUEMENT les joueurs actuellement dans ces clubs en 2025.
Mbappé joue au Real Madrid. Messi joue à l'Inter Miami. Neymar ne joue plus au PSG.

Infos collectées sur le web :
{infos}

MATCH : {match}
COMPÉTITION : {ligue}

Fournis une analyse complète et structurée exactement comme ceci :

⚽ MATCH : {match}
🏆 Compétition : {ligue}

📊 FORME ACTUELLE (5 derniers matchs) :
→ [Equipe1] : [W-D-L] | Buts marqués : X | Buts encaissés : X | Série actuelle
→ [Equipe2] : [W-D-L] | Buts marqués : X | Buts encaissés : X | Série actuelle

🏠 DOMICILE / EXTÉRIEUR :
→ [Equipe1] à domicile cette saison : [W-D-L]
→ [Equipe2] à l'extérieur cette saison : [W-D-L]

👥 JOUEURS CLÉS (noms réels 2025) :
→ [Equipe1] : [Nom joueur] - [Stat précise : X buts / X passes en N matchs]
→ [Equipe2] : [Nom joueur] - [Stat précise : X buts / X passes en N matchs]

🚑 BLESSURES & ABSENCES :
→ [Equipe1] : [Noms confirmés ou "Aucune absence confirmée"]
→ [Equipe2] : [Noms confirmés ou "Aucune absence confirmée"]

🔄 HEAD TO HEAD :
→ Historique global : [X victoires E1 / X nuls / X victoires E2]
→ Dernier match : [date + score]

📰 NEWS IMPORTANTES :
→ [Infos récentes qui impactent le match : motivation, contexte, etc.]

📈 PROBABILITÉS :
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
→ Les deux équipes marquent (BTTS) : XX%

💡 ANALYSE TACTIQUE :
→ Système de jeu de chaque équipe et avantages/faiblesses

💰 RECOMMANDATION FINALE :
→ Pari conseillé : [PARI PRÉCIS]
→ Cote estimée : [X.XX]
→ Niveau de confiance : XX% ⭐⭐⭐

⚠️ Utilise UNIQUEMENT des données réelles et vérifiées. Indique si une info est incertaine."""

    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        envoyer_message(target, chat.choices[0].message.content)
    except Exception as e:
        log.error(f"Erreur Groq foot: {e}")
        envoyer_message(target, f"❌ Erreur lors de l'analyse foot : {e}")


# ─────────────────────────────────────────────
#  SCHEDULER
# ─────────────────────────────────────────────
def verifier_matchs() -> None:
    maintenant    = datetime.now()
    heure_dans_2h = (maintenant + timedelta(hours=2)).strftime("%H:%M")
    heure_dans_1h = (maintenant + timedelta(hours=1)).strftime("%H:%M")

    for match in get_matchs_foot():
        if match["heure"] == heure_dans_2h:
            envoyer_pronostic_foot(match["match"], match.get("ligue", ""))

    for match in get_matchs_tennis():
        if match["heure"] == heure_dans_1h:
            envoyer_pronostic_tennis(
                match["match"],
                match.get("tournoi", ""),
                match.get("surface", ""),
            )


# ─────────────────────────────────────────────
#  COMMANDES TELEGRAM
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(message):
    texte = (
        "👋 Bot de pronostics actif !\n\n"
        "📋 Commandes disponibles :\n"
        "/matchs — Voir les matchs du jour\n"
        "/foot [match] — Analyser un match foot\n"
        "/tennis [match] — Analyser un match tennis\n"
        "/aide — Afficher cette aide\n\n"
        "✉️ Tu peux aussi m'envoyer directement le nom d'un match !"
    )
    bot.reply_to(message, texte)


@bot.message_handler(commands=["aide"])
def cmd_aide(message):
    cmd_start(message)


@bot.message_handler(commands=["matchs"])
def cmd_matchs(message):
    matchs_foot   = get_matchs_foot()
    matchs_tennis = get_matchs_tennis()

    if not matchs_foot and not matchs_tennis:
        bot.reply_to(message, "😕 Aucun match trouvé pour aujourd'hui.")
        return

    lignes = ["📅 Matchs du jour :\n"]
    if matchs_foot:
        lignes.append("⚽ FOOTBALL :")
        for m in matchs_foot:
            lignes.append(f"  {m['heure']} — {m['match']} ({m['ligue']})")
    if matchs_tennis:
        lignes.append("\n🎾 TENNIS :")
        for m in matchs_tennis:
            lignes.append(f"  {m['heure']} — {m['match']} ({m['tournoi']})")

    bot.reply_to(message, "\n".join(lignes))


@bot.message_handler(commands=["foot"])
def cmd_foot(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage : /foot Equipe1 vs Equipe2")
        return
    envoyer_pronostic_foot(parts[1], chat_id=str(message.chat.id))


@bot.message_handler(commands=["tennis"])
def cmd_tennis(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage : /tennis Joueur1 vs Joueur2")
        return
    envoyer_pronostic_tennis(parts[1], chat_id=str(message.chat.id))


@bot.message_handler(func=lambda m: True)
def analyser_message(message):
    texte = message.text.strip()
    if not texte:
        return
    sport = detecter_sport(texte)
    if sport == "tennis":
        envoyer_pronostic_tennis(texte, chat_id=str(message.chat.id))
    else:
        envoyer_pronostic_foot(texte, chat_id=str(message.chat.id))


# ─────────────────────────────────────────────
#  HEALTH CHECK
# ─────────────────────────────────────────────
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_scheduler():
    schedule.every(1).minutes.do(verifier_matchs)
    while True:
        schedule.run_pending()
        time.sleep(30)


# ─────────────────────────────────────────────
#  POINT D'ENTRÉE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage du bot de pronostics…")
    threading.Thread(target=run_scheduler, daemon=True).start()
    server = HTTPServer(("0.0.0.0", 8080), HealthCheck)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health check actif sur le port 8080")
    log.info("Bot démarré ! En attente de messages…")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
