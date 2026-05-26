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
        infos_web = recherche_web(message.text + " stats analyse pronostic mai 2025 effectif actuel")
        infos_web += recherche_web(message.text + " joueurs titulaires composition 2025")
        infos_web += recherche_web(message.text + " news blessures actualite 2025")
        prompt = f"""Tu es un expert en paris sportifs en 2025. Utilise UNIQUEMENT les informations recentes ci-dessous pour faire ton analyse. Ignore toute information obsolete sur des joueurs qui ont quitte les clubs.

Informations recentes trouvees:
{infos_web}

Donne un pronostic DETAILLE en francais pour : {message.text}

1. Analyse des statistiques et forme actuelle des equipes en 2025
2. Joueurs cles ACTUELS (pas Messi, Neymar ou autres partis)
3. News importantes (blessures, polémiques, vie privee des joueurs actuels)
4. Probabilite de victoire de chaque equipe en %
5. Score probable
6. Joueurs susceptibles de marquer et probabilite
7. Nombre de buts probable
8. Recommandation finale avec niveau de confiance"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.reply_to(message, chat.choices[0].message.content)
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

threading.Thread(target=run_server, daemon=True).start()
bot.polling(none_stop=True)
