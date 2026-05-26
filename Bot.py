import os
import telebot
import threading
import requests
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")

client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

def get_stats_equipe(equipe):
    try:
        url = "https://v3.football.api-sports.io/teams/statistics"
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        params = {"team": equipe, "season": "2024"}
        response = requests.get(url, headers=headers, params=params)
        return str(response.json())[:500]
    except:
        return ""

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
    bot.reply_to(message, "Bonjour! Demande-moi un pronostic sportif!")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    try:
        bot.reply_to(message, "Recherche en cours... 🔍")
        infos_web = recherche_web(message.text + " stats analyse pronostic 2024 2025")
        infos_web += recherche_web(message.text + " news joueurs forme blessures")
        prompt = f"""Tu es un expert en paris sportifs. Analyse ces informations et donne un pronostic DETAILLE en francais pour : {message.text}

Informations trouvees sur le web:
{infos_web}

Donne moi:
1. Analyse des statistiques et forme actuelle des equipes
2. News importantes sur les joueurs (blessures, polémiques, vie privée)
3. Probabilite de victoire de chaque equipe en %
4. Score probable
5. Joueurs susceptibles de marquer et probabilite
6. Nombre de buts probable
7. Recommandation finale avec niveau de confiance"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.reply_to(message, chat.choices[0].message.content)
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

threading.Thread(target=run_server, daemon=True).start()
bot.polling(none_stop=True)
