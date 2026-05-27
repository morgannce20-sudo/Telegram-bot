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
        infos = recherche_web("matchs tennis aujourd'hui ATP WTA 2025 programme")
        return infos
    except:
        return ""

def envoyer_pronostic_auto(match, sport):
    try:
        emoji = "🎾" if sport == "tennis" else "⚽"
        bot.send_message(CHAT_ID, f"⏰ Match dans 2h !\n{emoji} {match}\n\nAnalyse en cours...")
        infos = recherche_web(match + " stats forme 2025")
        infos += recherche_web(match + " blessures absents 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic")
        infos += recherche_web(match + " historique confrontations head to head")

        if sport == "tennis":
            prompt = f"""Tu es un expert en paris sportifs tennis en 2025.
Infos recentes: {infos}

Format pour {match}:

🎾 MATCH TENNIS

📊 FORME ACTUELLE
[derniers matchs et resultats]

👤 ANALYSE DES JOUEURS
[caracteristiques, points forts/faibles]

🏟️ SURFACE ET CONDITIONS
[avantage surface pour chaque joueur]

📰 NEWS IMPORTANTES
[blessures, forme physique, mental]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE PROBABLE: X-X sets

📉 PARIS CONSEILLES
Plus de 3 sets: XX%
Moins de 3 sets: XX%

💰 RECOMMANDATION FINALE
Pari: [...]
Confiance: XX% ⭐⭐⭐"""
        else:
            prompt = f"""Tu es un expert en paris sportifs football en 2025.
Mbappe joue au Real Madrid pas au PSG.
Messi joue a l Inter Miami.
Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels.

Infos recentes: {infos}

Format pour {match}:

⚽ MATCH FOOTBALL

📊 FORME ACTUELLE
[derniers 5 matchs]

👥 JOUEURS CLES ACTUELS
[joueurs importants actuels]

🚑 BLESSURES ET ABSENCES
[absents confirmes]

📰 NEWS IMPORTANTES
[polemiques, vie privee, facteurs psychologiques]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Match nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE PROBABLE: X-X

⚽ BUTEURS PROBABLES
[Joueur] - XX%

📉 NOMBRE DE BUTS
Plus de 2.5: XX%
Moins de 2.5: XX%

💰 RECOMMANDATION FINALE
Pari: [...]
Confiance: XX% ⭐⭐⭐"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur: {str(e)}")

def verifier_matchs():
    matchs = get_matchs_foot()
    now = datetime.now()
    for m in matchs:
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(
                year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=2)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_auto(m["match"], "foot")
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

def detecter_sport(texte):
    texte = texte.lower()
    if any(mot in texte for mot in ["tennis", "atp", "wta", "set", "roland", "wimbledon"]):
        return "tennis"
    return "foot"

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Bonjour! Je suis ton expert en paris sportifs!\n\nSports disponibles:\n⚽ Football\n🎾 Tennis\n\nDemande-moi un pronostic!")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    try:
        sport = detecter_sport(message.text)
        emoji = "🎾" if sport == "tennis" else "⚽"
        bot.reply_to(message, f"{emoji} Analyse en cours...")
        infos = recherche_web(message.text + " stats forme 2025")
        infos += recherche_web(message.text + " blessures absents 2025")
        infos += recherche_web(message.text + " cotes bookmakers pronostic")
        infos += recherche_web(message.text + " historique confrontations")

        if sport == "tennis":
            prompt = f"""Tu es un expert tennis en 2025.
Infos: {infos}

Format pour {message.text}:

🎾 MATCH TENNIS

📊 FORME ACTUELLE
[derniers matchs]

👤 ANALYSE DES JOUEURS
[points forts/faibles]

🏟️ SURFACE ET CONDITIONS
[avantage surface]

📰 NEWS IMPORTANTES
[blessures, forme physique]

📈 PROBABILITES
🔵 [Joueur1]: XX% 🟩🟩🟩⬜⬜
🔴 [Joueur2]: XX% 🟩🟩⬜⬜⬜

🎯 SCORE PROBABLE: X-X sets

📉 PARIS CONSEILLES
Plus de 3 sets: XX%
Moins de 3 sets: XX%

💰 RECOMMANDATION FINALE
Pari: [...]
Confiance: XX% ⭐⭐⭐"""
        else:
            prompt = f"""Tu es un expert football en 2025.
Mbappe joue au Real Madrid.
Messi joue a l Inter Miami.
Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels.

Infos: {infos}

Format pour {message.text}:

⚽ MATCH FOOTBALL

📊 FORME ACTUELLE
[derniers 5 matchs]

👥 JOUEURS CLES ACTUELS
[joueurs importants]

🚑 BLESSURES ET ABSENCES
[absents confirmes]

📰 NEWS IMPORTANTES
[polemiques, vie privee]

📈 PROBABILITES
🔵 [Equipe1]: XX% 🟩🟩🟩⬜⬜
🔴 [Equipe2]: XX% 🟩🟩⬜⬜⬜
⚪ Match nul: XX% 🟩⬜⬜⬜⬜

🎯 SCORE PROBABLE: X-X

⚽ BUTEURS PROBABLES
[Joueur] - XX%

📉 NOMBRE DE BUTS
Plus de 2.5: XX%
Moins de 2.5: XX%

💰 RECOMMANDATION FINALE
Pari: [...]
Confiance: XX% ⭐⭐⭐"""

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
