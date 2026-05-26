import os
import telebot
import threading
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

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
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": f"Tu es un expert en paris sportifs. Reponds en francais : {message.text}"}],
            model="llama3-8b-8192",
        )
        bot.reply_to(message, chat.choices[0].message.content)
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

threading.Thread(target=run_server, daemon=True).start()
bot.polling(none_stop=True)
