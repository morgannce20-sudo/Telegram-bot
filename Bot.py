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

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY   = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY   = os.environ.get("TENNIS_API_KEY")
MONGODB_URI      = os.environ.get("MONGODB_URI")
CHAT_ID          = "8449749928"

CAPITAL_INITIAL  = 50.0
MISE_MIN         = 1.0
MISE_MAX_PCT     = 0.10
CONFIANCE_MIN    = 0.60

# ─── CLIENTS ──────────────────────────────────────────────────────────────────
client_groq = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ─── MONGODB ──────────────────────────────────────────────────────────────────
mongo       = MongoClient(MONGODB_URI)
db          = mongo["pronostics"]
col_paris   = db["paris"]
col_capital = db["capital"]

def get_capital() -> float:
    doc = col_capital.find_one({"_id": "capital"})
    if doc:
        return doc["valeur"]
    col_capital.insert_one({"_id": "capital", "valeur": CAPITAL_INITIAL})
    return CAPITAL_INITIAL

def set_capital(valeur: float):
    col_capital.update_one(
        {"_id": "capital"},
        {"$set": {"valeur": round(valeur, 2)}},
        upsert=True
    )

def calculer_mise(confiance: float) -> float:
    capital = get_capital()
    if capital <= 0:
        return 0.0
    mise = capital * confiance * MISE_MAX_PCT
    mise = max(MISE_MIN, round(mise, 2))
    mise = min(mise, capital)
    return mise

def enregistrer_pari(match: str, sport: str, pari: str,
                     cote: float, confiance: float, mise: float):
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

def get_stats_historique() -> dict:
    paris = list(col_paris.find({"statut": {"$in": ["gagné", "perdu"]}}))
    if not paris:
        return {
            "total": 0, "gagnes": 0,
            "taux_reussite": 0, "roi": 0,
            "capital_actuel": round(get_capital(), 2)
        }
    gagnes = [p for p in paris if p["statut"] == "gagné"]
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

def _contexte_apprentissage() -> str:
    stats = get_stats_historique()
    if stats["total"] == 0:
        return "Aucun historique disponible. Capital de départ : 50 €."
    return (
        f"HISTORIQUE DU BOT (adapte tes recommandations en conséquence) :\n"
        f"- Paris joués : {stats['total']}\n"
        f"- Taux de réussite : {stats['taux_reussite']}%\n"
        f"- ROI : {stats['roi']}%\n"
        f"- Capital actuel : {stats['capital_actuel']} €\n"
        f"Si le ROI est négatif, sois plus sélectif et abaisse le niveau de confiance."
    )

def extraire_confiance_et_cote(texte: str) -> tuple:
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

# ─── RECHERCHE WEB ────────────────────────────────────────────────────────────
def recherche_web(query: str) -> str:
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

# ─── DÉTECTION SPORT ──────────────────────────────────────────────────────────
def detecter_sport(texte: str) -> str:
    try:
        chat = client_groq.chat.completions.create(
            messages=[{"role": "user", "content":
                f"Est-ce que ce texte parle de tennis ou de football ? "
                f"Réponds uniquement par 'tennis' ou 'foot' : {texte}"}],
            model="llama-3.3-70b-versatile",
        )
        r = chat.choices[0].message.content.lower().strip()
        return "tennis" if "tennis" in r else "foot"
    except:
        t = texte.lower()
        if any(m in t for m in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"

# ─── DONNÉES SPORTIVES ────────────────────────────────────────────────────────
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
                "match": f"{m['teams']['home']['name']} vs {m['teams']['away']['name']}",
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
                    "match":   f"{m['players']['home']['name']} vs {m['players']['away']['name']}",
                    "tournoi": m.get("tournament", {}).get("name", "Tournoi inconnu"),
                    "surface": m.get("surface", "inconnu"),
                })
            except:
                pass
        return matchs
    except:
        return []

# ─── PRONOSTIC FOOT ───────────────────────────────────────────────────────────
def envoyer_pronostic_foot(match: str, ligue: str = ""):
    try:
        capital = get_capital()
        if capital <= 0:
            bot.send_message(CHAT_ID, "⚠️ Capital épuisé !")
            return

        bot.send_message(CHAT_ID,
            f"⚽ Match foot dans 2h !\n{match}\n"
            f"Compétition : {ligue}\n⏳ Analyse en cours...")

        infos  = recherche_web(match + " stats forme composition equipe 2026")
        infos += recherche_web(match + " blessures absents suspendus 2026")
        infos += recherche_web(match + " cotes bookmakers pronostic 2026")
        infos += recherche_web(match + " historique confrontations head to head")
        infos += recherche_web(match + " classement " + ligue + " 2026")
        infos += recherche_web(match + " buteurs forme recente 2026")

        prompt = f"""Tu es un expert FOOTBALL en 2026. Analyse ce match avec des DONNÉES RÉELLES uniquement.

{_contexte_apprentissage()}

Infos collectées sur l
