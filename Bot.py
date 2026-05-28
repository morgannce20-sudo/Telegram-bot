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

# --- CONFIGURATION ---
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY   = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY   = os.environ.get("TENNIS_API_KEY")
MONGODB_URI      = os.environ.get("MONGODB_URI")
CHAT_ID          = "8449749928"

CAPITAL_INITIAL = 50.0
MISE_MIN        = 1.0
MISE_MAX_PCT    = 0.10
CONFIANCE_MIN   = 0.60

# --- CLIENTS ---
client_groq = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# --- MONGODB ---
mongo       = MongoClient(MONGODB_URI)
db          = mongo["pronostics"]
col_paris   = db["paris"]
col_capital = db["capital"]

def get_capital():
    doc = col_capital.find_one({"_id": "capital"})
    if doc:
        return doc["valeur"]
    col_capital.insert_one({"_id": "capital", "valeur": CAPITAL_INITIAL})
    return CAPITAL_INITIAL

def set_capital(valeur):
    col_capital.update_one(
        {"_id": "capital"},
        {"$set": {"valeur": round(valeur, 2)}},
        upsert=True
    )

def calculer_mise(confiance):
    capital = get_capital()
    if capital <= 0:
        return 0.0
    mise = capital * confiance * MISE_MAX_PCT
    mise = max(MISE_MIN, round(mise, 2))
    mise = min(mise, capital)
    return mise

def enregistrer_pari(match, sport, pari, cote, confiance, mise):
    doc = {
        "date":          datetime.now(),
        "match":         match,
        "sport":         sport,
        "pari":          pari,
        "cote":          cote,
        "confiance":     confiance,
        "mise":          mise,
        "statut":        "en_attente",
        "capital_avant": get_capital(),
    }
    col_paris.insert_one(doc)
    set_capital(get_capital() - mise)

def get_stats_historique():
    paris = list(col_paris.find({"statut": {"$in": ["gagne", "perdu"]}}))
    if not paris:
        return {"total": 0, "gagnes": 0, "taux_reussite": 0, "roi": 0, "capital_actuel": round(get_capital(), 2)}
    gagnes = [p for p in paris if p["statut"] == "gagne"]
    perdus = [p for p in paris if p["statut"] == "perdu"]
    gains  = sum(p["mise"] * (p["cote"] - 1) for p in gagnes)
    pertes = sum(p["mise"] for p in perdus)
    total_mise = sum(p["mise"] for p in paris)
    roi = round((gains - pertes) / total_mise * 100, 1) if total_mise > 0 else 0
    return {
        "total":          len(paris),
        "gagnes":         len(gagnes),
        "taux_reussite":  round(len(gagnes) / len(paris) * 100, 1),
        "roi":            roi,
        "capital_actuel": round(get_capital(), 2),
    }

def contexte_apprentissage():
    stats = get_stats_historique()
    if stats["total"] == 0:
        return "Aucun historique disponible. Capital de depart : 50 euros."
    return (
        "HISTORIQUE DU BOT :\n"
        "- Paris joues : " + str(stats["total"]) + "\n"
        "- Taux de reussite : " + str(stats["taux_reussite"]) + "%\n"
        "- ROI : " + str(stats["roi"]) + "%\n"
        "- Capital actuel : " + str(stats["capital_actuel"]) + " euros\n"
        "Si le ROI est negatif, sois plus selectif."
    )

def extraire_confiance_et_cote(texte):
    import re
    confiance = 0.55
    cote      = 1.80
    pari      = "Inconnu"
    m = re.search(r"Niveau de confiance\s*[:\-]\s*(\d+)%", texte, re.IGNORECASE)
    if m:
        confiance = int(m.group(1)) / 100
    m = re.search(r"Cote estim[ée]e?\s*[:\-]\s*([\d,.]+)", texte, re.IGNORECASE)
    if m:
        cote = float(m.group(1).replace(",", "."))
    m = re.search(r"Pari conseill[ée]\s*[:\-]\s*(.+)", texte, re.IGNORECASE)
    if m:
        pari = m.group(1).strip()
    return confiance, cote, pari

# --- RECHERCHE WEB ---
def recherche_web(query):
    try:
        url    = "https://serpapi.com/search"
        params = {"q": query, "api_key": SEARCH_API_KEY, "num": 5, "hl": "fr"}
        data   = requests.get(url, params=params, timeout=10).json()
        return "\n".join(
            r.get("title", "") + " - " + r.get("snippet", "")
            for r in data.get("organic_results", [])[:5]
        )
    except:
        return ""

# --- DETECTION SPORT ---
def detecter_sport(texte):
    try:
        chat = client_groq.chat.completions.create(
            messages=[{"role": "user", "content":
                "Est-ce que ce texte parle de tennis ou de football ? "
                "Reponds uniquement par 'tennis' ou 'foot' : " + texte}],
            model="llama-3.3-70b-versatile",
        )
        r = chat.choices[0].message.content.lower().strip()
        return "tennis" if "tennis" in r else "foot"
    except:
        t = texte.lower()
        if any(m in t for m in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"

# --- DONNEES SPORTIVES ---
def get_matchs_foot():
    try:
        today   = datetime.now().strftime("%Y-%m-%d")
        url     = "https://v3.football.api-sports.io/fixtures"
        headers = {"x-apisports-key": FOOTBALL_API_KEY}
        params  = {"date": today, "league": "39,140,135,78,61,2,3", "season": "2024"}
        data    = requests.get(url, headers=headers, params=params, timeout=10).json()
        matchs  = []
        for m in data.get("response", []):
            matchs.append({
                "heure": m["fixture"]["date"][11:16],
                "match": m["teams"]["home"]["name"] + " vs " + m["teams"]["away"]["name"],
                "ligue": m["league"]["name"],
            })
        return matchs
    except:
        return []

def get_matchs_tennis():
    try:
        today   = datetime.now().strftime("%Y-%m-%d")
        url     = "https://v1.tennis.api-sports.io/games"
        headers = {"x-apisports-key": TENNIS_API_KEY}
        data    = requests.get(url, headers=headers, params={"date": today}, timeout=10).json()
        matchs  = []
        for m in data.get("response", []):
            try:
                matchs.append({
                    "heure":   m["date"][11:16],
                    "match":   m["players"]["home"]["name"] + " vs " + m["players"]["away"]["name"],
                    "tournoi": m.get("tournament", {}).get("name", "Tournoi inconnu"),
                    "surface": m.get("surface", "inconnu"),
                })
            except:
                pass
        return matchs
    except:
        return []

# --- PRONOSTIC FOOT ---
def envoyer_pronostic_foot(match, ligue=""):
    try:
        capital = get_capital()
        if capital <= 0:
            bot.send_message(CHAT_ID, "Capital epuise !")
            return

        bot.send_message(CHAT_ID, "Match foot dans 2h !\n" + match + "\nCompetition : " + ligue + "\nAnalyse en cours...")

        infos  = recherche_web(match + " stats forme composition equipe 2026")
        infos += recherche_web(match + " blessures absents suspendus 2026")
        infos += recherche_web(match + " cotes bookmakers pronostic 2026")
        infos += recherche_web(match + " historique confrontations head to head")
        infos += recherche_web(match + " classement " + ligue + " 2026")
        infos += recherche_web(match + " buteurs forme recente 2026")

        prompt = (
            "Tu es un expert FOOTBALL en 2026. Analyse ce match avec des DONNEES REELLES uniquement.\n\n"
            + contexte_apprentissage() + "\n\n"
            "Infos collectees sur le web :\n" + infos + "\n\n"
            "MATCH : " + match + "\n"
            "COMPETITION : " + ligue + "\n\n"
            "Fournis une analyse complete structuree exactement comme ceci :\n\n"
            "MATCH : " + match + "\n"
            "Competition : " + ligue + "\n\n"
            "FORME ACTUELLE (5 derniers matchs) :\n"
            "-> [Equipe1] : [W-D-L] | Buts marques : X | Buts encaisses : X | Serie actuelle\n"
            "-> [Equipe2] : [W-D-L] | Buts marques : X | Buts encaisses : X | Serie actuelle\n\n"
            "DOMICILE / EXTERIEUR :\n"
            "-> [Equipe1] a domicile cette saison : [W-D-L]\n"
            "-> [Equipe2] a l exterieur cette saison : [W-D-L]\n\n"
            "JOUEURS CLES :\n"
            "-> [Equipe1] : [Nom] - [Stat precise]\n"
            "-> [Equipe2] : [Nom] - [Stat precise]\n\n"
            "BLESSURES ET ABSENCES :\n"
            "-> [Equipe1] : [Noms ou Aucune absence confirmee]\n"
            "-> [Equipe2] : [Noms ou Aucune absence confirmee]\n\n"
            "HEAD TO HEAD :\n"
            "-> Historique global : [X victoires E1 / X nuls / X victoires E2]\n"
            "-> Dernier match : [date + score]\n\n"
            "NEWS IMPORTANTES :\n"
            "-> [Infos recentes]\n\n"
            "PROBABILITES :\n"
            "-> [Equipe1] : XX%\n"
            "-> Match nul : XX%\n"
            "-> [Equipe2] : XX%\n\n"
            "SCORE PROBABLE : X - X\n\n"
            "BUTEURS PROBABLES :\n"
            "-> [Equipe1] : [Nom] (XX% de chances)\n"
            "-> [Equipe2] : [Nom] (XX% de chances)\n\n"
            "NOMBRE DE BUTS :\n"
            "-> Plus de 2.5 : XX%\n"
            "-> Moins de 2.5 : XX%\n"
            "-> BTTS : XX%\n\n"
            "ANALYSE TACTIQUE :\n"
            "-> Systemes de jeu et avantages/faiblesses\n\n"
            "RECOMMANDATION FINALE :\n"
            "-> Pari conseille : [PARI PRECIS]\n"
            "-> Cote estimee : [X.XX]\n"
            "-> Niveau de confiance : XX%\n\n"
            "Utilise UNIQUEMENT des donnees reelles et verifiees."
        )

        chat = client_groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        analyse = chat.choices[0].message.content
        bot.send_message(CHAT_ID, analyse)

        confiance, cote, pari = extraire_confiance_et_cote(analyse)
        if confiance >= CONFIANCE_MIN:
            mise = calculer_mise(confiance)
            enregistrer_pari(match, "foot", pari, cote, confiance, mise)
            bot.send_message(CHAT_ID,
                "Mise automatique : " + str(mise) + " euros @ " + str(cote) + "\n"
                "Confiance : " + str(int(confiance * 100)) + "%\n"
                "Capital restant : " + str(round(get_capital(), 2)) + " euros\n\n"
                "/gagne si gagne\n/perdu si perdu")
        else:
            bot.send_message(CHAT_ID,
                "Confiance trop faible (" + str(int(confiance * 100)) + "%) - pas de mise.\n"
                "Capital conserve : " + str(round(capital, 2)) + " euros")
    except Exception as e:
        bot.send_message(CHAT_ID, "Erreur foot : " + str(e))


# --- PRONOSTIC TENNIS ---
def envoyer_pronostic_tennis(match, tournoi="", surface=""):
    try:
        capital = get_capital()
        if capital <= 0:
            bot.send_message(CHAT_ID, "Capital epuise !")
            return

        bot.send_message(CHAT_ID, "Match tennis dans 1h !\n" + match + "\nTournoi : " + tournoi + " | Surface : " + surface + "\nAnalyse en cours...")

        infos  = recherche_web(match + " tennis stats forme recente 2026")
        infos += recherche_web(match + " head to head historique surface " + surface)
        infos += recherche_web(match + " blessure actualite 2026")
        infos += recherche_web(match + " classement ATP WTA 2026")
        infos += recherche_web(match + " pronostic cote bookmaker 2026")

        prompt = (
            "Tu es un expert TENNIS en 2026. Analyse ce match avec des DONNEES REELLES uniquement.\n"
            "Ces personnes sont des JOUEURS DE TENNIS. Ne parle JAMAIS de football.\n\n"
            + contexte_apprentissage() + "\n\n"
            "Infos collectees sur le web :\n" + infos + "\n\n"
            "MATCH : " + match + "\n"
            "TOURNOI : " + tournoi + "\n"
            "SURFACE : " + surface + "\n\n"
            "Fournis une analyse complete structuree exactement comme ceci :\n\n"
            "MATCH : " + match + "\n"
            "Tournoi : " + tournoi + " | Surface : " + surface + "\n\n"
            "FORME RECENTE (5 derniers matchs) :\n"
            "-> [Joueur1] : [W-L] | Derniers resultats\n"
            "-> [Joueur2] : [W-L] | Derniers resultats\n\n"
            "SURFACE :\n"
            "-> Stats sur " + surface + " cette saison\n"
            "-> Avantage : [Joueur favori et pourquoi]\n\n"
            "HEAD TO HEAD :\n"
            "-> Historique global : [X-Y]\n"
            "-> Sur " + surface + " : [X-Y]\n"
            "-> Dernier match : [date + score]\n\n"
            "BLESSURES :\n"
            "-> [Joueur1] : [etat ou RAS]\n"
            "-> [Joueur2] : [etat ou RAS]\n\n"
            "NEWS IMPORTANTES :\n"
            "-> [Infos recentes]\n\n"
            "PROBABILITES :\n"
            "-> [Joueur1] : XX%\n"
            "-> [Joueur2] : XX%\n\n"
            "SCORE PREDIT : X-X sets\n\n"
            "ANALYSE TACTIQUE :\n"
            "-> Style de jeu et confrontation sur cette surface\n\n"
            "RECOMMANDATION FINALE :\n"
            "-> Pari conseille : [VAINQUEUR ou SETS ou HANDICAP]\n"
            "-> Cote estimee : [X.XX]\n"
            "-> Niveau de confiance : XX%\n\n"
            "Utilise UNIQUEMENT des donnees reelles et verifiees."
        )

        chat = client_groq.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        analyse = chat.choices[0].message.content
        bot.send_message(CHAT_ID, analyse)

        confiance, cote, pari = extraire_confiance_et_cote(analyse)
        if confiance >= CONFIANCE_MIN:
            mise = calculer_mise(confiance)
            enregistrer_pari(match, "tennis", pari, cote, confiance, mise)
            bot.send_message(CHAT_ID,
                "Mise automatique : " + str(mise) + " euros @ " + str(cote) + "\n"
                "Confiance : " + str(int(confiance * 100)) + "%\n"
                "Capital restant : " + str(round(get_capital(), 2)) + " euros\n\n"
                "/gagne si gagne\n/perdu si perdu")
        else:
            bot.send_message(CHAT_ID,
                "Confiance trop faible (" + str(int(confiance * 100)) + "%) - pas de mise.\n"
                "Capital conserve : " + str(round(capital, 2)) + " euros")
    except Exception as e:
        bot.send_message(CHAT_ID, "Erreur tennis : " + str(e))


# --- SCHEDULER ---
def verifier_matchs():
    maintenant    = datetime.now()
    heure_dans_2h = (maintenant + timedelta(hours=2)).strftime("%H:%M")
    heure_dans_1h = (maintenant + timedelta(hours=1)).strftime("%H:%M")
    for match in get_matchs_foot():
        if match["heure"] == heure_dans_2h:
            envoyer_pronostic_foot(match["match"], match.get("ligue", ""))
    for match in get_matchs_tennis():
        if match["heure"] == heure_dans_1h:
            envoyer_pronostic_tennis(match["match"], match.get("tournoi", ""), match.get("surface", ""))


# --- COMMANDES TELEGRAM ---
@bot.message_handler(commands=["start"])
def cmd_start(message):
    capital = get_capital()
    bot.reply_to(message,
        "Bot de pronostics actif !\n"
        "Capital : " + str(round(capital, 2)) + " euros\n\n"
        "Commandes :\n"
        "/capital - capital et stats\n"
        "/historique - 5 derniers paris\n"
        "/gagne - pari gagne\n"
        "/perdu - pari perdu\n"
        "/encours - paris en attente")

@bot.message_handler(commands=["capital"])
def cmd_capital(message):
    stats = get_stats_historique()
    bot.reply_to(message,
        "Capital actuel : " + str(stats["capital_actuel"]) + " euros\n"
        "Capital initial : " + str(CAPITAL_INITIAL) + " euros\n"
        "Paris joues : " + str(stats["total"]) + "\n"
        "Gagnes : " + str(stats["gagnes"]) + " (" + str(stats["taux_reussite"]) + "%)\n"
        "ROI : " + str(stats["roi"]) + "%")

@bot.message_handler(commands=["gagne"])
def cmd_gagne(message):
    pari = col_paris.find_one({"statut": "en_attente"}, sort=[("date", -1)])
    if not pari:
        bot.reply_to(message, "Aucun pari en attente.")
        return
    gain = round(pari["mise"] * pari["cote"], 2)
    set_capital(get_capital() + gain)
    col_paris.update_one({"_id": pari["_id"]}, {"$set": {"statut": "gagne", "gain": gain}})
    bot.reply_to(message,
        "Pari gagne !\n"
        "Match : " + pari["match"] + "\n"
        "Mise : " + str(pari["mise"]) + " euros @ " + str(pari["cote"]) + "\n"
        "Gain : +" + str(gain) + " euros\n"
        "Nouveau capital : " + str(round(get_capital(), 2)) + " euros")

@bot.message_handler(commands=["perdu"])
def cmd_perdu(message):
    pari = col_paris.find_one({"statut": "en_attente"}, sort=[("date", -1)])
    if not pari:
        bot.reply_to(message, "Aucun pari en attente.")
        return
    col_paris.update_one({"_id": pari["_id"]}, {"$set": {"statut": "perdu", "gain": 0}})
    bot.reply_to(message,
        "Pari perdu.\n"
        "Match : " + pari["match"] + "\n"
        "Mise perdue : -" + str(pari["mise"]) + " euros\n"
        "Capital restant : " + str(round(get_capital(), 2)) + " euros")

@bot.message_handler(commands=["encours"])
def cmd_encours(message):
    paris = list(col_paris.find({"statut": "en_attente"}).sort("date", -1))
    if not paris:
        bot.reply_to(message, "Aucun pari en attente.")
        return
    lignes = ["Paris en attente :\n"]
    for p in paris:
        lignes.append("- " + p["match"] + " | " + p["pari"] + " | " + str(p["mise"]) + " euros @ " + str(p["cote"]))
    bot.reply_to(message, "\n".join(lignes))

@bot.message_handler(commands=["historique"])
def cmd_historique(message):
    paris = list(col_paris.find().sort("date", -1).limit(5))
    if not paris:
        bot.reply_to(message, "Aucun historique disponible.")
        return
    lignes = ["5 derniers paris :\n"]
    for p in paris:
        emoji = "OK" if p["statut"] == "gagne" else ("X" if p["statut"] == "perdu" else "?")
        lignes.append(emoji + " " + p["match"] + " | " + p["pari"] + " | " + str(p["mise"]) + " euros @ " + str(p["cote"]) + " | " + p["statut"])
    bot.reply_to(message, "\n".join(lignes))

@bot.message_handler(func=lambda m: True)
def analyser_message(message):
    sport = detecter_sport(message.text)
    if sport == "tennis":
        envoyer_pronostic_tennis(message.text)
    else:
        envoyer_pronostic_foot(message.text)


# --- HEALTH CHECK ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass


# --- MAIN ---
def run_scheduler():
    schedule.every(1).minutes.do(verifier_matchs)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    print("Bot demarre ! Capital : " + str(get_capital()) + " euros")
    threading.Thread(target=run_scheduler, daemon=True).start()
    server = HTTPServer(("0.0.0.0", 8080), HealthCheck)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bot.infinity_polling()
