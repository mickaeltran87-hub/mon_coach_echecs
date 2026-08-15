import io
import os
from collections import Counter
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
    r = requests.get(url, headers={"User-Agent": "MonCoachEchecs/1.0 personal-training-app"}, timeout=20)
    r.raise_for_status()
    return r.json()

def get_archives(username):
    data = api_get(f"{CHESS_API}/player/{username}/games/archives")
    return data.get("archives", [])

def get_month_games(archive_url):
    data = api_get(archive_url)
    return data.get("games", [])

def parse_game(game_dict):
    pgn_text = game_dict.get("pgn")
    if not pgn_text: return None
    return chess.pgn.read_game(io.StringIO(pgn_text))

def result_for_user(game, username):
    headers = game.headers
    white, black = headers.get("White", "").lower(), headers.get("Black", "").lower()
    result = headers.get("Result", "*")
    if username.lower() == white: return result
    if username.lower() == black: return {"1-0": "0-1", "0-1": "1-0"}.get(result, result)
    return result

def user_color(game, username):
    if game.headers.get("White", "").lower() == username.lower(): return chess.WHITE
    if game.headers.get("Black", "").lower() == username.lower(): return chess.BLACK
    return None

def outcome_label(result):
    return {"1-0": "Victoire", "0-1": "Défaite", "1/2-1/2": "Nulle", "*": "En cours"}.get(result, result)

def est_une_partie_longue(game_dict):
    try:
        seconds = int(game_dict.get("time_control", "").split('+')[0])
        return seconds >= 600
    except: return False

# -----------------------------
# Stockfish & Analysis
# -----------------------------

@st.cache_resource
def find_engine(path_hint=""):
    candidates = [path_hint, os.environ.get("STOCKFISH_PATH", ""), "/usr/games/stockfish", "/usr/bin/stockfish", "stockfish"]
    for p in candidates:
        if p:
            try: return chess.engine.SimpleEngine.popen_uci(p)
            except: continue
    return None

def score_cp(score, pov):
    s = score.pov(pov)
    if s.is_mate(): return 100000 if s.mate() > 0 else -100000
    return s.score(mate_score=100000)

def classify_drop(drop):
    if drop >= 250: return "Gaffe"
    if drop >= 100: return "Erreur"
    if drop >= 50: return "Imprécision"
    return "OK"

def analyze_game(game, username, engine, depth=12):
    if engine is None: return None
    u_color = user_color(game, username)
    records = []
    board = game.board()
    for move in game.mainline_moves():
        mover = board.turn
        info_before = engine.analyse(board, chess.engine.Limit(depth=depth))
        eval_before = score_cp(info_before["score"], u_color)
        san = board.san(move)
        board.push(move)
        info_after = engine.analyse(board, chess.engine.Limit(depth=depth))
        eval_after = score_cp(info_after["score"], u_color)
        if mover == u_color:
            drop = max(0, eval_before - eval_after)
            records.append({"move_no": len(records)+1, "san": san, "drop": round(drop, 1), "category": classify_drop(drop)})
    return records

def calculer_note_partie(recs):
    if not recs: return 10
    total_drop = sum(r['drop'] for r in recs)
    return round(max(0, 10 - (total_drop / 300)), 1)

# -----------------------------
# UI
# -----------------------------

st.title(APP_TITLE)
username = st.sidebar.text_input("Pseudo Chess.com", value=st.session_state.get("username", "gerardmansoif"))
st.session_state["username"] = username.strip()

if st.sidebar.button("🔄 Charger mes parties"):
    st.session_state.clear()
    st.rerun()

if "games" not in st.session_state and username:
    with st.spinner("Récupération..."):
        archives = get_archives(username)
        raw = []
        for url in reversed(archives[-3:]):
            raw.extend([g for g in get_month_games(url) if est_une_partie_longue(g)])
        st.session_state["games"] = [g for g in [parse_game(r) for r in raw[:50]] if g]

games = st.session_state.get("games", [])
engine = find_engine()

tabs = st.tabs(["🏠 Tableau de bord", "🔎 Analyse"])

with tabs[0]:
    st.metric("Parties longues chargées", len(games))
    if games: st.write(pd.DataFrame([{"Date": g.headers.get("Date"), "Res": outcome_label(result_for_user(g, username))} for g in games]))

with tabs[1]:
    if games:
        selected = st.selectbox("Choisis une partie", range(len(games)), format_func=lambda i: f"Partie {i+1} - {games[i].headers.get('Date')}")
        if st.button("🧠 Lancer l'analyse du coach"):
            st.session_state["curr_analysis"] = analyze_game(games[selected], username, engine)
        
        recs = st.session_state.get("curr_analysis")
        if recs:
            st.metric("Note du Coach", f"{calculer_note_partie(recs)}/10")
            st.subheader("🔴 Moments décisifs")
            for r in sorted(recs, key=lambda x: x['drop'], reverse=True)[:3]:
                st.write(f"**Coup {r['move_no']}** : {r['category']} (-{r['drop']/100} pions)")
