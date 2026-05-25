import telebot
import google.generativeai as genai

TELEGRAM_TOKEN = "8960918325:AAHNq2C8H_hQ8HXX64jFdpvfQ_YaA5Ac4Ew"
GEMINI_API_KEY = "ta_clé_gemini_ici"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Bonjour ! Demande-moi un pronostic sportif 🏆")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    reponse = model.generate_content(
        f"Tu es un expert en paris sportifs. Réponds en français : {message.text}"
    )
    bot.reply_to(message, reponse.text)

bot.polling()
