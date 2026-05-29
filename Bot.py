import os
import telebot
import threading
import requests
import schedule
import time
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from pymongo import MongoClient

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY = os.environ.get("TENNIS_API_KEY")
MONGODB_URL = os.environ.get("MONGODB_URL")
CHAT_ID = "8449749928"
CAPITAL_INITIAL = 50.0

client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

mongo_client = MongoClient(MONGODB_URL)
db = mongo_client["pronostics"]
collection_paris = db["paris"]
collection_bankroll = db["bankroll"]

def get_bankroll():
    doc = collection_bankroll.find_one({"id": "bankroll"})
    if doc:
        return doc["capital"]
    collection_bankroll.insert_one({"id": "bankroll", "capital": CAPITAL_INITIAL})
    return CAPITAL_INITIAL

def update_bankroll(nouveau_capital):
    collection_bankroll.update_one({"id": "bankroll"}, {"$set": {"capital": nouveau_capital}})

def calculer_mise(confiance, bankroll):
    fraction_kelly = (confiance / 100 - (1 - confiance / 100)) / 1
    fraction_kelly = max(0.02, min(fraction_kelly, 0.1))
    return round(bankroll * fraction_kelly, 2)

def sauvegarder_pari(match, sport, pari, confiance, mise, cote):
    collection_paris.insert_one({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "match": match,
        "sport": sport,
        "pari": pari,
        "confiance": confiance,
        "mise": mise,
        "cote": cote,
        "resultat": "en attente",
        "gain": 0
    })

def get_statistiques():
    paris = list(collection_paris.find({"resultat": {"$ne": "en attente"}}))
    if not paris:
        return "Aucun pari termine pour le moment."
    total = len(paris)
    gagnes = len([p for p in paris if p["resultat"] == "gagne"])
    pertes = len([p for p in paris if p["resultat"] == "perdu"])
    gain_total = sum([p.get("gain", 0) for p in paris])
    taux = round(gagnes / total * 100, 1) if total > 0 else 0
    bankroll = get_bankroll()
    return f"""STATISTIQUES :
Total paris : {total}
Gagnes : {gagnes} | Perdus : {pertes}
Taux de reussite : {taux}%
Gain/Perte total : {gain_total:.2f}
Bankroll actuelle : {bankroll:.2f}"""

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
            ligue = match["league"]["name"]
            matchs.append({"heure": heure, "match": f"{equipe1} vs {equipe2}", "ligue": ligue})
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
                tournoi = match.get("tournament", {}).get("name", "Tournoi inconnu")
                surface = match.get("surface", "inconnu")
                matchs.append({
                    "heure": heure,
                    "match": f"{joueur1} vs {joueur2}",
                    "tournoi": tournoi,
                    "surface": surface,
                })
            except:
                pass
        return matchs
    except:
        return []

def envoyer_pronostic_tennis(match, tournoi="", surface=""):
    try:
        bankroll = get_bankroll()
        bot.send_message(CHAT_ID, f"Match tennis dans 1h!\n{match}\nTournoi : {tournoi} | Surface : {surface}\n\nAnalyse en cours...")
        infos = recherche_web(match + " tennis stats forme recente 2025")
        infos += recherche_web(match + " head to head historique surface " + surface)
        infos += recherche_web(match + " blessure actualite 2025")
        infos += recherche_web(match + " classement ATP WTA 2025")
        infos += recherche_web(match + " pronostic cote bookmaker 2025")

        prompt = f"""Tu es un expert TENNIS en 2025. Ces personnes sont des JOUEURS DE TENNIS. Ne parle JAMAIS de football.
Infos: {infos}
MATCH : {match} | TOURNOI : {tournoi} | SURFACE : {surface}

Reponds exactement dans ce format :

MATCH : {match}
Tournoi : {tournoi} | Surface : {surface}

FORME RECENTE :
[Joueur1] : derniers resultats
[Joueur2] : derniers resultats

SURFACE :
Avantage : [qui et pourquoi]

HEAD TO HEAD :
Historique : [X-Y]
Dernier match : [date + score]

BLESSURES :
[Joueur1] : [etat ou RAS]
[Joueur2] : [etat ou RAS]

NEWS : [infos importantes]

PROBABILITES :
[Joueur1] : XX%
[Joueur2] : XX%

SCORE PREDIT : X-X sets

RECOMMANDATION :
Pari : [...]
Cote estimee : [X.XX]
Confiance : XX%"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        reponse = chat.choices[0].message.content

        try:
            lignes = reponse.lower().split("\n")
            confiance = 65
            cote = 1.80
            pari = "Vainqueur"
            for ligne in lignes:
                if "confiance" in ligne:
                    import re
                    nums = re.findall(r'\d+', ligne)
                    if nums:
                        confiance = int(nums[0])
                if "cote" in ligne:
                    import re
                    nums = re.findall(r'\d+\.?\d*', ligne)
                    if nums:
                        cote = float(nums[0])
                if "pari" in ligne and ":" in ligne:
                    pari = ligne.split(":")[-1].strip()
            mise = calculer_mise(confiance, bankroll)
            sauvegarder_pari(match, "tennis", pari, confiance, mise, cote)
            reponse += f"\n\nBANKROLL : {bankroll:.2f}\nMISE CONSEILLEE : {mise:.2f} ({round(mise/bankroll*100,1)}% du capital)"
        except:
            pass

        bot.send_message(CHAT_ID, reponse)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur tennis: {str(e)}")

def envoyer_pronostic_foot(match, ligue=""):
    try:
        bankroll = get_bankroll()
        bot.send_message(CHAT_ID, f"Match foot dans 2h!\n{match}\nCompetition : {ligue}\n\nAnalyse en cours...")
        infos = recherche_web(match + " stats forme composition equipe 2025")
        infos += recherche_web(match + " blessures absents suspendus 2025")
        infos += recherche_web(match + " cotes bookmakers pronostic 2025")
        infos += recherche_web(match + " historique confrontations head to head")
        infos += recherche_web(match + " classement " + ligue + " 2025")
        infos += recherche_web(match + " buteurs forme recente 2025")

        prompt = f"""Tu es un expert FOOTBALL en 2025.
Mbappe joue au Real Madrid. Messi joue a l'Inter Miami. Neymar ne joue plus au PSG.
Utilise UNIQUEMENT les joueurs actuels.
Infos: {infos}
MATCH : {match} | COMPETITION : {ligue}

Reponds exactement dans ce format :

MATCH : {match}
Competition : {ligue}

FORME ACTUELLE :
[Equipe1] : W-D-L | Serie actuelle
[Equipe2] : W-D-L | Serie actuelle

DOMICILE/EXTERIEUR :
[Equipe1] domicile : W-D-L
[Equipe2] exterieur : W-D-L

JOUEURS CLES 2025 :
[Equipe1] : [Nom] - [stats]
[Equipe2] : [Nom] - [stats]

BLESSURES :
[Equipe1] : [noms ou aucune]
[Equipe2] : [noms ou aucune]

HEAD TO HEAD :
Historique : [X-Y-Z]
Dernier match : [date + score]

NEWS : [infos importantes]

PROBABILITES :
[Equipe1] : XX%
Match nul : XX%
[Equipe2] : XX%

SCORE PROBABLE : X-X

BUTEURS :
[Equipe1] : [Nom] XX%
[Equipe2] : [Nom] XX%

BUTS :
Plus 2.5 : XX% | Moins 2.5 : XX% | BTTS : XX%

RECOMMANDATION :
Pari : [...]
Cote estimee : [X.XX]
Confiance : XX%"""

        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        reponse = chat.choices[0].message.content

        try:
            lignes = reponse.lower().split("\n")
            confiance = 65
            cote = 1.80
            pari = "Victoire domicile"
            for ligne in lignes:
                if "confiance" in ligne:
                    import re
                    nums = re.findall(r'\d+', ligne)
                    if nums:
                        confiance = int(nums[0])
                if "cote" in ligne:
                    import re
                    nums = re.findall(r'\d+\.?\d*', ligne)
                    if nums:
                        cote = float(nums[0])
                if "pari" in ligne and ":" in ligne:
                    pari = ligne.split(":")[-1].strip()
            mise = calculer_mise(confiance, bankroll)
            sauvegarder_pari(match, "foot", pari, confiance, mise, cote)
            reponse += f"\n\nBANKROLL : {bankroll:.2f}\nMISE CONSEILLEE : {mise:.2f} ({round(mise/bankroll*100,1)}% du capital)"
        except:
            pass

        bot.send_message(CHAT_ID, reponse)
    except Exception as e:
        bot.send_message(CHAT_ID, f"Erreur foot: {str(e)}")

def verifier_matchs():
    maintenant = datetime.now()
    heure_dans_2h = (maintenant + timedelta(hours=2)).strftime("%H:%M")
    heure_dans_1h = (maintenant + timedelta(hours=1)).strftime("%H:%M")
    for match in get_matchs_foot():
        if match["heure"] == heure_dans_2h:
            envoyer_pronostic_foot(match["match"], match.get("ligue", ""))
    for match in get_matchs_tennis():
        if match["heure"] == heure_dans_1h:
            envoyer_pronostic_tennis(match["match"], match.get("tournoi", ""), match.get("surface", ""))

def run_scheduler():
    schedule.every(1).minutes.do(verifier_matchs)
    while True:
        schedule.run_pending()
        time.sleep(30)

class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

@bot.message_handler(commands=["start"])
def start(message):
    bankroll = get_bankroll()
    bot.reply_to(message, f"Bot de pronostics actif!\n\nFoot : analyse 2h avant\nTennis : analyse 1h avant\n\nCapital : {bankroll:.2f}\n\nCommandes:\n/stats - voir tes statistiques\n/bankroll - voir ton capital\n/gagne [match] - marquer un pari gagne\n/perdu [match] - marquer un pari perdu")

@bot.message_handler(commands=["stats"])
def stats(message):
    bot.reply_to(message, get_statistiques())

@bot.message_handler(commands=["bankroll"])
def bankroll_cmd(message):
    bankroll = get_bankroll()
    bot.reply_to(message, f"Capital actuel : {bankroll:.2f}\nCapital initial : {CAPITAL_INITIAL:.2f}\nProfit : {bankroll - CAPITAL_INITIAL:.2f}")

@bot.message_handler(commands=["gagne"])
def pari_gagne(message):
    try:
        match = message.text.replace("/gagne", "").strip()
        pari = collection_paris.find_one({"match": {"$regex": match, "$options": "i"}, "resultat": "en attente"})
        if pari:
            gain = round(pari["mise"] * pari["cote"] - pari["mise"], 2)
            bankroll = get_bankroll()
            nouvelle_bankroll = round(bankroll + gain, 2)
            update_bankroll(nouvelle_bankroll)
            collection_paris.update_one({"_id": pari["_id"]}, {"$set": {"resultat": "gagne", "gain": gain}})
            bot.reply_to(message, f"Pari gagne!\nGain : +{gain}\nNouvelle bankroll : {nouvelle_bankroll}")
        else:
            bot.reply_to(message, "Pari non trouve. Verifie le nom du match.")
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

@bot.message_handler(commands=["perdu"])
def pari_perdu(message):
    try:
        match = message.text.replace("/perdu", "").strip()
        pari = collection_paris.find_one({"match": {"$regex": match, "$options": "i"}, "resultat": "en attente"})
        if pari:
            bankroll = get_bankroll()
            nouvelle_bankroll = round(bankroll - pari["mise"], 2)
            update_bankroll(nouvelle_bankroll)
            collection_paris.update_one({"_id": pari["_id"]}, {"$set": {"resultat": "perdu", "gain": -pari["mise"]}})
            bot.reply_to(message, f"Pari perdu.\nPerte : -{pari['mise']}\nNouvelle bankroll : {nouvelle_bankroll}")
        else:
            bot.reply_to(message, "Pari non trouve. Verifie le nom du match.")
    except Exception as e:
        bot.reply_to(message, f"Erreur: {str(e)}")

@bot.message_handler(func=lambda m: True)
def analyser_message(message):
    sport = detecter_sport(message.text)
    if sport == "tennis":
        envoyer_pronostic_tennis(message.text)
    else:
        envoyer_pronostic_foot(message.text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheck)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    print("Bot demarre!")
    bot.infinity_polling()
