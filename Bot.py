import os
import time
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import telebot
from groq import Groq
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# ---------------------------------------------------------------------------
# Configuration & logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
TENNIS_API_KEY = os.environ.get("TENNIS_API_KEY")
MONGODB_URL = os.environ.get("MONGODB_URL")
CAPITAL_INITIAL = 50.0
SAISON_FOOT = os.environ.get("SAISON_FOOT", "2025")

# Chat ou seront envoyes les pronostics automatiques (ton chat prive).
# Peut etre surcharge par une variable d'environnement Render.
MON_CHAT_ID = os.environ.get("MON_CHAT_ID", "8233541336")
# Combien de minutes avant le coup d'envoi on envoie l'analyse.
MINUTES_AVANT = int(os.environ.get("MINUTES_AVANT", "30"))
# Marge : on envoie si le match est entre MINUTES_AVANT et MINUTES_AVANT+10 min.
FENETRE_MINUTES = 10

# --- Validation des variables d'environnement au demarrage -----------------
REQUIS = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "GROQ_API_KEY": GROQ_API_KEY,
    "MONGODB_URL": MONGODB_URL,
}
manquants = [nom for nom, valeur in REQUIS.items() if not valeur]
if manquants:
    raise RuntimeError(f"Variables d'environnement manquantes : {', '.join(manquants)}")

# ---------------------------------------------------------------------------
# Clients externes
# ---------------------------------------------------------------------------
client = Groq(api_key=GROQ_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000)
db = mongo_client["pronostics"]
collection_paris = db["paris"]
collection_bankroll = db["bankroll"]
collection_envois = db["envois_auto"]


def verifier_mongo():
    """Teste la connexion Mongo au demarrage pour un diagnostic clair."""
    try:
        mongo_client.admin.command("ping")
        log.info("Connexion MongoDB OK")
        return True
    except PyMongoError as e:
        log.error("ERREUR MongoDB au demarrage : %s", e)
        return False


# ---------------------------------------------------------------------------
# Bankroll & paris
# ---------------------------------------------------------------------------
def get_bankroll():
    try:
        doc = collection_bankroll.find_one({"id": "bankroll"})
        if doc:
            return doc["capital"]
        collection_bankroll.insert_one({"id": "bankroll", "capital": CAPITAL_INITIAL})
        return CAPITAL_INITIAL
    except PyMongoError as e:
        log.error("get_bankroll : %s", e)
        return CAPITAL_INITIAL


def update_bankroll(nouveau_capital):
    try:
        collection_bankroll.update_one(
            {"id": "bankroll"}, {"$set": {"capital": nouveau_capital}}, upsert=True
        )
    except PyMongoError as e:
        log.error("update_bankroll : %s", e)


def calculer_mise(confiance, cote, bankroll):
    """Kelly fractionne base sur la cote reelle, plafonne entre 2% et 10%."""
    p = confiance / 100.0
    b = max(cote - 1, 0.01)
    kelly = (b * p - (1 - p)) / b
    kelly = max(0.02, min(kelly, 0.10))
    return round(bankroll * kelly, 2)


def sauvegarder_pari(match, sport, pari, confiance, mise, cote):
    try:
        collection_paris.insert_one({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "match": match,
            "sport": sport,
            "pari": pari,
            "confiance": confiance,
            "mise": mise,
            "cote": cote,
            "resultat": "en attente",
            "gain": 0,
        })
    except PyMongoError as e:
        log.error("sauvegarder_pari : %s", e)


def get_statistiques():
    try:
        paris = list(collection_paris.find({"resultat": {"$ne": "en attente"}}))
    except PyMongoError as e:
        log.error("get_statistiques : %s", e)
        return "Erreur de connexion a la base de donnees."
    if not paris:
        return "Aucun pari termine pour le moment."
    total = len(paris)
    gagnes = len([p for p in paris if p["resultat"] == "gagne"])
    pertes = len([p for p in paris if p["resultat"] == "perdu"])
    gain_total = sum(p.get("gain", 0) for p in paris)
    taux = round(gagnes / total * 100, 1) if total else 0
    return (
        "STATISTIQUES :\n"
        f"Total paris : {total}\n"
        f"Gagnes : {gagnes} | Perdus : {pertes}\n"
        f"Taux de reussite : {taux}%\n"
        f"Gain/Perte total : {gain_total:.2f}\n"
        f"Bankroll actuelle : {get_bankroll():.2f}"
    )


# ---------------------------------------------------------------------------
# Recherche web & analyse IA
# ---------------------------------------------------------------------------
def recherche_web(query):
    if not SEARCH_API_KEY:
        return ""
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": SEARCH_API_KEY, "num": 5, "hl": "fr"},
            timeout=10,
        )
        data = r.json()
        return "\n".join(
            f"{res.get('title', '')} - {res.get('snippet', '')}"
            for res in data.get("organic_results", [])[:5]
        )
    except (requests.RequestException, ValueError) as e:
        log.error("recherche_web : %s", e)
        return ""


def detecter_sport(texte):
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content":
                f"Ce texte parle de tennis ou de football ? Reponds uniquement 'tennis' ou 'foot' : {texte}"}],
            model="llama-3.3-70b-versatile",
        )
        if "tennis" in chat.choices[0].message.content.lower():
            return "tennis"
        return "foot"
    except Exception as e:
        log.error("detecter_sport (fallback) : %s", e)
        t = texte.lower()
        if any(m in t for m in ["tennis", "atp", "wta", "roland", "wimbledon"]):
            return "tennis"
        return "foot"


def analyser_match(match, sport):
    if sport == "tennis":
        infos = recherche_web(f"{match} tennis stats forme recente 2026")
        infos += recherche_web(f"{match} head to head historique surface")
        infos += recherche_web(f"{match} blessure actualite 2026")
        infos += recherche_web(f"{match} classement ATP WTA 2026")
        infos += recherche_web(f"{match} pronostic cote bookmaker 2026")
        prompt = f"""Tu es un expert TENNIS. Analyse ce match avec des DONNEES REELLES.
Ces personnes sont des JOUEURS DE TENNIS professionnels. Ne parle JAMAIS de football.

Infos collectees sur le web :
{infos}

MATCH : {match}

Fournis une analyse complete et structuree exactement comme ceci, en francais :

🎾 MATCH : {match}
🏆 Tournoi : [nom] | Surface : [surface]

📊 FORME RECENTE (5 derniers matchs) :
→ [Joueur1] : [W-L] | Derniers resultats avec scores exacts
→ [Joueur2] : [W-L] | Derniers resultats avec scores exacts

🎯 SURFACE :
→ Stats sur cette surface cette saison pour chaque joueur (victoires/defaites)
→ Avantage : [Joueur qui a l'avantage sur cette surface et pourquoi]

🔄 HEAD TO HEAD :
→ Historique global : [X-Y]
→ Dernier match : [date + score]

🤕 BLESSURES & FORME PHYSIQUE :
→ [Joueur1] : [etat physique confirme ou RAS]
→ [Joueur2] : [etat physique confirme ou RAS]

📰 NEWS IMPORTANTES :
→ [Infos recentes importantes pour ce match]

📈 PROBABILITES :
→ [Joueur1] : XX%
→ [Joueur2] : XX%

🎯 SCORE PREDIT : X-X sets

💡 ANALYSE TACTIQUE :
→ Style de jeu de chaque joueur et comment ils s'affrontent sur cette surface

Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :
PARI: [ton pronostic]
CONFIANCE: [pourcentage entre 50 et 95]
COTE: [cote estimee, ex 1.85]

Utilise les donnees reelles trouvees. Si une info est incertaine, indique-le, mais remplis chaque section au mieux."""
    else:
        infos = recherche_web(f"{match} stats forme composition equipe 2026")
        infos += recherche_web(f"{match} blessures absents suspendus 2026")
        infos += recherche_web(f"{match} cotes bookmakers pronostic 2026")
        infos += recherche_web(f"{match} historique confrontations head to head")
        infos += recherche_web(f"{match} classement 2026")
        infos += recherche_web(f"{match} buteurs forme recente 2026")
        prompt = f"""Tu es un expert FOOTBALL. Analyse ce match avec des DONNEES REELLES.
Utilise UNIQUEMENT les joueurs actuellement dans ces clubs.

Infos collectees sur le web :
{infos}

MATCH : {match}

Fournis une analyse complete et structuree exactement comme ceci, en francais :

⚽ MATCH : {match}
🏆 Competition : [nom]

📊 FORME ACTUELLE (5 derniers matchs) :
→ [Equipe1] : [W-D-L] | Buts marques : X | Buts encaisses : X | Serie actuelle
→ [Equipe2] : [W-D-L] | Buts marques : X | Buts encaisses : X | Serie actuelle

🏠 DOMICILE / EXTERIEUR :
→ [Equipe1] a domicile cette saison : [W-D-L]
→ [Equipe2] a l'exterieur cette saison : [W-D-L]

👥 JOUEURS CLES (noms reels) :
→ [Equipe1] : [Nom joueur] - [Stat precise : X buts / X passes en N matchs]
→ [Equipe2] : [Nom joueur] - [Stat precise : X buts / X passes en N matchs]

🚑 BLESSURES & ABSENCES :
→ [Equipe1] : [Noms confirmes ou Aucune absence confirmee]
→ [Equipe2] : [Noms confirmes ou Aucune absence confirmee]

🔄 HEAD TO HEAD :
→ Historique global : [X victoires E1 / X nuls / X victoires E2]
→ Dernier match : [date + score]

📰 NEWS IMPORTANTES :
→ [Infos recentes qui impactent le match : motivation, contexte, etc.]

📈 PROBABILITES :
→ [Equipe1] : XX%
→ Match nul : XX%
→ [Equipe2] : XX%

🎯 SCORE PROBABLE : X - X

⚽ BUTEURS PROBABLES :
→ [Equipe1] : [Nom] (XX% de chances - X buts cette saison)
→ [Equipe2] : [Nom] (XX% de chances - X buts cette saison)

🔢 NOMBRE DE BUTS :
→ Plus de 2.5 : XX%
→ Moins de 2.5 : XX%
→ Les deux equipes marquent (BTTS) : XX%

💡 ANALYSE TACTIQUE :
→ Systeme de jeu de chaque equipe et avantages/faiblesses

Puis termine OBLIGATOIREMENT par ces 3 lignes exactes :
PARI: [ton pronostic]
CONFIANCE: [pourcentage entre 50 et 95]
COTE: [cote estimee, ex 1.85]

Utilise les donnees reelles trouvees. Si une info est incertaine, indique-le, mais remplis chaque section au mieux."""
    try:
        chat = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=2000,
        )
        return chat.choices[0].message.content
    except Exception as e:
        log.error("analyser_match : %s", e)
        return f"Erreur analyse IA : {e}"


def parser_analyse(texte):
    pari, confiance, cote = "Non determine", 50.0, 1.5
    for ligne in texte.split("\n"):
        l = ligne.strip()
        u = l.upper()
        if u.startswith("PARI:"):
            pari = l.split(":", 1)[1].strip()
        elif u.startswith("CONFIANCE:"):
            try:
                confiance = float("".join(c for c in l.split(":", 1)[1] if c.isdigit() or c == "."))
            except ValueError:
                pass
        elif u.startswith("COTE:"):
            try:
                cote = float("".join(c for c in l.split(":", 1)[1] if c.isdigit() or c == "."))
            except ValueError:
                pass
    confiance = max(50.0, min(confiance, 95.0))
    cote = max(1.01, cote)
    return pari, confiance, cote


# ---------------------------------------------------------------------------
# Recuperation des matchs
# ---------------------------------------------------------------------------
def get_matchs_foot():
    if not FOOTBALL_API_KEY:
        return []
    try:
        r = requests.get(
            "https://v3.football.api-sports.io/fixtures",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d"),
                    "league": "39,140,135,78,61,2,3", "season": SAISON_FOOT},
            timeout=10,
        )
        data = r.json()
        matchs = []
        for m in data.get("response", []):
            matchs.append({
                "heure": m["fixture"]["date"][11:16],
                "match": f'{m["teams"]["home"]["name"]} vs {m["teams"]["away"]["name"]}',
                "ligue": m["league"]["name"],
            })
        return matchs
    except (requests.RequestException, ValueError, KeyError) as e:
        log.error("get_matchs_foot : %s", e)
        return []


def get_matchs_tennis():
    if not TENNIS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://v1.tennis.api-sports.io/games",
            headers={"x-apisports-key": TENNIS_API_KEY},
            params={"date": datetime.now().strftime("%Y-%m-%d")},
            timeout=10,
        )
        data = r.json()
        matchs = []
        for m in data.get("response", []):
            try:
                matchs.append({
                    "heure": m["date"][11:16],
                    "match": f'{m["players"]["home"]["name"]} vs {m["players"]["away"]["name"]}',
                    "tournoi": m.get("tournament", {}).get("name", "Tournoi inconnu"),
                    "surface": m.get("surface", "Inconnue"),
                })
            except (KeyError, TypeError):
                continue
        return matchs
    except (requests.RequestException, ValueError) as e:
        log.error("get_matchs_tennis : %s", e)
        return []


# ---------------------------------------------------------------------------
# Handlers Telegram
# ---------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(
        message,
        "Bot de pronostics actif.\n"
        "Envoie un match a analyser, ou utilise :\n"
        "/matchs - matchs du jour\n"
        "/stats - statistiques\n"
        "/bankroll - capital actuel",
    )


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    bot.reply_to(message, get_statistiques())


@bot.message_handler(commands=["bankroll"])
def cmd_bankroll(message):
    bot.reply_to(message, f"Bankroll actuelle : {get_bankroll():.2f}")


@bot.message_handler(commands=["matchs"])
def cmd_matchs(message):
    foot = get_matchs_foot()
    tennis = get_matchs_tennis()
    lignes = ["MATCHS DU JOUR\n", "FOOT :"]
    lignes += [f'{m["heure"]} - {m["match"]} ({m["ligue"]})' for m in foot] or ["  aucun"]
    lignes.append("\nTENNIS :")
    lignes += [f'{m["heure"]} - {m["match"]} ({m["tournoi"]})' for m in tennis] or ["  aucun"]
    bot.reply_to(message, "\n".join(lignes))


@bot.message_handler(func=lambda m: True)
def traiter_message(message):
    texte = message.text or ""
    sport = detecter_sport(texte)
    bot.reply_to(message, "Analyse en cours...")
    envoyer_analyse(message.chat.id, texte, sport)


def envoyer_analyse(chat_id, texte, sport):
    """Analyse un match et envoie le resultat decoupe au chat indique."""
    analyse = analyser_match(texte, sport)
    pari, confiance, cote = parser_analyse(analyse)
    bankroll = get_bankroll()
    mise = calculer_mise(confiance, cote, bankroll)
    sauvegarder_pari(texte, sport, pari, confiance, mise, cote)

    reponse = (
        f"{analyse}\n\n"
        f"💰 Mise conseillee : {mise:.2f} (bankroll : {bankroll:.2f})\n\n"
        "⚠️ Analyse generee par IA a titre indicatif. Certaines donnees "
        "peuvent etre incertaines ou non a jour. Verifie toujours avant de parier."
    )

    # Telegram limite a 4096 caracteres : on decoupe si besoin
    for i in range(0, len(reponse), 4000):
        bot.send_message(chat_id, reponse[i:i + 4000])


# ---------------------------------------------------------------------------
# Planificateur : pronostics automatiques 30 min avant chaque match
# ---------------------------------------------------------------------------
def deja_envoye(cle):
    """Verifie si un pronostic a deja ete envoye pour ce match aujourd'hui."""
    try:
        return collection_envois.find_one({"_id": cle}) is not None
    except PyMongoError as e:
        log.error("deja_envoye : %s", e)
        return False


def marquer_envoye(cle):
    try:
        collection_envois.update_one(
            {"_id": cle},
            {"$set": {"envoye_le": datetime.now().isoformat()}},
            upsert=True,
        )
    except PyMongoError as e:
        log.error("marquer_envoye : %s", e)


def minutes_avant_match(heure_str):
    """Retourne le nombre de minutes entre maintenant et l'heure du match.

    heure_str est au format 'HH:MM' (UTC, tel que renvoye par l'API).
    """
    try:
        maintenant = datetime.utcnow()
        h, m = heure_str.split(":")
        debut = maintenant.replace(hour=int(h), minute=int(m),
                                   second=0, microsecond=0)
        delta = (debut - maintenant).total_seconds() / 60.0
        return delta
    except (ValueError, AttributeError):
        return None


def verifier_et_envoyer():
    """Parcourt les matchs du jour et envoie ceux qui demarrent bientot."""
    aujourd_hui = datetime.utcnow().strftime("%Y-%m-%d")

    foot = [{**m, "sport": "foot"} for m in get_matchs_foot()]
    tennis = [{**m, "sport": "tennis"} for m in get_matchs_tennis()]

    for m in foot + tennis:
        minutes = minutes_avant_match(m["heure"])
        if minutes is None:
            continue
        # On envoie si le match commence dans MINUTES_AVANT a MINUTES_AVANT+FENETRE.
        if MINUTES_AVANT <= minutes < MINUTES_AVANT + FENETRE_MINUTES:
            cle = f'{aujourd_hui}_{m["sport"]}_{m["match"]}'
            if deja_envoye(cle):
                continue
            try:
                bot.send_message(
                    MON_CHAT_ID,
                    f'⏰ Match dans ~{int(minutes)} min\n{m["match"]} '
                    f'({m["heure"]} UTC)\nAnalyse en cours...',
                )
                envoyer_analyse(MON_CHAT_ID, m["match"], m["sport"])
                marquer_envoye(cle)
                log.info("Pronostic auto envoye : %s", m["match"])
            except Exception as e:
                log.error("Echec envoi auto pour %s : %s", m["match"], e)


def lancer_planificateur():
    """Boucle infinie : verifie les matchs toutes les 5 minutes."""
    log.info("Planificateur de pronostics automatiques demarre")
    while True:
        try:
            verifier_et_envoyer()
        except Exception as e:
            log.error("Erreur planificateur : %s", e)
        time.sleep(300)  # 5 minutes


# ---------------------------------------------------------------------------
# Serveur HTTP (health check Render) + lancement
# ---------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def lancer_serveur():
    port = int(os.environ.get("PORT", 8080))
    log.info("Serveur health check sur le port %s", port)
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


def lancer_bot():
    while True:
        try:
            log.info("Demarrage du polling Telegram")
            bot.infinity_polling(timeout=20, long_polling_timeout=20)
        except Exception as e:
            log.error("Crash du polling, redemarrage dans 15s : %s", e)
            time.sleep(15)


if __name__ == "__main__":
    verifier_mongo()
    threading.Thread(target=lancer_serveur, daemon=True).start()
    threading.Thread(target=lancer_planificateur, daemon=True).start()
    lancer_bot()
