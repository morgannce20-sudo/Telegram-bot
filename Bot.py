import os
import telebot
import threading
import requests
import schedule
import time
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
CHAT_ID = "8449749928"
ADMINS = ["8449749928", "ID_2EME_COMPTE"]

client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

def recherche_web(query):
    try:
        url = "https://serpapi.com/search"
        params = {"q": query, "api_key": SEARCH_API_KEY, "num": 5, "hl": "fr"}
        response = requests.get(url, params=params)
        data = response.json()
        resultats = ""
        for r in data.get("organic_results", [])[:5]:
            resultats += r.get("title", "") + " - " + r.get("snippet", "") + "\n"
        return resultats
    except:
        return ""

def get_matchs_du_jour():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = "https://v3.football.api-sports.io/fixtures"
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        params = {"date": today, "league": "39,140,135,78,61,2,3", "season": "2024"}
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        matchs = []
        for match in data.get("response", []):
            heure = match["fixture"]["date"][11:16]
            equipe1 = match["teams"]["home"]["name"]
            equipe2 = match["teams"]["away"]["name"]
            matchs.append({"heure": heure, "match": f"{equipe1} vs {equipe2}"})
        return matchs
    except:
        return []

def get_matchs_tennis():
    try:
        url = "https://v1.tennis.api-sports.io/games"
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        today = datetime.now().strftime("%Y-%m-%d")
        params = {"date": today}
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        matchs = []
        for match in data.get("response", []):
            heure = match["date"][11:16]
            joueur1 = match["players"]["home"]["name"]
            joueur2 = match["players"]["away"]["name"]
            tournoi = match.get("tournament", {}).get("name", "")
            matchs.append({
                "heure": heure,
                "match": f"{joueur1} vs {joueur2}",
                "tournoi": tournoi
            })
        return matchs
    except:
        return []

def envoyer_pronostic_auto(match):
    try:
        bot.send_message(CHAT_ID, f"⏰ Match dans 2h !\n⚽ {match}\n\nAnalyse en cours... 🔍")
        infos = recherche_web(match + " stats forme composition 2025")
        infos += recherche_web(match + " blessures absents actualite 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic")
        infos += recherche_web(match + " historique confrontations")

        prompt = f"""Tu es un expert en paris sportifs en 2025. Analyse ces infos et donne un pronostic en francais pour : {match}
Mbappé a quitte le PSG en 2024 pour le Real Madrid.
Messi joue a l'Inter Miami, pas au PSG.
Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels des clubs.

Infos recentes:
{infos}

Format de reponse:
⚽ {match}

📊 FORME ACTUELLE
[analyse forme des 5 derniers matchs]

👥 JOUEURS CLES
[joueurs importants ACTUELS avec leur forme]

🚑 BLESSURES ET ABSENCES
[liste des absents confirmes]

📰 NEWS IMPORTANTES
[polémiques, vie privée, facteurs psychologiques]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Match nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE PROBABLE: X - X

⚽ BUTEURS PROBABLES
[Joueur1] - XX% de marquer
[Joueur2] - XX% de marquer

📉 NOMBRE DE BUTS
Plus de 2.5: XX%
Moins de 2.5: XX%

💰 RECOMMANDATION FINALE
Pari conseille: [...]
Niveau de confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur pronostic foot: {str(e)}")

def envoyer_pronostic_tennis(match, tournoi):
    try:
        bot.send_message(CHAT_ID, f"⏰ Match tennis dans 2h !\n🎾 {match}\n🏆 {tournoi}\n\nAnalyse en cours... 🔍")
        infos = recherche_web(match + " stats forme 2025")
        infos += recherche_web(match + " blessures historique 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic tennis")

        prompt = f"""Tu es un expert en paris sportifs tennis en 2025. Analyse et donne un pronostic en francais pour : {match} ({tournoi})

Infos recentes:
{infos}

Format de reponse:
🎾 {match}
🏆 {tournoi}

📊 FORME ACTUELLE
[analyse forme des 5 derniers matchs de chaque joueur]

🎯 STYLE DE JEU
[analyse style et surface favorite]

🚑 BLESSURES ET CONDITION PHYSIQUE
[état physique des joueurs]

📰 NEWS IMPORTANTES
[actualités récentes]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE PROBABLE: X-X X-X

📉 NOMBRE DE SETS
Plus de 2.5 sets: XX%
Moins de 2.5 sets: XX%

💰 RECOMMANDATION FINALE
Pari conseillé: [...]
Niveau de confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur pronostic tennis: {str(e)}")

def verifier_matchs():
    now = datetime.now()

    # Football
    for m in get_matchs_du_jour():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=2)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_auto(m["match"])
        except:
            pass

    # Tennis
    for m in get_matchs_tennis():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=2)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_tennis(m["match"], m["tournoi"])
        except:
            pass

def scheduler():
    schedule.every().minute.do(verifier_matchs)
    while True:
        schedule.run_pending()
        time.sleep(30)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

@bot.message_handler(commands=["start"])
def start(message):
    if str(message.chat.id) not in ADMINS:
        bot.reply_to(message, "❌ Accès refusé.")
        return
    bot.reply_to(message, "Bonjour! Je suis ton expert en paris sportifs! 🏆\n\nCommandes disponibles:\n⚽ Demande un pronostic foot\n🎾 Demande un pronostic tennis\n\nOu attends mes analyses automatiques 2h avant chaque match!")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    if str(message.chat.id) not in ADMINS:
        bot.reply_to(message, "❌ Accès refusé.")
        return
    try:
        bot.reply_to(message, "Recherche en cours... 🔍")
        texte = message.text.lower()

        # Détection tennis
        mots_tennis = ["tennis", "atp", "wta", "set", "ace", "roland", "wimbledon", "open"]
        is_tennis = any(mot in texte for mot in mots_tennis)

        if is_tennis:
            infos = recherche_web(message.text + " stats forme 2025")
            infos += recherche_web(message.text + " blessures historique 2025")
            infos += recherche_web(message.text + " cotes bookmakers pronostic tennis")

            prompt = f"""Tu es un expert en paris sportifs tennis en 2025. Donne un pronostic détaillé en francais pour : {message.text}

Infos recentes:
{infos}

Format de reponse:
🎾 MATCH : [Joueur1] - [Joueur2]
🏆 [Tournoi]

📊 FORME ACTUELLE
[analyse forme des 5 derniers matchs]

🎯 STYLE DE JEU
[analyse style et surface favorite]

🚑 BLESSURES ET CONDITION PHYSIQUE
[état physique]

📰 NEWS IMPORTANTES
[actualités récentes]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE PROBABLE: X-X X-X

📉 NOMBRE DE SETS
Plus de 2.5 sets: XX%
Moins de 2.5 sets: XX%

💰 RECOMMANDATION FINALE
Pari conseillé: [...]
Niveau de confiance: XX% ⭐⭐⭐"""

        else:
            infos = recherche_web(message.text + " stats forme composition 2025")
            infos += recherche_web(message.text + " blessures absents actualite 2025")
            infos += recherche_web(message.text + " cotes bookmakers pronostic")
            infos += recherche_web(message.text + " historique confrontations")

            prompt = f"""Tu es un expert en paris sportifs en 2025. Donne un pronostic détaillé en francais pour : {message.text}
Mbappé joue au Real Madrid. Messi joue a l'Inter Miami. Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels.

Infos recentes:
{infos}

Format de reponse:
⚽ MATCH : [Equipe1] - [Equipe2]

📊 FORME ACTUELLE
[analyse forme des 5 derniers matchs]

👥 JOUEURS CLES
[joueurs importants ACTUELS]

🚑 BLESSURES ET ABSENCES
[absents confirmés]

📰 NEWS IMPORTANTES
[actualités récentes]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Match nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE PROBABLE: X - X

⚽ BUTEURS PROBABLES
[Joueur1] - XX% de marquer
[Joueur2] - XX% de marquer

📉 NOMBRE DE BUTS
Plus de 2.5: XX%
Moins de 2.5: XX%

💰 RECOMMAND
