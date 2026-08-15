import io
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

import chess
import chess.pgn
import chess.engine
import pandas as pd
import requests
import streamlit as st

APP_TITLE = "♟️ Mon Coach d'Échecs"
CHESS_API = "https://api.chess.com/pub"

st.set_page_config(page_title=APP_TITLE, page_icon="♟️", layout="wide")

# -----------------------------
# Configuration / utilities
# -----------------------------

def api_get(url):
    r = requests.get(
        url,
        headers={"User-Agent": "MonCoachEchecs/1.0 personal-training-app"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def get_player(username):
    return api_get(f"{CHESS_API}/player/{username}")

def get_archives(username):
    data = api_get(f"{CHESS_API}/player/{username}/games/archives")
    return data.get("archives", [])

def get_month_games(archive_url):
    data = api_get(archive_url)
    return data.get("games", [])

def parse_game(game_dict):
    pgn_text = game_dict.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    return game

def result_for_user(game, username):
    headers = game.headers
    white = headers.get("White", "").lower()
    black = headers.get("Black", "").lower()
    result = headers.get("Result", "*")
    if username.lower() == white:
        return result
    if username.lower() == black:
        return {"1-0": "0-1", "0-1": "1-0"}.get(result, result)
    return result

def user_color(game, username):
    if game.headers.get("White", "").lower() == username.lower():
        return chess.WHITE
    if game.headers.get("Black", "").lower() == username.lower():
        return chess.BLACK
    return None

def outcome_label(result):
    return {"1-0": "Victoire", "0-1": "Défaite", "1/2-1/2": "Nulle", "*": "En cours"}.get(result, result)

def eco_name(game):
    eco = game.headers.get("ECO", "")
    opening = game.headers.get("Opening", "")
    variation = game.headers.get("Variation", "")
    return " — ".join([x for x in [eco, opening, variation] if x])

def parse_clock_seconds(clock):
    if not clock:
        return None
    try:
        parts = clock.split(":")
        if len(parts) == 3:
            h, m, s = map(float, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(float, parts)
            return m * 60 + s
    except Exception:
        pass
    return None

def est_une_partie_longue(game_dict):
    """
    Filtre les parties pour ne garder que les cadences de réflexion.
    On considère une cadence longue si le temps initial est >= 600s (10 minutes).
    """
    time_control = game_dict.get("time_control", "")
    try:
        seconds = int(time_control.split('+')[0])
        return seconds >= 600
    except (ValueError, AttributeError, IndexError):
        return False

# -----------------------------
# Stockfish analysis
# -----------------------------

@st.cache_resource
def find_engine(path_hint=""):
    candidates = [
        path_hint,
        os.environ.get("STOCKFISH_PATH", ""),
        "/usr/games/stockfish",
        "/usr/bin/stockfish",
        "stockfish",
    ]
    for p in candidates:
        if not p:
            continue
        try:
            return chess.engine.SimpleEngine.popen_uci(p)
        except Exception:
            continue
    return None

def score_cp(score, pov):
    s = score.pov(pov)
    if s.is_mate():
        mate = s.mate()
        return 100000 if mate and mate > 0 else -100000
    return s.score(mate_score=100000)

def classify_drop(drop):
    if drop >= 250:
        return "Gaffe"
    if drop >= 100:
        return "Erreur"
    if drop >= 50:
        return "Imprécision"
    return "OK"

def calculer_note_partie(recs):
    if not recs: return 10
    total_drop = sum(r['drop'] for r in recs)
    return round(max(0, 10 - (total_drop / 300)), 1)
    
def analyze_game(game, username, engine, depth=16, max_plies=160):
    if engine is None:
        return None

    board = game.board()
    u_color = user_color(game, username)
    if u_color is None:
        return None

    records = []

    for ply, move in enumerate(game.mainline_moves()):
        if ply >= max_plies:
            break

        mover = board.turn
        before = board.copy()
        
        try:
            info_before = engine.analyse(before, chess.engine.Limit(depth=depth), multipv=1)
            pv_before = info_before.get("pv")
            best = pv_before[0] if pv_before else None
            eval_before = score_cp(info_before["score"], u_color)
        except Exception:
            board.push(move)
            continue

        san = board.san(move)
        board.push(move)

        try:
            info_after = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=1)
            eval_after = score_cp(info_after["score"], u_color)
        except Exception:
            eval_after = eval_before

        if mover == u_color:
            drop = max(0, eval_before - eval_after)
            category = classify_drop(drop)
            records.append({
                "ply": ply + 1,
                "move_no": ply // 2 + 1,
                "san": san,
                "drop": round(drop, 1),
                "category": category,
                "best": before.san(best) if best else "",
                "eval_before": round(eval_before / 100, 2),
                "eval_after": round(eval_after / 100, 2),
            })

    return records

# -----------------------------
# Coach layer
# -----------------------------

def build_coach_summary(stats):
    if not stats:
        return "Pas encore assez de données pour établir un diagnostic."

    total = stats["games"]
    wins = stats["wins"]
    losses = stats["losses"]
    draws = stats["draws"]
    blunders = stats["blunders"]
    errors = stats["errors"]
    inaccuracies = stats["inaccuracies"]

    lines = [
        f"Après **{total} partie(s)** analysée(s), ton bilan est de "
        f"**{wins} victoire(s), {draws} nulle(s), {losses} défaite(s)**.",
    ]

    if blunders / max(total, 1) >= 0.8:
        lines.append("🔴 **Priorité : les gaffes.** Tu dois surtout améliorer la vérification des menaces adverses.")
    elif errors / max(total, 1) >= 1.5:
        lines.append("🟠 **Priorité : la qualité des décisions critiques.** Il y a plusieurs erreurs qui coûtent significativement de l'évaluation.")
    elif inaccuracies / max(total, 1) >= 3:
        lines.append("🟡 **Priorité : la précision.** Le prochain palier est de réduire les petites pertes d'évaluation.")
    else:
        lines.append("🟢 Les erreurs graves semblent relativement contenues. On peut davantage travailler la compréhension et la conversion.")

    lines.append("Le coach ne cherche pas seulement le meilleur coup : il cherche surtout les **comportements qui se répètent**.")
    return "\n\n".join(lines)

def calculate_stats(games, analyses, username):
    stats = {
        "games": len(games), "wins": 0, "losses": 0, "draws": 0,
        "blunders": 0, "errors": 0, "inaccuracies": 0,
    }
    for g in games:
        r = result_for_user(g, username)
        if r == "1-0":
            stats["wins"] += 1
        elif r == "0-1":
            stats["losses"] += 1
        elif r == "1/2-1/2":
            stats["draws"] += 1

    for recs in analyses.values():
        for r in recs or []:
            if r["category"] == "Gaffe":
                stats["blunders"] += 1
            elif r["category"] == "Erreur":
                stats["errors"] += 1
            elif r["category"] == "Imprécision":
                stats["inaccuracies"] += 1
    return stats

# -----------------------------
# UI
# -----------------------------

st.title(APP_TITLE)
st.caption("Prototype personnel : analyse Stockfish + suivi des tendances + coaching pédagogique.")

with st.sidebar:
    st.header("⚙️ Paramètres")
    username = st.text_input("Pseudo Chess.com", value=st.session_state.get("username", "gerardmansoif"))
    st.session_state["username"] = username.strip()

    months = st.slider("Nombre de mois à récupérer", 1, 12, 3)
    max_games = st.slider("Nombre maximum de parties", 10, 200, 50, step=10)
    depth = st.slider("Profondeur Stockfish", 10, 20, 15)
    engine_path = st.text_input("Chemin Stockfish (optionnel)", value=os.environ.get("STOCKFISH_PATH", ""))

    if st.button("🔄 Charger mes parties", use_container_width=True):
        st.session_state.pop("games", None)
        st.session_state.pop("analyses", None)
        st.rerun()

    st.divider()
    st.info(
        "Pour une première version gratuite, cette app utilise l'API publique Chess.com "
        "pour récupérer les parties publiques et Stockfish en local."
    )

if not username:
    st.warning("Indique ton pseudo Chess.com.")
    st.stop()

# Load data
if "games" not in st.session_state:
    with st.spinner("Récupération de tes parties (sans le blitz)..."):
        try:
            archives = get_archives(username)
            selected_archives = archives[-months:]
            raw_games = []
            for url in reversed(selected_archives):
                raw_games.extend(get_month_games(url))
            
            filtered_raw_games = [g for g in raw_games if est_une_partie_longue(g)]
            
            parsed = []
            for gd in filtered_raw_games[:max_games]:
                g = parse_game(gd)
                if g:
                    parsed.append(g)
            
            st.session_state["games"] = parsed
            st.session_state["loaded_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            st.error(f"Impossible de récupérer les parties Chess.com : {e}")
            st.stop()

games = st.session_state["games"]
engine = find_engine(engine_path)

if engine is None:
    st.warning(
        "⚠️ Stockfish n'est pas détecté. L'application peut afficher tes parties, "
        "mais l'analyse précise des coups nécessite Stockfish. Voir README.md."
    )

tabs = st.tabs(["🏠 Tableau de bord", "♟️ Mes parties", "🔎 Analyse", "📈 Progression", "🎯 Entraînement"])

# Dashboard
with tabs[0]:
    st.subheader("Ton tableau de bord")
    analyses = st.session_state.get("analyses", {})
    stats = calculate_stats(games, analyses, username)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Parties", stats["games"])
    c2.metric("Victoires", stats["wins"])
    c3.metric("Défaites", stats["losses"])
    c4.metric("Gaffes", stats["blunders"])

    st.markdown(build_coach_summary(stats))

    if games:
        rows = []
        for g in games:
            rows.append({
                "Date": g.headers.get("Date", ""),
                "Blancs": g.headers.get("White", ""),
                "Noirs": g.headers.get("Black", ""),
                "Résultat": outcome_label(result_for_user(g, username)),
                "Ouverture": eco_name(g) or g.headers.get("ECO", ""),
                "Cadence": g.headers.get("TimeControl", ""),
            })
        st.dataframe(pd.DataFrame(rows).head(20), use_container_width=True, hide_index=True)

# Games
with tabs[1]:
    st.subheader("Mes parties")
    if not games:
        st.info("Aucune partie trouvée.")
    else:
        rows = []
        for i, g in enumerate(games):
            rows.append({
                "ID": i,
                "Date": g.headers.get("Date", ""),
                "Blancs": g.headers.get("White", ""),
                "Noirs": g.headers.get("Black", ""),
                "Résultat": outcome_label(result_for_user(g, username)),
                "Ouverture": eco_name(g) or g.headers.get("ECO", ""),
                "Cadence": g.headers.get("TimeControl", ""),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# Analysis
with tabs[2]:
    st.subheader("Analyse d'une partie")
    if not games:
        st.info("Aucune partie.")
    else:
        labels = []
        for i, g in enumerate(games):
            labels.append(
                f"{i} — {g.headers.get('Date','')} — "
                f"{g.headers.get('White','')} vs {g.headers.get('Black','')} — "
                f"{outcome_label(result_for_user(g, username))}"
            )
        selected = st.selectbox("Choisis une partie", range(len(labels)), format_func=lambda i: labels[i])

        if st.button("🧠 Analyser avec Stockfish", type="primary"):
            if engine is None:
                st.error("Stockfish est nécessaire pour cette fonction.")
            else:
                with st.spinner("Analyse en cours…"):
                    result = analyze_game(games[selected], username, engine, depth=depth)
                    st.session_state.setdefault("analyses", {})[selected] = result
                    st.success("Analyse terminée.")

        recs = st.session_state.get("analyses", {}).get(selected)
        if recs:
            st.markdown("### 📊 Fiche de performance")
            note = calculer_note_partie(recs)
            st.metric("Note du Coach", f"{note}/10")
            
            st.markdown("### 🔴 Les 3 moments décisifs")
            severe = sorted(recs, key=lambda x: x['drop'], reverse=True)[:3]
            for r in severe:
                with st.expander(f"Coup {r['move_no']} : {r['category']} (-{r['drop']/100:.2f} pions)"):
                    st.write(f"Tu as joué **{r['san']}**. Le meilleur coup suggéré était **{r['best']}**.")
                    st.write("Conseil : Dans cette position, vérifie bien les menaces directes et les tactiques adverses avant de jouer.")
            
            with st.expander("Voir tout le détail des coups"):
                st.dataframe(pd.DataFrame(recs), use_container_width=True, hide_index=True)
        else:
            st.info("Lance l'analyse pour obtenir la note du coach et les moments clés.")

# Progress
with tabs[3]:
    st.subheader("📈 Progression")
    analyses = st.session_state.get("analyses", {})
    if not analyses:
        st.info("Analyse quelques parties pour commencer à construire ton historique.")
    else:
        timeline = []
        for idx, recs in analyses.items():
            if recs is None:
                continue
            g = games[int(idx)]
            timeline.append({
                "Date": g.headers.get("Date", ""),
                "Gaffes": sum(r["category"] == "Gaffe" for r in recs),
                "Erreurs": sum(r["category"] == "Erreur" for r in recs),
                "Imprécisions": sum(r["category"] == "Imprécision" for r in recs),
            })
        if timeline:
            df = pd.DataFrame(timeline).sort_values("Date")
            st.line_chart(df.set_index("Date"))
            st.dataframe(df, use_container_width=True, hide_index=True)

        st.markdown("### 🎯 Axes de progression actuels")
        st.markdown(
            "- **1. Menaces adverses :** avant chaque décision critique, vérifier les échecs, captures et menaces.\n"
            "- **2. Calcul :** chercher 2–3 coups candidats avant de choisir.\n"
            "- **3. Conversion :** lorsqu'une position est meilleure, identifier le plan le plus simple.\n"
            "- **4. Gestion du temps :** réserver la réflexion longue aux positions réellement critiques."
        )

# Training
with tabs[4]:
    st.subheader("🎯 Programme d'entraînement")
    st.write("Le programme est volontairement simple dans cette V1 ; il sera personnalisé automatiquement lorsque davantage de parties auront été analysées.")

    st.markdown("### Cette semaine")
    st.markdown(
        "1. **10 min/jour — tactique** : motifs en 2–3 coups.\n"
        "2. **2 parties en 10+5** : objectif = ne jamais jouer automatiquement dans une position tactique.\n"
        "3. **15 min après chaque partie** : retrouver sans moteur le premier moment où le plan a changé.\n"
        "4. **1 finale** : travailler une finale de pions ou de tours."
    )

    st.markdown("### 🧑‍🏫 Règle du coach")
    st.info(
        "Je préfère que tu comprennes pourquoi tu as perdu une position plutôt que de mémoriser "
        "le coup que Stockfish aurait joué."
    )

st.divider()
st.caption(
    "Prototype V1 — Stockfish fournit l'évaluation objective ; la couche Coach fournit l'interprétation pédagogique."
)
