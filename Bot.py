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
            matchs.append({"heure": heure, "match": equipe1 + " vs " + equipe2})
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
            matchs.append({"heure": heure, "match": joueur1 + " vs " + joueur2, "tournoi": tournoi})
        return matchs
    except:
        return []

def envoyer_pronostic_auto(match):
    try:
        bot.send_message(CHAT_ID, "Match dans 2h !\n" + match + "\n\nAnalyse en cours...")
        infos = recherche_web(match + " stats forme composition 2025")
        infos += recherche_web(match + " blessures absents actualite 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic")
        infos += recherche_web(match + " historique confrontations")

        prompt = (
            "Tu es un expert en paris sportifs en 2025. Analyse et donne un pronostic en francais pour : " + match + "\n"
            "Mbappe joue au Real Madrid. Messi joue a l Inter Miami. Neymar ne joue plus au PSG.\n"
            "Utilise UNIQUEMENT les joueurs actuels.\n\n"
            "Infos recentes:\n" + infos + "\n\n"
            "Format de reponse:\n"
            "MATCH : " + match + "\n\n"
            "FORME ACTUELLE\n"
            "[analyse forme des 5 derniers matchs]\n\n"
            "JOUEURS CLES\n"
            "[joueurs importants ACTUELS]\n\n"
            "BLESSURES ET ABSENCES\n"
            "[absents confirmes]\n\n"
            "NEWS IMPORTANTES\n"
            "[actualites recentes]\n\n"
            "PROBABILITES\n"
            "Equipe1: XX%\n"
            "Equipe2: XX%\n"
            "Match nul: XX%\n\n"
            "SCORE PROBABLE: X - X\n\n"
            "BUTEURS PROBABLES\n"
            "Joueur1 - XX% de marquer\n"
            "Joueur2 - XX% de marquer\n\n"
            "NOMBRE DE BUTS\n"
            "Plus de 2.5: XX%\n"
            "Moins de 2.5: XX%\n\n"
            "RECOMMANDATION FINALE\n"
            "Pari conseille: [...]\n"
            "Niveau de confiance: XX%"
        )

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, "Erreur pronostic foot: " + str(e))

def envoyer_pronostic_tennis(match, tournoi):
    try:
        bot.send_message(CHAT_ID, "Match tennis dans 2h !\n" + match + "\n" + tournoi + "\n\nAnalyse en cours...")
        infos = recherche_web(match + " stats forme 2025")
        infos += recherche_web(match + " blessures historique 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic tennis")

        prompt = (
            "Tu es un expert en paris sportifs tennis en 2025. Donne un pronostic en francais pour : " + match + " (" + tournoi + ")\n\n"
            "Infos recentes:\n" + infos + "\n\n"
            "Format de reponse:\n"
            "MATCH : " + match + "\n"
            "TOURNOI : " + tournoi + "\n\n"
            "FORME ACTUELLE\n"
            "[analyse forme des 5 derniers matchs]\n\n"
            "STYLE DE JEU\n"
            "[analyse style et surface favorite]\n\n"
            "BLESSURES ET CONDITION PHYSIQUE\n"
            "[etat physique]\n\n"
            "NEWS IMPORTANTES\n"
            "[actualites recentes]\n\n"
            "PROBABILITES\n"
            "Joueur1: XX%\n"
            "Joueur2: XX%\n\n"
            "SCORE PROBABLE: X-X X-X\n\n"
            "NOMBRE DE SETS\n"
            "Plus de 2.5 sets: XX%\n"
            "Moins de 2.5 sets: XX%\n\n"
            "RECOMMANDATION FINALE\n"
            "Pari conseille: [...]\n"
            "Niveau de confiance: XX%"
        )

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
        )
        bot.send_message(CHAT_ID, chat.choices[0].message.content)
    except Exception as e:
        bot.send_message(CHAT_ID, "Erreur pronostic tennis: " + str(e))

def verifier_matchs():
    now = datetime.now()
    for m in get_matchs_du_jour():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
            heure_envoi = heure_match - timedelta(hours=2)
            if abs((heure_envoi - now).total_seconds()) < 60:
                envoyer_pronostic_auto(m["match"])
        except:
            pass
    for m in get_matchs_tennis():
        try:
            heure_match = datetime.strptime(m["heure"], "%H:%M").replace(year=now.year, month=now.month, day=now.day)
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
        self.send_header("Content-type", "text/plain")
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
        bot.reply_to(message, "Acces refuse.")
        return
    bot.reply_to(message, "Bonjour! Je suis ton expert en paris sportifs!\n\nDemande-moi un pronostic foot ou tennis!\nOu attends mes analyses automatiques 2h avant chaque match!")

@bot.message_handler(func=lambda m: True)
def repondre(message):
    if str(message.chat.id) not in ADMINS:
        bot.reply_to(message, "Acces refuse.")
        return
    try:
        bot.reply_to(message, "Recherche en cours...")
        texte = message.text.lower()
        mots_tennis = ["tennis", "atp", "wta", "set", "ace", "roland", "wimbledon", "open"]
        is_tennis = any(mot in texte for mot in mots_tennis)

        if is_tennis:
            infos = recherche_web(message.text + " stats forme 2025")
            infos += recherche_web(message.text + " blessures historique 2025")
            infos += recherche_web(message.text + " cotes bookmakers pronostic tennis")
            prompt = (
                "Tu es un expert en paris sportifs tennis en 2025. Donne un pronostic detaille en francais pour : " + message.text + "\n\n"
                "Infos recentes:\n" + infos + "\n\n"
                "Format de reponse:\n"
                "MATCH : Joueur1 - Joueur2\n"
                "TOURNOI : ...\n\n"
                "FORME ACTUELLE\n[analyse forme]\n\n"
                "STYLE DE JEU\n[style et surface]\n\n"
                "BLESSURES\n[etat physique]\n\n"
                "NEWS\n[actualites]\n\n"
                "PROBABILITES\nJoueur1: XX%\nJoueur2: XX%\n\n"
                "SCORE PROBABLE: X-X X-X\n\n"
                "NOMBRE DE SETS\nPlus de 2.5: XX%\nMoins de 2.5: XX%\n\n"
                "RECOMMANDATION FINALE\nPari conseille: [...]\nNiveau de confiance: XX%"
            )
        else:
            infos = recherche_web(message.text + " stats forme composition 2025")
            infos += recherche_web(message.text + " blessures absents actualite 2025")
            infos += recherche_web(message.text + " cotes bookmakers pronostic")
            infos += recherche_web(message.text + " historique confrontations")
            prompt = (
                "Tu es un expert en paris sportifs en 2025. Donne un pronostic detaille en francais pour : " + message.text + "\n"
                "Mbappe joue au Real Madrid. Messi joue a l 
