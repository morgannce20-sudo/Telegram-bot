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
TENNIS_API_KEY = os.environ.get("TENNIS_API_KEY")
CHAT_ID = "8449749928"

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

def detecter_sport(texte):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": f"Est-ce que ce texte parle de tennis ou de football? Reponds uniquement par 'tennis' ou 'foot': {texte}"}],
            model="llama-3.3-70b-versatile",
        )
        reponse = chat.choices[0].message.content.lower().strip()
        if "tennis" in reponse:
            return "tennis"
        return "foot"
    except:
        texte_lower = texte.lower()
        if any(mot in texte_lower for mot in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"

def get_matchs_foot():
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
            matchs.append({"heure": heure, "match": f"{equipe1} vs {equipe2}", "sport": "foot"})
        return matchs
    except:
        return []

def get_matchs_tennis():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = "https://v1.tennis.api-sports.io/games"
        headers = {"x-apisports-key": TENNIS_API_KEY}
        params = {"date": today}
        response = requests.get(url, headers=headers, params=params)
        data = response.json()
        matchs = []
        for match in data.get("response", []):
            try:
                heure = match["date"][11:16]
                joueur1 = match["players"]["home"]["name"]
                joueur2 = match["players"]["away"]["name"]
                matchs.append({"heure": heure, "match": f"{joueur1} vs {joueur2}", "sport": "tennis"})
            except:
                pass
        return matchs
    except:
        return []

def envoyer_pronostic_tennis(match):
    try:
        bot.send_message(CHAT_ID, f"⏰ Match tennis dans 1h!\n🎾 {match}\n\nAnalyse en cours...")
        infos = recherche_web(match + " tennis stats forme 2025")
        infos += recherche_web(match + " head to head historique surface")
        infos += recherche_web(match + " blessure actualite 2025")

        prompt = f"""Tu es un expert TENNIS en 2025. Ces personnes sont des JOUEURS DE TENNIS.
Ne parle JAMAIS de football.
Infos: {infos}

🎾 {match}

📊 FORME RECENTE
[derniers matchs tennis]

🏟️ SURFACE
[avantage pour qui]

📰 NEWS
[blessures, forme physique]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE: X-X sets

💰 PARI CONSEILLE
Vainqueur: [...]
Confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur tennis: {str(e)}")

def envoyer_pronostic_foot(match):
    try:
        bot.send_message(CHAT_ID, f"⏰ Match foot dans 2h!\n⚽ {match}\n\nAnalyse en cours...")
        infos = recherche_web(match + " stats forme composition 2025")
        infos += recherche_web(match + " blessures absents 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic")
        infos += recherche_web(match + " historique confrontations")

        prompt = f"""Tu es un expert FOOTBALL en 2025.
Mbappe joue au Real Madrid.
Messi joue a l Inter Miami.
Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels.
Infos: {infos}

⚽ {match}

📊 FORME ACTUELLE
[derniers 5 matchs]

👥 JOUEURS CLES
[joueurs actuels]

🚑 ABSENCES
[absents confirmes]

📰 NEWS
[polemiques, vie privee]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE: X-X

⚽ BUTEURS
[Joueur] - XX%

📉 BUTS
Plus 2.5: XX% / Moins 2.5: XX%

💰 PARI CONSEILLE
Pari: [...] Confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur foot: {str(e)}")

def verifier_matchs():
    now = datetime.now()
    for m in get_matchs_foot():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=2)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_foot(m["match"])
        except:
            pass
    for m in get_matchs_tennis():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=1)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_tennis(m["match"])
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
    bot.reply_to(message, "Bonjour! Je suis ton expert en paris sportifs!\n\nSports:\n⚽ Football\n🎾 Tennis\n\nDemande-moi un pronostic!")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    try:
        sport = detecter_sport(message.text)
        emoji = "🎾" if sport == "tennis" else "⚽"
        bot.reply_to(message, f"{emoji} Analyse en cours...")
        infos = recherche_web(message.text + " stats forme 2025")
        infos += recherche_web(message.text + " blessures actualite 2025")
        infos += recherche_web(message.text + " cotes pronostic")
        infos += recherche_web(message.text + " historique head to head")

        if sport == "tennis":
            prompt = f"""Tu es un expert TENNIS en 2025.
Ne parle JAMAIS de football pour une question tennis.
Infos: {infos}

🎾 {message.text}

📊 FORME RECENTE
[derniers matchs tennis]

🏟️ SURFACE
[avantage pour qui]

📰 NEWS
[blessures, forme physique]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE: X-X sets

💰 PARI CONSEILLE
Vainqueur: [...]
Confiance: XX% ⭐⭐⭐"""
        else:
            prompt = f"""Tu es un expert FOOTBALL en 2025.
Mbappe joue au Real Madrid.
Messi joue a l Inter Miami.
Neymar ne joue plus au PSG.
Infos: {infos}

⚽ {message.text}

📊 FORME ACTUELLE
[derniers 5 matchs]

👥 JOUEURS CLES
[joueurs actuels]

🚑 ABSENCES
[absents confirmes]

📰 NEWS
[polemiques, vie privee]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE: X-X

⚽ BUTEURS
[Joueur] - XX%

📉 BUTS
Plus 2.5: XX% / Moins 2.5: XX%

💰 PARI CONSEILLE
Pari: [...] Confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.reply_to(message, chat.choices[0].message.content)
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

threading.Thread(target=run_server, daemon=True).start()
threading.Thread(target=scheduler, daemon=True).start()
bot.polling(none_stop=True)
