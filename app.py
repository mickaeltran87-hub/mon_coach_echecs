# -*- coding: utf-8 -*-
"""
Mon Coach d'Échecs — v3
------------------------
Dépendances : streamlit, python-chess (chess), pandas, requests
    pip install streamlit chess pandas requests

Nouveautés v3 :
- Les recommandations (vidéos YouTube + thème prioritaire) ne sont plus basées
  sur le cumul depuis toujours, mais sur une fenêtre récente comparée à la
  période précédente : si tu progresses sur un point et régresses sur un
  autre, les recommandations suivent.
- Puzzles Lichess (API officielle /api/puzzle/next) ciblés automatiquement
  sur ton thème faible actuel, en complément (pas en remplacement) des
  puzzles issus de tes propres gaffes et des liens YouTube existants.
- Analyse des résultats selon l'écart de classement avec l'adversaire.
- Nouvel onglet "Rapport hebdo" : bilan auto-généré de la semaine en cours
  comparée à la précédente (parties, classement, note, gaffes, thème du
  moment), avec une version texte copiable.
- Thèmes d'erreur plus précis : la meilleure réponse adverse calculée par
  Stockfish (et pas seulement la position) sert désormais à distinguer
  "fourchette subie", "clouage subi", "mat forcé permis" et "pièce déjà en
  prise ignorée" (vs. "mise en prise par ce coup"). Les catégories
  générales (Gaffe tactique majeure, Erreur de calcul, Imprécision
  positionnelle) restent le filet de sécurité pour ce que ces heuristiques
  n'identifient pas — une classification exhaustive de tous les motifs
  tactiques nommés (clouage, déviation, x-ray, zugzwang...) demanderait un
  moteur de reconnaissance dédié, ce qui dépasse ce script.
- Copie en un clic (bouton HTML + JS) pour l'export Gemini et le rapport
  hebdomadaire, avec repli manuel discret si le presse-papiers du
  navigateur est bloqué.

Nouveautés v2 (rappel) :
- Historique persistant en base SQLite locale (coach_echecs_history.db) :
  toutes les parties chargées ET toutes les analyses Stockfish sont conservées
  d'une session à l'autre, pour un vrai suivi de progression long terme.
- Réutilisation automatique d'une analyse déjà enregistrée (évite de refaire
  tourner Stockfish sur une partie déjà analysée à profondeur suffisante).
- Suivi du classement Chess.com (extrait de chaque partie) dans le temps.
- Détection des gaffes liées au zeitnot (utilise les annotations d'horloge
  déjà présentes dans les PGN Chess.com).
- Thème "Roi exposé" désormais réellement détecté.
- Note de partie recalculée en perte moyenne (ACPL) plutôt qu'en perte totale,
  pour ne plus pénaliser injustement les parties longues.
- Export CSV et réinitialisation de l'historique.

Important : le "rapport hebdomadaire" est calculé en direct à chaque fois que
tu ouvres l'onglet (pas d'envoi automatique par email/notification — ce
script n'est pas un service qui tourne en arrière-plan).
"""

import io
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import chess
import chess.pgn
import chess.engine
import chess.svg
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as st_components

APP_TITLE = "♟️ Mon Coach d'Échecs"
CHESS_API = "https://api.chess.com/pub"
DB_PATH = Path(__file__).resolve().parent / "coach_echecs_history.db"
CLK_RE = re.compile(r"\[%clk\s+([0-9:.]+)\]")

st.set_page_config(page_title=APP_TITLE, page_icon="♟️", layout="wide")

# ==========================================================
# Base de données (historique persistant long terme)
# ==========================================================

@st.cache_resource
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT NOT NULL,
            username TEXT NOT NULL,
            end_time INTEGER,
            date TEXT,
            white TEXT,
            black TEXT,
            result TEXT,
            user_color TEXT,
            user_rating INTEGER,
            opp_rating INTEGER,
            eco TEXT,
            opening TEXT,
            time_control TEXT,
            pgn TEXT,
            first_seen_at TEXT,
            PRIMARY KEY (game_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            game_id TEXT NOT NULL,
            username TEXT NOT NULL,
            depth INTEGER,
            analyzed_at TEXT,
            note REAL,
            acpl REAL,
            blunders INTEGER,
            errors INTEGER,
            inaccuracies INTEGER,
            total_moves INTEGER,
            zeitnot_ratio REAL,
            details_json TEXT,
            PRIMARY KEY (game_id, username)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS puzzle_reviews (
            username TEXT NOT NULL,
            game_id TEXT NOT NULL,
            ply INTEGER NOT NULL,
            box INTEGER DEFAULT 1,
            next_review TEXT,
            last_result TEXT,
            review_count INTEGER DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (username, game_id, ply)
        )
    """)
    conn.commit()


def upsert_game(username, game_id, meta, game):
    if not game_id:
        return
    conn = get_db_connection()
    conn.execute("""
        INSERT INTO games (game_id, username, end_time, date, white, black, result,
                            user_color, user_rating, opp_rating, eco, opening,
                            time_control, pgn, first_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(game_id, username) DO UPDATE SET
            end_time=excluded.end_time,
            date=excluded.date,
            result=excluded.result,
            user_rating=excluded.user_rating,
            opp_rating=excluded.opp_rating
    """, (
        game_id, username, meta.get("end_time"), game.headers.get("Date", ""),
        game.headers.get("White", ""), game.headers.get("Black", ""),
        result_for_user(game, username), meta.get("user_color", ""),
        meta.get("user_rating"), meta.get("opp_rating"),
        game.headers.get("ECO", ""), eco_name(game), game.headers.get("TimeControl", ""),
        str(game), datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()


def save_analysis(username, game_id, depth, recs):
    if not game_id:
        return
    conn = get_db_connection()
    note = calculer_note_partie(recs)
    acpl = round(sum(r["drop"] for r in recs) / len(recs), 1) if recs else 0.0
    blunders = sum(1 for r in recs if r["category"] == "Gaffe")
    errors = sum(1 for r in recs if r["category"] == "Erreur")
    inaccuracies = sum(1 for r in recs if r["category"] == "Imprécision")
    zeitnot = sum(1 for r in recs if r.get("time_pressure"))
    zeitnot_ratio = round(zeitnot / len(recs), 2) if recs else 0.0
    conn.execute("""
        INSERT INTO analyses (game_id, username, depth, analyzed_at, note, acpl, blunders,
                               errors, inaccuracies, total_moves, zeitnot_ratio, details_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(game_id, username) DO UPDATE SET
            depth=excluded.depth, analyzed_at=excluded.analyzed_at, note=excluded.note,
            acpl=excluded.acpl, blunders=excluded.blunders, errors=excluded.errors,
            inaccuracies=excluded.inaccuracies, total_moves=excluded.total_moves,
            zeitnot_ratio=excluded.zeitnot_ratio, details_json=excluded.details_json
        WHERE excluded.depth >= analyses.depth
    """, (
        game_id, username, depth, datetime.now(timezone.utc).isoformat(), note, acpl,
        blunders, errors, inaccuracies, len(recs), zeitnot_ratio, json.dumps(recs),
    ))
    conn.commit()


def get_cached_analysis(username, game_id, min_depth):
    if not game_id:
        return None
    conn = get_db_connection()
    row = conn.execute(
        "SELECT depth, details_json FROM analyses WHERE username=? AND game_id=?",
        (username, game_id),
    ).fetchone()
    if row and row[0] is not None and row[0] >= min_depth:
        return json.loads(row[1])
    return None


def load_history_df(username):
    conn = get_db_connection()
    query = """
        SELECT g.game_id, g.end_time, g.date, g.white, g.black, g.result, g.user_color,
               g.user_rating, g.opp_rating, g.opening, g.time_control,
               a.note, a.acpl, a.blunders, a.errors, a.inaccuracies, a.total_moves,
               a.zeitnot_ratio, a.analyzed_at
        FROM games g
        LEFT JOIN analyses a ON g.game_id = a.game_id AND g.username = a.username
        WHERE g.username = ?
        ORDER BY g.end_time ASC
    """
    return pd.read_sql_query(query, conn, params=(username,))


def load_all_games_from_db(username):
    conn = get_db_connection()
    rows = conn.execute("SELECT pgn FROM games WHERE username=? ORDER BY end_time ASC",
                         (username,)).fetchall()
    out = []
    for (pgn_text,) in rows:
        g = chess.pgn.read_game(io.StringIO(pgn_text))
        if g:
            out.append(g)
    return out


def load_all_puzzles(username, limit=300):
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT g.game_id, g.date, g.white, g.black, a.details_json
        FROM analyses a JOIN games g ON a.game_id = g.game_id AND a.username = g.username
        WHERE a.username = ?
        ORDER BY g.end_time DESC
        LIMIT ?
    """, (username, limit)).fetchall()
    puzzles = []
    for game_id, date, white, black, dj in rows:
        try:
            recs = json.loads(dj)
        except Exception:
            continue
        for r in recs:
            if r["category"] in ("Gaffe", "Erreur") and r.get("best"):
                entry = dict(r)
                entry.update({"game_id": game_id, "date": date, "white": white, "black": black})
                puzzles.append(entry)
    return puzzles


def load_game_pgn_and_color(username, game_id):
    """Renvoie (texte pgn, couleur de l'utilisateur 'white'/'black') pour une partie donnée."""
    conn = get_db_connection()
    row = conn.execute("SELECT pgn, user_color FROM games WHERE username=? AND game_id=?",
                        (username, game_id)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def board_before_ply(pgn_text, ply):
    """Reconstruit la position juste avant le coup n°`ply` (1-indexé, dans
    l'ordre du fil principal de la partie) et renvoie (position, coup joué)."""
    if not pgn_text:
        return None, None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None, None
    moves = list(game.mainline_moves())
    idx = ply - 1
    if idx < 0 or idx >= len(moves):
        return None, None
    board = game.board()
    for m in moves[:idx]:
        board.push(m)
    return board, moves[idx]


def render_puzzle_board(board, user_color_str, best_san=None):
    """Affiche un échiquier SVG (via python-chess), orienté du point de vue
    de l'utilisateur, avec une flèche verte sur le meilleur coup si fourni."""
    if board is None:
        return
    orientation = chess.BLACK if user_color_str == "black" else chess.WHITE
    arrows = []
    if best_san:
        try:
            mv = board.parse_san(best_san)
            arrows.append(chess.svg.Arrow(mv.from_square, mv.to_square, color="#15781Bcc"))
        except Exception:
            pass
    svg = chess.svg.board(board, size=360, orientation=orientation, arrows=arrows)
    st_components.html(svg, height=390)


# -----------------------------
# Révision espacée des puzzles personnels (méthode Leitner)
# -----------------------------

LEITNER_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}  # jours avant la prochaine révision


def load_puzzle_review_states(username):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT game_id, ply, box, next_review, review_count FROM puzzle_reviews WHERE username=?",
        (username,),
    ).fetchall()
    return {(game_id, ply): {"box": box, "next_review": next_review, "review_count": review_count}
            for game_id, ply, box, next_review, review_count in rows}


def record_review(username, game_id, ply, correct):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT box FROM puzzle_reviews WHERE username=? AND game_id=? AND ply=?",
        (username, game_id, ply),
    ).fetchone()
    box = row[0] if row else 1
    box = min(box + 1, 5) if correct else 1
    next_review = (datetime.now(timezone.utc) + timedelta(days=LEITNER_INTERVALS[box])).isoformat()
    conn.execute("""
        INSERT INTO puzzle_reviews (username, game_id, ply, box, next_review, last_result, review_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(username, game_id, ply) DO UPDATE SET
            box=excluded.box, next_review=excluded.next_review, last_result=excluded.last_result,
            review_count=puzzle_reviews.review_count + 1, updated_at=excluded.updated_at
    """, (username, game_id, ply, box, next_review, "correct" if correct else "incorrect",
          datetime.now(timezone.utc).isoformat()))
    conn.commit()


def load_due_puzzles(username, limit=300):
    """Renvoie (puzzles à réviser aujourd'hui, tous les puzzles enrichis avec
    leur boîte Leitner), triés boîte croissante puis date."""
    puzzles = load_all_puzzles(username, limit=limit)
    review_states = load_puzzle_review_states(username)
    now = pd.Timestamp.now(tz="UTC")

    enriched = []
    for p in puzzles:
        key = (p["game_id"], p["ply"])
        rev = review_states.get(key)
        if rev is None:
            box, review_count, due = 1, 0, True
        else:
            box = rev["box"]
            review_count = rev["review_count"]
            try:
                due = pd.Timestamp(rev["next_review"]) <= now
            except Exception:
                due = True
        p2 = dict(p)
        p2["box"] = box
        p2["review_count"] = review_count
        p2["due"] = due
        enriched.append(p2)

    due_puzzles = [p for p in enriched if p["due"]]
    due_puzzles.sort(key=lambda p: (p["box"], p.get("date", "")))
    return due_puzzles, enriched


def lifetime_stats(username):
    conn = get_db_connection()
    row = conn.execute("""
        SELECT COUNT(*) AS n_games,
               SUM(CASE WHEN a.game_id IS NOT NULL THEN 1 ELSE 0 END) AS n_analyzed,
               AVG(a.note) AS avg_note,
               SUM(a.blunders) AS total_blunders,
               SUM(a.errors) AS total_errors
        FROM games g LEFT JOIN analyses a ON g.game_id = a.game_id AND g.username = a.username
        WHERE g.username = ?
    """, (username,)).fetchone()
    return {
        "n_games": row[0] or 0,
        "n_analyzed": row[1] or 0,
        "avg_note": round(row[2], 2) if row[2] is not None else None,
        "total_blunders": row[3] or 0,
        "total_errors": row[4] or 0,
    }


def clear_history(username):
    conn = get_db_connection()
    conn.execute("DELETE FROM analyses WHERE username=?", (username,))
    conn.execute("DELETE FROM games WHERE username=?", (username,))
    conn.commit()


def compute_datetime(row):
    if pd.notna(row.get("end_time")):
        try:
            return pd.Timestamp(int(row["end_time"]), unit="s", tz="UTC")
        except Exception:
            pass
    try:
        return pd.Timestamp(datetime.strptime(row.get("date", ""), "%Y.%m.%d"), tz="UTC")
    except Exception:
        return pd.NaT


def rating_gap_bucket(diff):
    """Classe un écart de classement (toi - adversaire) en catégorie lisible."""
    if diff is None or pd.isna(diff):
        return None
    if diff <= -100:
        return "1. Bien plus fort que moi (≤ -100)"
    if diff <= -20:
        return "2. Plus fort que moi (-100 à -20)"
    if diff < 20:
        return "3. Niveau équivalent (-20 à +20)"
    if diff < 100:
        return "4. Plus faible que moi (+20 à +100)"
    return "5. Bien plus faible que moi (≥ +100)"


# -----------------------------
# Tendance des thèmes (fenêtre récente vs période précédente)
# -----------------------------

def load_theme_batches(username, recent_n=10):
    """Renvoie (lignes récentes, lignes précédentes) triées de la plus
    récente à la plus ancienne, chaque ligne = (end_time, details_json)."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT g.end_time, a.details_json
        FROM analyses a JOIN games g ON a.game_id = g.game_id AND a.username = g.username
        WHERE a.username = ?
        ORDER BY g.end_time DESC
    """, (username,)).fetchall()
    recent_rows = rows[:recent_n]
    previous_rows = rows[recent_n:recent_n * 2]
    return recent_rows, previous_rows


def theme_counter_from_rows(rows):
    counter = Counter()
    for _, dj in rows:
        try:
            recs = json.loads(dj)
        except Exception:
            continue
        for r in recs:
            if r["category"] in ("Gaffe", "Erreur"):
                counter[r.get("theme", "Non classé")] += 1
    return counter


def compute_theme_trends(username, recent_n=10):
    """Compare la fréquence de chaque thème d'erreur sur les recent_n
    dernières parties analysées par rapport aux recent_n parties précédentes.
    Renvoie (DataFrame de tendances, nb parties récentes, nb parties
    précédentes, a_comparaison_valide)."""
    recent_rows, previous_rows = load_theme_batches(username, recent_n)
    recent_counter = theme_counter_from_rows(recent_rows)
    previous_counter = theme_counter_from_rows(previous_rows)

    n_recent = max(1, len(recent_rows))
    n_previous = max(1, len(previous_rows))
    has_comparison = len(previous_rows) >= max(3, recent_n // 2)

    themes = set(recent_counter) | set(previous_counter)
    data = []
    for theme in themes:
        recent_rate = recent_counter.get(theme, 0) / n_recent
        previous_rate = previous_counter.get(theme, 0) / n_previous if has_comparison else None
        delta = round(recent_rate - previous_rate, 2) if previous_rate is not None else None
        data.append({
            "theme": theme,
            "recent_count": recent_counter.get(theme, 0),
            "recent_rate": round(recent_rate, 2),
            "previous_rate": round(previous_rate, 2) if previous_rate is not None else None,
            "delta": delta,
        })

    df = pd.DataFrame(data)
    if not df.empty:
        if has_comparison:
            df["priority"] = df["recent_rate"] + df["delta"].fillna(0).clip(lower=0) * 0.5
        else:
            df["priority"] = df["recent_rate"]
        df = df.sort_values("priority", ascending=False).reset_index(drop=True)

    return df, len(recent_rows), len(previous_rows), has_comparison


def format_trend(delta):
    if delta is None:
        return "— (pas encore de comparaison)"
    if delta > 0.15:
        return f"↗️ en régression (+{delta:.2f}/partie)"
    if delta < -0.15:
        return f"↘️ en progrès ({delta:.2f}/partie)"
    return "➡️ stable"


# -----------------------------
# Rapport hebdomadaire
# -----------------------------

def _summarize_period(df):
    n = len(df)
    wins = int((df["result"] == "1-0").sum())
    losses = int((df["result"] == "0-1").sum())
    draws = int((df["result"] == "1/2-1/2").sum())
    analyzed_mask = df["note"].notna()
    analyzed = int(analyzed_mask.sum())
    avg_note = round(df.loc[analyzed_mask, "note"].mean(), 2) if analyzed else None
    blunders = int(df.loc[analyzed_mask, "blunders"].sum()) if analyzed else 0
    errors = int(df.loc[analyzed_mask, "errors"].sum()) if analyzed else 0
    rating_series = df.dropna(subset=["user_rating"]).sort_values("DateJeu")["user_rating"]
    rating_start = rating_series.iloc[0] if not rating_series.empty else None
    rating_end = rating_series.iloc[-1] if not rating_series.empty else None
    return {
        "n": n, "wins": wins, "losses": losses, "draws": draws,
        "analyzed": analyzed, "avg_note": avg_note, "blunders": blunders, "errors": errors,
        "rating_start": rating_start, "rating_end": rating_end,
    }


def _top_theme_for_period(username, df):
    conn = get_db_connection()
    counter = Counter()
    for game_id in df["game_id"]:
        row = conn.execute(
            "SELECT details_json FROM analyses WHERE username=? AND game_id=?",
            (username, game_id),
        ).fetchone()
        if not row or not row[0]:
            continue
        try:
            recs = json.loads(row[0])
        except Exception:
            continue
        for r in recs:
            if r["category"] in ("Gaffe", "Erreur"):
                counter[r.get("theme", "Non classé")] += 1
    return counter.most_common(1)[0][0] if counter else None


def weekly_report(username):
    hist = load_history_df(username)
    if hist.empty:
        return None

    hist["DateJeu"] = hist.apply(compute_datetime, axis=1)
    hist = hist.dropna(subset=["DateJeu"])
    if hist.empty:
        return None

    now = pd.Timestamp.now(tz="UTC")
    week_start = now - pd.Timedelta(days=7)
    prev_week_start = now - pd.Timedelta(days=14)

    this_week = hist[hist["DateJeu"] >= week_start]
    last_week = hist[(hist["DateJeu"] >= prev_week_start) & (hist["DateJeu"] < week_start)]

    cur = _summarize_period(this_week)
    prev = _summarize_period(last_week)
    top_theme_week = _top_theme_for_period(username, this_week) if not this_week.empty else None

    return {
        "cur": cur, "prev": prev, "top_theme_week": top_theme_week,
        "period_start": week_start, "period_end": now, "hist": hist,
    }


def build_weekly_narrative(cur, prev, top_theme):
    phrases = []
    if cur["n"] == 0:
        return "Aucune partie jouée cette semaine — pas de quoi s'inquiéter, mais pas de données à commenter non plus."

    phrases.append(
        f"Cette semaine : **{cur['n']} partie(s)** jouée(s) ({cur['wins']}V / {cur['draws']}N / {cur['losses']}D), "
        f"contre {prev['n']} la semaine précédente."
    )

    if cur["rating_start"] is not None and cur["rating_end"] is not None:
        delta_rating = cur["rating_end"] - cur["rating_start"]
        if delta_rating > 0:
            phrases.append(f"Ton classement a progressé de **+{delta_rating} points** sur la semaine.")
        elif delta_rating < 0:
            phrases.append(f"Ton classement a reculé de **{delta_rating} points** sur la semaine.")
        else:
            phrases.append("Ton classement est resté stable sur la semaine.")

    if cur["analyzed"] and prev["analyzed"] and cur["avg_note"] is not None and prev["avg_note"] is not None:
        delta_note = round(cur["avg_note"] - prev["avg_note"], 2)
        if delta_note > 0.3:
            phrases.append(f"🟢 Ta note moyenne s'améliore ({prev['avg_note']} → {cur['avg_note']}) — continue comme ça.")
        elif delta_note < -0.3:
            phrases.append(f"🟠 Ta note moyenne baisse un peu ({prev['avg_note']} → {cur['avg_note']}), rien d'alarmant sur une semaine.")
        else:
            phrases.append(f"Ta note moyenne est stable ({cur['avg_note']}/10).")
    elif cur["analyzed"]:
        phrases.append(f"Note moyenne de la semaine : **{cur['avg_note']}/10** sur {cur['analyzed']} partie(s) analysée(s).")
    else:
        phrases.append("Aucune partie analysée par Stockfish cette semaine — pense à passer par l'onglet Analyse pour affiner ce bilan.")

    if top_theme:
        phrases.append(f"🎯 Le point à travailler en priorité cette semaine : **{top_theme}**.")

    return "\n\n".join(phrases)


def generate_weekly_report_text(username, cur, prev, top_theme, period_start, period_end):
    lignes = [
        f"Rapport hebdomadaire — {username}",
        f"Période : {period_start.date()} au {period_end.date()}",
        "",
        f"Parties jouées : {cur['n']} (semaine précédente : {prev['n']})",
        f"Score : {cur['wins']}V / {cur['draws']}N / {cur['losses']}D",
    ]
    if cur["rating_end"] is not None:
        lignes.append(f"Classement en fin de semaine : {cur['rating_end']}")
    if cur["avg_note"] is not None:
        lignes.append(f"Note moyenne (Stockfish) : {cur['avg_note']}/10 sur {cur['analyzed']} partie(s) analysée(s)")
        lignes.append(f"Gaffes : {cur['blunders']} — Erreurs : {cur['errors']}")
    if top_theme:
        lignes.append(f"Thème prioritaire de la semaine : {top_theme}")
    return "\n".join(lignes)


# -----------------------------
# Chess.com API
# -----------------------------

def api_get(url):
    r = requests.get(
        url,
        headers={"User-Agent": "MonCoachEchecs/2.0 personal-training-app"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def get_archives(username):
    data = api_get(f"{CHESS_API}/player/{username}/games/archives")
    return data.get("archives", [])


def get_month_games(archive_url):
    data = api_get(archive_url)
    return data.get("games", [])


def get_player_stats(username):
    return api_get(f"{CHESS_API}/player/{username}/stats")


# -----------------------------
# Puzzles Lichess (API officielle, en complément des vidéos et de tes puzzles)
# -----------------------------

LICHESS_PUZZLE_API = "https://lichess.org/api/puzzle/next"

# Correspondance entre nos thèmes internes (français) et les "angle" reconnus
# par l'API Lichess (https://lichess.org/api#tag/Puzzles/operation/apiPuzzleNext).
THEME_TO_LICHESS_ANGLE = {
    "Pièce suspendue (non protégée)": "hangingPiece",
    "Roi exposé / Échec subi": "exposedKing",
    "Tactique ou capture ratée": "advantage",
    "Sortie de Dame précoce / Perte de tempo": "opening",
    "Précipitation (zeitnot)": "short",
    "Gaffe tactique majeure": "crushing",
    "Erreur de calcul / Structure": "middlegame",
    "Imprécision positionnelle": "middlegame",
    "Fourchette subie": "fork",
    "Clouage subi": "pin",
    "A permis un mat forcé": "defensiveMove",
    "N'a pas paré une pièce déjà en prise": "hangingPiece",
}


@st.cache_data(ttl=3600, show_spinner=False)
def get_lichess_puzzles_for_theme(theme, n=3, difficulty="normal"):
    """Va chercher n puzzles Lichess correspondant au thème donné.
    Résultat mis en cache 1h pour éviter de solliciter l'API à chaque rerun."""
    angle = THEME_TO_LICHESS_ANGLE.get(theme, "middlegame")
    puzzles = []
    seen_ids = set()
    for _ in range(n * 3):
        if len(puzzles) >= n:
            break
        try:
            data = api_get(f"{LICHESS_PUZZLE_API}?angle={angle}&difficulty={difficulty}")
        except Exception:
            break
        puzzle = (data or {}).get("puzzle", {})
        pid = puzzle.get("id")
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        puzzles.append({
            "id": pid,
            "rating": puzzle.get("rating"),
            "themes": puzzle.get("themes", []),
            "url": f"https://lichess.org/training/{pid}",
        })
    return puzzles


# -----------------------------
# Écarts à la théorie d'ouverture (API Opening Explorer de Lichess)
# -----------------------------

LICHESS_EXPLORER_API = "https://explorer.lichess.org/lichess"


@st.cache_data(ttl=86400, show_spinner=False)
def get_explorer_stats(fen, speeds="rapid", top_n=8):
    """Interroge la base Lichess (parties à la cadence donnée) pour une
    position. Mis en cache 24h par position : les débuts de partie se
    répètent beaucoup, donc peu d'appels réels après les premières parties."""
    try:
        r = requests.get(
            LICHESS_EXPLORER_API,
            params={"variant": "standard", "fen": fen, "speeds": speeds, "moves": top_n},
            headers={"User-Agent": "MonCoachEchecs/3.0 personal-training-app"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def most_recent_games(games_list, n):
    """Trie une liste de chess.pgn.Game par date (en-tête PGN) et renvoie
    les n plus récentes, quel que soit l'ordre de la liste d'origine."""
    def game_date(g):
        try:
            return datetime.strptime(g.headers.get("Date", ""), "%Y.%m.%d")
        except Exception:
            return datetime.min
    sorted_games = sorted(games_list, key=game_date)
    return sorted_games[-n:] if len(sorted_games) > n else sorted_games


def find_opening_deviations(username, games_list, max_ply=20, min_games_threshold=5, speeds="rapid"):
    """Pour chaque partie, repère le premier coup de l'utilisateur qui ne
    figure pas parmi les coups les plus joués selon la base Lichess (à la
    cadence donnée) — utile pour voir où s'arrête la préparation connue.
    Renvoie aussi un indicateur si l'API n'a pas pu être contactée du tout."""
    results = []
    api_reachable = False
    for game in games_list:
        u_color = user_color(game, username)
        if u_color is None:
            continue
        board = game.board()
        deviation = None
        for ply, move in enumerate(game.mainline_moves()):
            if ply >= max_ply:
                break
            mover = board.turn
            if mover == u_color:
                fen = board.fen()
                data = get_explorer_stats(fen, speeds=speeds)
                if data is not None:
                    api_reachable = True
                if data and data.get("moves"):
                    total_games = sum(m.get("white", 0) + m.get("draws", 0) + m.get("black", 0)
                                       for m in data["moves"])
                    top_ucis = {m["uci"] for m in data["moves"]}
                    if total_games >= min_games_threshold and move.uci() not in top_ucis:
                        deviation = {
                            "ply": ply, "move_no": ply // 2 + 1, "san_joue": board.san(move),
                            "alternatives": [m["san"] for m in data["moves"][:3]],
                        }
                        break
            board.push(move)
        if deviation:
            results.append({
                "date": game.headers.get("Date", ""),
                "white": game.headers.get("White", ""),
                "black": game.headers.get("Black", ""),
                **deviation,
            })
    return results, api_reachable


def parse_game(game_dict):
    pgn_text = game_dict.get("pgn")
    if not pgn_text:
        return None
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    return game


def extract_meta(gd, username):
    user = username.strip().lower()
    white_username = (gd.get("white") or {}).get("username", "").lower()
    black_username = (gd.get("black") or {}).get("username", "").lower()
    if user == white_username:
        color = "white"
        user_rating = (gd.get("white") or {}).get("rating")
        opp_rating = (gd.get("black") or {}).get("rating")
    elif user == black_username:
        color = "black"
        user_rating = (gd.get("black") or {}).get("rating")
        opp_rating = (gd.get("white") or {}).get("rating")
    else:
        color, user_rating, opp_rating = "", None, None
    return {
        "game_id": gd.get("url") or gd.get("uuid"),
        "end_time": gd.get("end_time"),
        "user_color": color,
        "user_rating": user_rating,
        "opp_rating": opp_rating,
    }


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
    white = game.headers.get("White", "").strip().lower()
    black = game.headers.get("Black", "").strip().lower()
    user = username.strip().lower()
    if user in white:
        return chess.WHITE
    if user in black:
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


def clock_from_comment(comment):
    if not comment:
        return None
    m = CLK_RE.search(comment)
    if not m:
        return None
    return parse_clock_seconds(m.group(1))


def format_seconds(s):
    if s is None:
        return "—"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"


def parse_tc_base_seconds(time_control):
    try:
        base = time_control.split("+")[0].split(":")[0] if time_control else ""
        return int(base)
    except (ValueError, AttributeError, IndexError):
        return None


def est_une_partie_longue(game_dict):
    """Ne garde que les cadences de réflexion (>= 10 minutes de base)."""
    base = parse_tc_base_seconds(game_dict.get("time_control", ""))
    if base is None:
        return True
    return base >= 600


def extraire_stats_ouvertures(parties_pgn, pseudo):
    """Statistiques victoire/nulle/défaite par ouverture pour un joueur donné."""
    donnees = []
    for partie in parties_pgn:
        headers = partie.headers
        eco = headers.get("ECO", "Inconnu")
        eco_url = headers.get("ECOUrl", "")
        nom_ouverture = eco_url.split("/")[-1].replace("-", " ") if eco_url else eco

        blanc = headers.get("White", "").lower()
        noir = headers.get("Black", "").lower()
        resultat = headers.get("Result", "*")
        pseudo_min = pseudo.lower()

        if pseudo_min == blanc:
            couleur = "Blancs"
            if resultat == "1-0": issue = "Victoire"
            elif resultat == "0-1": issue = "Défaite"
            elif resultat == "1/2-1/2": issue = "Nulle"
            else: continue
        elif pseudo_min == noir:
            couleur = "Noirs"
            if resultat == "0-1": issue = "Victoire"
            elif resultat == "1-0": issue = "Défaite"
            elif resultat == "1/2-1/2": issue = "Nulle"
            else: continue
        else:
            continue

        donnees.append({"Couleur": couleur, "Ouverture": nom_ouverture, "Résultat": issue})

    return pd.DataFrame(donnees)


def generate_coach_prompt(game, recs=None):
    """Génère un texte formaté pour poursuivre l'analyse pédagogique."""
    white = game.headers.get("White", "Inconnu")
    black = game.headers.get("Black", "Inconnu")
    date = game.headers.get("Date", "Inconnu")
    pgn_str = str(game)
    rapport = recs if recs else "Aucune analyse Stockfish détaillée pour le moment."

    lignes = [
        "Voici ma partie d'échecs pour une analyse pédagogique :", "",
        "--- INFOS PARTIE ---",
        f"- Date : {date}", f"- Blancs : {white}", f"- Noirs : {black}", "",
        "--- RAPPORT STOCKFISH ---", str(rapport), "",
        "--- PGN ---", pgn_str, "",
        "--- TA MISSION ---",
        "Peux-tu analyser cette partie ? Concentre-toi sur les moments clés et les erreurs.",
        "Explique-moi le \"pourquoi\" stratégique et aide-moi à progresser.",
    ]
    return "\n".join(lignes)


# -----------------------------
# Copie en un clic (sans étape de sélection manuelle)
# -----------------------------

def copy_button(text, label="📋 Copier dans le presse-papiers", key="default"):
    """Bouton HTML autonome : copie `text` dans le presse-papiers au clic,
    sans passer par une sélection manuelle. Essaie l'API Clipboard moderne,
    puis se rabat sur document.execCommand('copy') si elle est indisponible
    (contexte iframe, permissions du navigateur, etc.)."""
    text_js = json.dumps(text)
    safe_key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(key))
    html_code = f"""
    <div style="font-family: 'Source Sans Pro', sans-serif;">
      <button id="copy-btn-{safe_key}" style="
          background-color:#FF4B4B;color:white;border:none;border-radius:8px;
          padding:0.5em 1em;font-size:0.9em;cursor:pointer;">
        {label}
      </button>
      <span id="copy-status-{safe_key}" style="margin-left:10px;font-size:0.9em;"></span>
    </div>
    <script>
      (function() {{
        const btn = document.getElementById("copy-btn-{safe_key}");
        const status = document.getElementById("copy-status-{safe_key}");
        const text = {text_js};
        btn.addEventListener("click", async () => {{
          let ok = false;
          try {{
            await navigator.clipboard.writeText(text);
            ok = true;
          }} catch (e) {{
            try {{
              const ta = document.createElement("textarea");
              ta.value = text;
              ta.style.position = "fixed";
              ta.style.opacity = "0";
              document.body.appendChild(ta);
              ta.focus();
              ta.select();
              ok = document.execCommand("copy");
              document.body.removeChild(ta);
            }} catch (e2) {{
              ok = false;
            }}
          }}
          status.style.color = ok ? "#2ecc71" : "#e67e22";
          status.textContent = ok ? "✅ Copié !" : "⚠️ Échec — utilise le texte ci-dessous à la place.";
          setTimeout(() => {{ status.textContent = ""; }}, 3000);
        }});
      }})();
    </script>
    """
    st_components.html(html_code, height=45)


def copyable_text_block(text, label="📋 Copier dans le presse-papiers", key="default"):
    """Bouton de copie en un clic + repli manuel discret si jamais ça échoue
    (certains navigateurs/entreprises bloquent le presse-papiers en script)."""
    copy_button(text, label=label, key=key)
    with st.expander("Afficher le texte (si la copie automatique ne fonctionne pas)"):
        st.text_area("Texte", value=text, height=200, key=f"ta_{key}", label_visibility="collapsed")


# -----------------------------
# Analyse Stockfish
# -----------------------------

@st.cache_resource
def find_engine(path_hint=""):
    candidates = [
        path_hint,
        __import__("os").environ.get("STOCKFISH_PATH", ""),
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
        if mate == 0:
            return 0
        return 100000 if mate > 0 else -100000
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
    """Note sur 10 basée sur la perte moyenne de centipions (ACPL),
    indépendante de la longueur de la partie."""
    if not recs:
        return 10.0
    acpl = sum(r["drop"] for r in recs) / len(recs)
    return round(max(0.0, min(10.0, 10 - acpl / 40)), 1)


PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}


def _hanging_squares(board, color):
    """Cases occupées par une pièce de `color` attaquée et non défendue."""
    squares = []
    for sq in chess.SQUARES:
        p = board.piece_at(sq)
        if p and p.color == color:
            if board.is_attacked_by(not color, sq) and not board.is_attacked_by(color, sq):
                squares.append(sq)
    return squares


def _detect_fork(board_after, refutation_move, user_color):
    """La meilleure réponse adverse (calculée par Stockfish) attaque-t-elle
    d'un coup au moins 2 pièces (≥ cavalier) de l'utilisateur ? -> fourchette."""
    if board_after is None or refutation_move is None:
        return False
    try:
        temp = board_after.copy()
        if temp.piece_at(refutation_move.from_square) is None:
            return False
        temp.push(refutation_move)
        dest = refutation_move.to_square
        count = 0
        for sq in temp.attacks(dest):
            p = temp.piece_at(sq)
            if p and p.color == user_color and PIECE_VALUES.get(p.piece_type, 0) >= 3:
                count += 1
        return count >= 2
    except Exception:
        return False


def _detect_new_pin(before_board, board_after, user_color):
    """Le coup joué a-t-il fait apparaître un clouage sur une pièce de
    l'utilisateur qui n'existait pas avant ?"""
    try:
        before_pins = sum(
            1 for sq in chess.SQUARES
            if before_board.piece_at(sq) and before_board.piece_at(sq).color == user_color
            and before_board.is_pinned(user_color, sq)
        )
        after_pins = sum(
            1 for sq in chess.SQUARES
            if board_after.piece_at(sq) and board_after.piece_at(sq).color == user_color
            and board_after.is_pinned(user_color, sq)
        )
        return after_pins > before_pins
    except Exception:
        return False


def identifier_theme_coup(before_board, move, drop, time_pressure=False,
                           board_after=None, refutation_move=None, walked_into_mate=False):
    """Identifie le thème du coup. Utilise, quand elles sont fournies, la
    position après le coup et la meilleure réponse adverse calculée par
    Stockfish (refutation_move) pour distinguer des motifs plus précis
    (fourchette, clouage, mat forcé permis, pièce déjà en prise ignorée)
    plutôt que de retomber systématiquement sur les catégories générales."""
    if drop < 50:
        return "Coup solide"

    if time_pressure and drop >= 100:
        return "Précipitation (zeitnot)"

    if walked_into_mate:
        return "A permis un mat forcé"

    own_color = before_board.turn
    piece_moved = before_board.piece_at(move.from_square)

    if board_after is None:
        board_after = before_board.copy()
        board_after.push(move)

    if piece_moved and piece_moved.piece_type == chess.KING:
        return "Roi exposé / Échec subi"

    king_sq_before = before_board.king(own_color)
    king_sq_after = board_after.king(own_color)
    if king_sq_before is not None and king_sq_after is not None:
        attackers_before = len(before_board.attackers(not own_color, king_sq_before))
        attackers_after = len(board_after.attackers(not own_color, king_sq_after))
        if attackers_after > attackers_before and attackers_after > 0:
            return "Roi exposé / Échec subi"

    if _detect_fork(board_after, refutation_move, own_color):
        return "Fourchette subie"

    if _detect_new_pin(before_board, board_after, own_color):
        return "Clouage subi"

    pre_existing_hanging = [sq for sq in _hanging_squares(before_board, own_color) if sq != move.from_square]
    if refutation_move is not None and pre_existing_hanging and refutation_move.to_square in pre_existing_hanging:
        return "N'a pas paré une pièce déjà en prise"

    if not before_board.is_capture(move):
        to_square = move.to_square
        if (board_after.is_attacked_by(not own_color, to_square)
                and not board_after.is_attacked_by(own_color, to_square)):
            return "Pièce suspendue (non protégée)"

    captures_visibles = list(before_board.generate_legal_captures())
    if captures_visibles and not before_board.is_capture(move):
        return "Tactique ou capture ratée"

    if piece_moved and piece_moved.piece_type == chess.QUEEN and before_board.fullmove_number <= 10:
        return "Sortie de Dame précoce / Perte de tempo"

    if drop >= 250:
        return "Gaffe tactique majeure"
    elif drop >= 100:
        return "Erreur de calcul / Structure"

    return "Imprécision positionnelle"


def analyze_game(game, username, engine, depth=12, max_plies=160):
    if engine is None:
        return []

    u_color = user_color(game, username)
    if u_color is None:
        u_color = chess.WHITE

    board = game.board()
    records = []
    ply = 0

    for node in game.mainline():
        if ply >= max_plies:
            break

        move = node.move
        mover = board.turn
        before = board.copy()
        san = before.san(move)
        clock_left = clock_from_comment(node.comment)

        try:
            info_before = engine.analyse(before, chess.engine.Limit(depth=depth))
            pv_before = info_before.get("pv", [])
            best_move = pv_before[0] if pv_before else None
            eval_before = score_cp(info_before["score"], u_color)
        except Exception:
            best_move = None
            eval_before = 0

        board.push(move)

        info_after = None
        if board.is_checkmate():
            eval_after = -100000 if board.turn == u_color else 100000
        elif board.is_stalemate() or board.is_insufficient_material():
            eval_after = 0
        else:
            try:
                info_after = engine.analyse(board, chess.engine.Limit(depth=depth))
                eval_after = score_cp(info_after["score"], u_color)
            except Exception:
                eval_after = eval_before

        # La meilleure réponse adverse selon Stockfish sert à affiner le thème
        # (fourchette, clouage, mat forcé...) plutôt que de deviner à l'aveugle.
        refutation_move = None
        walked_into_mate = False
        if info_after is not None:
            pv_after = info_after.get("pv", [])
            refutation_move = pv_after[0] if pv_after else None
            try:
                sc_after = info_after["score"].pov(u_color)
                if sc_after.is_mate() and sc_after.mate() is not None and sc_after.mate() < 0 and eval_before > -3000:
                    walked_into_mate = True
            except Exception:
                pass

        if mover == u_color:
            time_pressure = clock_left is not None and clock_left < 60
            if board.is_checkmate() and board.turn != u_color:
                drop, theme, category = 0.0, "Mat délivré", "OK"
            elif eval_before >= 10000:
                drop, theme, category = 0.0, "Coup gagnant / Attaque décisive", "OK"
            else:
                drop = max(0, eval_before - eval_after)
                category = classify_drop(drop)
                theme = identifier_theme_coup(before, move, drop, time_pressure,
                                               board_after=board, refutation_move=refutation_move,
                                               walked_into_mate=walked_into_mate)

            records.append({
                "ply": ply + 1,
                "move_no": ply // 2 + 1,
                "san": san,
                "drop": round(drop, 1),
                "category": category,
                "theme": theme,
                "best": before.san(best_move) if best_move else "",
                "eval_before": round(eval_before / 100, 2),
                "eval_after": round(eval_after / 100, 2),
                "clock_left": clock_left,
                "time_pressure": bool(time_pressure),
            })

        ply += 1

    return records


# -----------------------------
# Couche coach
# -----------------------------

def build_coach_summary(stats):
    if not stats or stats["games"] == 0:
        return "Pas encore assez de données pour établir un diagnostic."

    total = stats["games"]
    wins, losses, draws = stats["wins"], stats["losses"], stats["draws"]
    blunders, errors, inaccuracies = stats["blunders"], stats["errors"], stats["inaccuracies"]

    lines = [
        f"Après **{total} partie(s)** de cette session, ton bilan est de "
        f"**{wins} victoire(s), {draws} nulle(s), {losses} défaite(s)**.",
    ]

    if blunders / max(total, 1) >= 0.8:
        lines.append("🔴 **Priorité : les gaffes.** Tu dois surtout améliorer la vérification des menaces adverses.")
    elif errors / max(total, 1) >= 1.5:
        lines.append("🟠 **Priorité : la qualité des décisions critiques.** Plusieurs erreurs coûtent significativement de l'évaluation.")
    elif inaccuracies / max(total, 1) >= 3:
        lines.append("🟡 **Priorité : la précision.** Le prochain palier est de réduire les petites pertes d'évaluation.")
    else:
        lines.append("🟢 Les erreurs graves semblent relativement contenues. On peut travailler la compréhension et la conversion.")

    lines.append("Le coach ne cherche pas seulement le meilleur coup : il cherche surtout les **comportements qui se répètent**.")
    return "\n\n".join(lines)


def calculate_stats(games, analyses, username):
    stats = {"games": len(games), "wins": 0, "losses": 0, "draws": 0,
              "blunders": 0, "errors": 0, "inaccuracies": 0}
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


VIDEOS_PAR_THEME = {
    "Pièce suspendue (non protégée)": [
        ("🎥 Méthode pour ne plus donner de pièces", "https://www.youtube.com/results?search_query=eviter+les+gaffes+echecs+pieces+pendantes"),
        ("🎥 Vision tactique et sécurité des pièces", "https://www.youtube.com/results?search_query=vision+tactique+echecs+debutant"),
    ],
    "Roi exposé / Échec subi": [
        ("🎥 La sécurité du Roi et le roque", "https://www.youtube.com/results?search_query=securite+du+roi+echecs"),
        ("🎥 Défendre contre une attaque directe", "https://www.youtube.com/results?search_query=defendre+une+attaque+echecs"),
    ],
    "Tactique ou capture ratée": [
        ("🎥 Les motifs tactiques clés (Julien Song)", "https://www.youtube.com/results?search_query=motifs+tactiques+echecs+julien+song"),
        ("🎥 Comment calculer les coups candidats", "https://www.youtube.com/results?search_query=calculer+les+coups+candidats+echecs"),
    ],
    "Sortie de Dame précoce / Perte de tempo": [
        ("🎥 Pourquoi il ne faut pas sortir la Dame trop tôt", "https://www.youtube.com/results?search_query=sortir+la+dame+trop+tot+echecs"),
        ("🎥 Les grands principes de l'ouverture", "https://www.youtube.com/results?search_query=principes+de+l+ouverture+echecs"),
    ],
    "Précipitation (zeitnot)": [
        ("🎥 Bien gérer sa pendule aux échecs", "https://www.youtube.com/results?search_query=gestion+du+temps+echecs+pendule"),
        ("🎥 Jouer vite sans sacrifier la qualité", "https://www.youtube.com/results?search_query=jouer+vite+echecs+blitz+rapide"),
    ],
    "Fourchette subie": [
        ("🎥 Repérer et éviter les fourchettes", "https://www.youtube.com/results?search_query=fourchette+echecs+eviter"),
        ("🎥 Les fourchettes de cavalier expliquées", "https://www.youtube.com/results?search_query=fourchette+cavalier+echecs"),
    ],
    "Clouage subi": [
        ("🎥 Comprendre et exploiter les clouages", "https://www.youtube.com/results?search_query=clouage+echecs+explication"),
        ("🎥 Se défendre contre un clouage", "https://www.youtube.com/results?search_query=se+defendre+clouage+echecs"),
    ],
    "A permis un mat forcé": [
        ("🎥 Calculer les mats forcés et s'en défendre", "https://www.youtube.com/results?search_query=defendre+mat+force+echecs"),
        ("🎥 Sécurité du roi en milieu de partie", "https://www.youtube.com/results?search_query=securite+du+roi+milieu+de+partie+echecs"),
    ],
    "N'a pas paré une pièce déjà en prise": [
        ("🎥 Vérifier systématiquement les menaces avant de jouer", "https://www.youtube.com/results?search_query=verifier+les+menaces+avant+de+jouer+echecs"),
    ],
}


# ==========================================================
# UI
# ==========================================================

init_db()

st.title(APP_TITLE)
st.caption("Prototype personnel : analyse Stockfish + historique long terme + coaching pédagogique.")

with st.sidebar:
    st.header("⚙️ Paramètres")
    username = st.text_input("Pseudo Chess.com", value=st.session_state.get("username", "gerardmansoif"))
    st.session_state["username"] = username.strip()

    months = st.slider("Nombre de mois à récupérer", 1, 12, 3)
    max_games = st.slider("Nombre maximum de parties", 10, 200, 50, step=10)
    depth = st.slider("Profondeur Stockfish", 10, 20, 15)
    reuse_cache = st.checkbox("Réutiliser une analyse déjà enregistrée si suffisante", value=True)
    engine_path = st.text_input("Chemin Stockfish (optionnel)", value=__import__("os").environ.get("STOCKFISH_PATH", ""))

    if st.button("🔄 Charger mes parties", use_container_width=True):
        st.session_state.pop("games", None)
        st.session_state.pop("games_meta", None)
        st.session_state.pop("analyses_map", None)
        st.rerun()

    st.divider()
    st.subheader("🗄️ Historique local")
    if username:
        life_sidebar = lifetime_stats(username)
        st.caption(f"{life_sidebar['n_games']} partie(s) de **{username}** enregistrée(s) en base locale, "
                   f"dont {life_sidebar['n_analyzed']} analysée(s).")

        hist_export = load_history_df(username)
        if not hist_export.empty:
            csv_data = hist_export.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Exporter l'historique (CSV)", data=csv_data,
                                file_name=f"historique_{username}.csv", mime="text/csv",
                                use_container_width=True)

        with st.expander("⚠️ Réinitialiser l'historique"):
            st.warning("Supprime définitivement l'historique local (parties + analyses) pour ce pseudo.")
            confirm = st.checkbox("Je confirme vouloir tout supprimer", key="confirm_reset")
            if st.button("🗑️ Supprimer l'historique", disabled=not confirm):
                clear_history(username)
                st.success("Historique supprimé.")
                st.rerun()

    st.divider()
    st.info(
        "Cette app utilise l'API publique Chess.com pour récupérer les parties publiques "
        "et Stockfish en local. Toutes les parties chargées et analyses sont sauvegardées "
        "automatiquement dans une base locale pour suivre ta progression dans le temps."
    )

if not username:
    st.warning("Indique ton pseudo Chess.com.")
    st.stop()

# Chargement des données
if "games" not in st.session_state:
    with st.spinner("Récupération de tes parties (cadences longues uniquement)..."):
        try:
            archives = get_archives(username)
            selected_archives = archives[-months:]

            raw_games = []
            for url in reversed(selected_archives):
                month_games = get_month_games(url)
                month_games.reverse()
                raw_games.extend(month_games)

            filtered_raw_games = [g for g in raw_games if est_une_partie_longue(g)]

            parsed, metas = [], []
            for gd in filtered_raw_games[:max_games]:
                g = parse_game(gd)
                if g:
                    parsed.append(g)
                    metas.append(extract_meta(gd, username))

            st.session_state["games"] = parsed
            st.session_state["games_meta"] = metas
            st.session_state["loaded_at"] = datetime.now(timezone.utc).isoformat()

            # Sauvegarde immédiate en base : même sans analyse Stockfish, on
            # conserve la trace (classement, résultat, ouverture) pour le suivi long terme.
            for g, meta in zip(parsed, metas):
                upsert_game(username, meta["game_id"], meta, g)

        except Exception as e:
            st.error(f"Impossible de récupérer les parties Chess.com : {e}")
            st.stop()

games = st.session_state["games"]
games_meta = st.session_state.get("games_meta", [])
engine = find_engine(engine_path)

if engine is None:
    st.warning(
        "⚠️ Stockfish n'est pas détecté. L'application peut afficher tes parties, "
        "mais l'analyse précise des coups nécessite Stockfish. Voir README.md."
    )

tabs = st.tabs([
    "🏠 Tableau de bord", "♟️ Mes parties", "🔎 Analyse", "📈 Progression",
    "📊 Statistiques", "🎯 Entraînement", "🗞️ Rapport hebdo",
])

# --- Tableau de bord ---
with tabs[0]:
    st.subheader("Ton tableau de bord")

    life = lifetime_stats(username)
    st.markdown("##### 📚 Historique global (toutes sessions confondues)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Parties en base", life["n_games"])
    c2.metric("Parties analysées", life["n_analyzed"])
    c3.metric("Note moyenne", life["avg_note"] if life["avg_note"] is not None else "—")
    c4.metric("Gaffes cumulées", life["total_blunders"])

    try:
        player_stats = get_player_stats(username)
        rapid = ((player_stats.get("chess_rapid") or {}).get("last") or {}).get("rating")
        if rapid:
            st.caption(f"🏆 Classement Rapid actuel sur Chess.com : **{rapid}**")
    except Exception:
        pass

    st.markdown("##### 🗂️ Session en cours")
    analyses_map = st.session_state.get("analyses_map", {})
    session_stats = calculate_stats(games, analyses_map, username)
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Parties chargées", session_stats["games"])
    d2.metric("Victoires", session_stats["wins"])
    d3.metric("Défaites", session_stats["losses"])
    d4.metric("Gaffes (session)", session_stats["blunders"])

    st.markdown(build_coach_summary(session_stats))

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

# --- Mes parties ---
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

# --- Analyse ---
with tabs[2]:
    st.subheader("Analyse d'une partie")
    if not games:
        st.info("Aucune partie disponible.")
    else:
        labels = [
            f"{i} — {g.headers.get('Date','')} — {g.headers.get('White','')} vs {g.headers.get('Black','')} — "
            f"{outcome_label(result_for_user(g, username))}"
            for i, g in enumerate(games)
        ]
        selected = st.selectbox("Choisis une partie", range(len(labels)),
                                 format_func=lambda i: labels[i], key="select_game_analyse")

        if st.button("🧠 Analyser avec Stockfish", type="primary"):
            game_id = games_meta[selected].get("game_id") if selected < len(games_meta) else None
            cached = get_cached_analysis(username, game_id, depth) if (reuse_cache and game_id) else None

            if cached is not None:
                res = cached
                st.info("Analyse déjà disponible en base pour cette partie (profondeur suffisante) — réutilisée. "
                        "Décoche la case dans la barre latérale pour forcer une nouvelle analyse.")
            elif engine is None:
                st.error("⚠️ Stockfish n'est pas disponible sur le serveur.")
                res = None
            else:
                with st.spinner("Analyse approfondie en cours…"):
                    res = analyze_game(games[selected], username, engine, depth=depth)
                if game_id:
                    save_analysis(username, game_id, depth, res)

            if res is not None:
                st.session_state.setdefault("analyses_map", {})[selected] = res

        analyses_map = st.session_state.get("analyses_map", {})
        recs = analyses_map.get(selected)

        if recs is not None:
            if len(recs) == 0:
                st.warning("Aucun coup n'a pu être analysé. Vérifie que le pseudo dans la barre latérale "
                           "correspond à l'un des deux joueurs.")
            else:
                st.markdown("### 📊 Fiche de performance")
                note = calculer_note_partie(recs)
                acpl = round(sum(r["drop"] for r in recs) / len(recs), 1)
                zeitnot_moves = sum(1 for r in recs if r.get("time_pressure"))

                m1, m2, m3 = st.columns(3)
                m1.metric("Note du Coach", f"{note}/10")
                m2.metric("Perte moyenne (ACPL)", f"{acpl} cp")
                m3.metric("Coups joués sous 60s", zeitnot_moves)

                erreurs_notables = [r for r in recs if r["category"] != "OK"]
                severe = sorted(erreurs_notables, key=lambda x: x["drop"], reverse=True)[:3]

                st.markdown("### 🔴 Les moments décisifs (Analyse Pédagogique)")
                if not severe:
                    st.success("🎉 Partie très propre, aucune erreur significative détectée !")
                else:
                    for r in severe:
                        clock_txt = f" — ⏱️ {format_seconds(r.get('clock_left'))} restant" if r.get("time_pressure") else ""
                        with st.expander(f"Coup {r['move_no']} ({r['san']}) — {r['theme']} (-{r['drop']/100:.2f} pions){clock_txt}"):
                            st.write(f"✏️ **Thème identifié :** `{r['theme']}`")
                            st.write(f"❌ **Ton coup :** **{r['san']}** (Éval : {r['eval_after']})")
                            st.write(f"💡 **Recommandation Stockfish :** **{r['best']}** (Éval : {r['eval_before']})")

            st.divider()
            prompt = generate_coach_prompt(games[selected], recs)
            copyable_text_block(prompt, label="📋 Copier l'analyse pour Gemini", key=f"gemini_{selected}")

# --- Progression ---
with tabs[3]:
    st.subheader("📈 Progression (historique long terme)")
    hist = load_history_df(username)

    if hist.empty:
        st.info("Charge et analyse quelques parties pour commencer à construire ton historique long terme. "
                "Toutes tes parties chargées sont désormais enregistrées automatiquement, même sans analyse Stockfish.")
    else:
        hist["DateJeu"] = hist.apply(compute_datetime, axis=1)
        hist = hist.dropna(subset=["DateJeu"]).sort_values("DateJeu")

        periode = st.selectbox("Période à afficher",
                                ["30 derniers jours", "90 derniers jours", "1 an", "Tout l'historique"],
                                index=3)
        if periode != "Tout l'historique":
            jours = {"30 derniers jours": 30, "90 derniers jours": 90, "1 an": 365}[periode]
            seuil = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=jours)
            hist_filtre = hist[hist["DateJeu"] >= seuil]
        else:
            hist_filtre = hist

        st.caption(f"{len(hist_filtre)} partie(s) dans la période sélectionnée, "
                   f"dont {hist_filtre['note'].notna().sum()} analysée(s) par Stockfish.")

        rating_df = hist_filtre.dropna(subset=["user_rating"]).set_index("DateJeu")[["user_rating"]]
        if not rating_df.empty:
            st.markdown("### 🏆 Évolution de ton classement Chess.com")
            st.line_chart(rating_df.rename(columns={"user_rating": "Classement"}))

        note_df = hist_filtre.dropna(subset=["note"]).sort_values("DateJeu").set_index("DateJeu")
        if not note_df.empty:
            note_df["Note (moy. mobile 5)"] = note_df["note"].rolling(5, min_periods=1).mean()
            st.markdown("### 🧠 Évolution de la note du Coach (moyenne mobile sur 5 parties)")
            st.line_chart(note_df[["Note (moy. mobile 5)"]])

            st.markdown("### 📉 Évolution des erreurs par partie")
            st.line_chart(note_df[["blunders", "errors", "inaccuracies"]].rename(columns={
                "blunders": "Gaffes", "errors": "Erreurs", "inaccuracies": "Imprécisions"}))

            st.markdown("### 📋 Historique détaillé")
            table = hist_filtre[[
                "date", "white", "black", "result", "user_rating", "opp_rating",
                "note", "acpl", "blunders", "errors", "inaccuracies", "zeitnot_ratio",
            ]].rename(columns={
                "date": "Date", "white": "Blancs", "black": "Noirs", "result": "Résultat",
                "user_rating": "Ton classement", "opp_rating": "Classement adverse",
                "note": "Note", "acpl": "ACPL", "blunders": "Gaffes", "errors": "Erreurs",
                "inaccuracies": "Imprécisions", "zeitnot_ratio": "% coups en zeitnot",
            })
            st.dataframe(table.iloc[::-1], use_container_width=True, hide_index=True)
        else:
            st.info("Aucune partie analysée par Stockfish dans cette période pour l'instant.")

        st.markdown("### ⚖️ Résultats selon l'écart de classement")
        gap_df = hist_filtre.dropna(subset=["user_rating", "opp_rating"]).copy()
        if gap_df.empty:
            st.info("Pas encore assez de parties avec classement connu pour cette analyse.")
        else:
            gap_df["rating_diff"] = gap_df["user_rating"] - gap_df["opp_rating"]
            gap_df["bucket"] = gap_df["rating_diff"].apply(rating_gap_bucket)
            gap_df = gap_df.dropna(subset=["bucket"])

            grouped = gap_df.groupby("bucket")["result"].value_counts().unstack().fillna(0)
            for col in ["1-0", "1/2-1/2", "0-1"]:
                if col not in grouped.columns:
                    grouped[col] = 0
            grouped["Total"] = grouped[["1-0", "1/2-1/2", "0-1"]].sum(axis=1)
            grouped["Taux de victoire (%)"] = (grouped["1-0"] / grouped["Total"] * 100).round(1)
            grouped = grouped.rename(columns={"1-0": "Victoires", "1/2-1/2": "Nulles", "0-1": "Défaites"})
            grouped = grouped[["Victoires", "Nulles", "Défaites", "Total", "Taux de victoire (%)"]]
            grouped = grouped.sort_index()

            st.dataframe(grouped, use_container_width=True)
            st.bar_chart(grouped[["Taux de victoire (%)"]])

            gap_df_analyzed = gap_df.dropna(subset=["blunders"])
            if not gap_df_analyzed.empty and gap_df_analyzed["bucket"].nunique() > 1:
                blunders_by_bucket = gap_df_analyzed.groupby("bucket")["blunders"].mean().sort_index()
                ecart = blunders_by_bucket.max() - blunders_by_bucket.min()
                if ecart >= 0.5:
                    bucket_pire = blunders_by_bucket.idxmax()
                    st.info(f"💡 Tu commets en moyenne {ecart:.1f} gaffe(s) de plus par partie dans la catégorie "
                            f"« {bucket_pire.split('. ', 1)[-1]} » que dans ta meilleure catégorie. "
                            f"Ça peut valoir le coup de regarder si c'est un problème de préparation ou de nervosité.")

    st.divider()
    st.markdown("### 🎯 Axes de progression actuels")
    st.markdown(
        "- **1. Menaces adverses :** avant chaque décision critique, vérifier les échecs, captures et menaces.\n"
        "- **2. Calcul :** chercher 2–3 coups candidats avant de choisir.\n"
        "- **3. Conversion :** lorsqu'une position est meilleure, identifier le plan le plus simple.\n"
        "- **4. Gestion du temps :** réserver la réflexion longue aux positions réellement critiques."
    )

# --- Statistiques ---
with tabs[4]:
    st.subheader("📈 Mes performances par ouverture")

    utiliser_historique_complet = st.checkbox(
        "Utiliser tout l'historique enregistré en base (recommandé pour les tendances long terme)",
        value=True,
    )
    if utiliser_historique_complet:
        games_pour_stats = load_all_games_from_db(username) or games
    else:
        games_pour_stats = games

    st.caption(f"Analyse basée sur {len(games_pour_stats)} partie(s).")
    df_stats = extraire_stats_ouvertures(games_pour_stats, username)

    if not df_stats.empty:
        couleur_choisie = st.radio("Analyser les parties avec les :", ["Blancs", "Noirs"], horizontal=True)
        df_filtre = df_stats[df_stats["Couleur"] == couleur_choisie]

        if not df_filtre.empty:
            tableau_resume = df_filtre.groupby("Ouverture")["Résultat"].value_counts().unstack().fillna(0)
            for col in ["Victoire", "Nulle", "Défaite"]:
                if col not in tableau_resume.columns:
                    tableau_resume[col] = 0
            tableau_resume["Total"] = tableau_resume.sum(axis=1)
            tableau_resume = tableau_resume.sort_values("Total", ascending=False).astype(int)
            tableau_resume = tableau_resume[["Victoire", "Nulle", "Défaite", "Total"]]

            st.dataframe(tableau_resume, use_container_width=True)
            st.bar_chart(tableau_resume[["Victoire", "Nulle", "Défaite"]])
        else:
            st.info(f"Aucune partie trouvée avec les {couleur_choisie} dans l'échantillon analysé.")
    else:
        st.warning("Pas assez de données pour générer des statistiques sur les ouvertures.")

    st.divider()
    st.markdown("### 🔍 Écarts à la théorie d'ouverture (base Lichess, cadence rapide)")
    st.caption(
        "Repère, pour tes parties les plus récentes, le premier coup qui sort des coups les plus joués "
        "dans la base Lichess à cadence rapide — utile pour voir où s'arrête ta préparation. "
        "Nécessite de contacter l'API publique de Lichess (explorer.lichess.org)."
    )
    n_games_explorer = st.slider("Nombre de parties récentes à vérifier", 3, 20, 8, key="n_explorer")
    if st.button("🔍 Analyser mes ouvertures récentes"):
        recent_games_for_explorer = most_recent_games(games_pour_stats, n_games_explorer)
        with st.spinner("Interrogation de la base Lichess (peut prendre quelques secondes)..."):
            deviations, api_reachable = find_opening_deviations(username, recent_games_for_explorer)
        st.session_state["opening_deviations"] = deviations
        st.session_state["opening_deviations_api_ok"] = api_reachable

    deviations = st.session_state.get("opening_deviations")
    if deviations is not None:
        if not st.session_state.get("opening_deviations_api_ok"):
            st.warning("Impossible de contacter l'explorateur d'ouvertures Lichess pour le moment "
                       "(service indisponible, ou accès restreint). Réessaie plus tard.")
        elif not deviations:
            st.success("Aucun écart notable détecté sur cet échantillon — tu restes dans les clous "
                       "de la théorie sur ces parties (ou l'échantillon est trop petit pour conclure).")
        else:
            for d in deviations:
                st.markdown(f"**{d['date']} — {d['white']} vs {d['black']}** : au coup {d['move_no']}, "
                            f"tu as joué **{d['san_joue']}**, alors que la base privilégie "
                            f"{', '.join(d['alternatives'])}.")

# --- Entraînement & Ressources ---
with tabs[5]:
    st.subheader("🎯 Entraînement & Ressources Pédagogiques")

    st.markdown("### 📊 Tes Thèmes de Travail Prioritaires (tendance récente)")
    st.caption(
        "Les recommandations ci-dessous suivent tes parties récentes, pas seulement le cumul depuis toujours : "
        "si tu progresses sur un point et régresses sur un autre, elles évoluent avec toi."
    )
    recent_n = st.slider("Nombre de parties récentes prises en compte pour la tendance", 5, 30, 10)
    trends_df, n_recent, n_previous, has_comparison = compute_theme_trends(username, recent_n=recent_n)

    if trends_df.empty:
        st.info("Analyse quelques parties dans l'onglet 'Analyse' pour générer ton bilan thématique et tes vidéos sur mesure.")
        top_theme = None
    else:
        if has_comparison:
            st.caption(f"Comparaison entre tes {n_recent} dernière(s) partie(s) analysée(s) et les {n_previous} précédente(s).")
        else:
            st.caption(f"Bilan basé sur tes {n_recent} dernière(s) partie(s) analysée(s) "
                       f"(pas encore assez d'historique pour comparer à une période antérieure).")

        display_df = trends_df[["theme", "recent_count", "recent_rate", "delta"]].rename(columns={
            "theme": "Thème", "recent_count": "Occurrences récentes",
            "recent_rate": "Fréquence (par partie)", "delta": "delta_raw",
        })
        display_df["Évolution"] = trends_df["delta"].apply(format_trend)
        display_df = display_df.drop(columns=["delta_raw"])
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        top_theme = trends_df.iloc[0]["theme"]
        st.error(f"🎯 **Axe d'entraînement principal en ce moment :** `{top_theme}` "
                 f"({trends_df.iloc[0]['recent_rate']:.2f} occurrence(s)/partie récemment).")

        if has_comparison:
            improved = trends_df[trends_df["delta"] < -0.15].sort_values("delta")
            if not improved.empty:
                best = improved.iloc[0]
                st.success(f"👏 Belle progression sur **{best['theme']}** "
                           f"({best['delta']:.2f}/partie par rapport à la période précédente) — continue comme ça !")

        st.markdown("#### 🎬 Vidéos recommandées pour corriger ce point :")
        cours_suggeres = VIDEOS_PAR_THEME.get(top_theme, [
            ("🎥 Travailler sa vision du jeu et sa régularité", "https://www.youtube.com/results?search_query=progression+echecs+conseils"),
        ])
        for titre, url in cours_suggeres:
            st.markdown(f"* [{titre}]({url})")

        st.markdown("#### 🌐 Puzzles Lichess ciblés sur ce thème")
        st.caption("En complément de tes propres puzzles ci-dessous : des puzzles de la base Lichess sur ce thème précis.")
        if st.button("🔄 Charger des puzzles Lichess sur ce thème", key="btn_lichess_puzzles"):
            with st.spinner("Recherche de puzzles sur Lichess..."):
                lichess_puzzles = get_lichess_puzzles_for_theme(top_theme, n=3)
            st.session_state["lichess_puzzles"] = lichess_puzzles
            st.session_state["lichess_puzzles_theme"] = top_theme

        cached_puzzles = st.session_state.get("lichess_puzzles")
        cached_theme = st.session_state.get("lichess_puzzles_theme")
        if cached_puzzles and cached_theme == top_theme:
            if not cached_puzzles:
                st.warning("Impossible de récupérer des puzzles Lichess pour le moment (API indisponible ou thème sans correspondance).")
            for p in cached_puzzles:
                rating_txt = f" — difficulté ≈ {p['rating']}" if p.get("rating") else ""
                st.markdown(f"* [Puzzle Lichess {p['id']}]({p['url']}){rating_txt}")
        elif cached_theme and cached_theme != top_theme:
            st.caption("Le thème prioritaire a changé depuis ton dernier chargement — clique à nouveau pour actualiser les puzzles.")

    st.divider()

    st.markdown("### 🧩 Puzzles personnels (révision espacée)")
    st.caption(
        "Méthode Leitner : un coup que tu retrouves revient de moins en moins souvent (jusqu'à tous les 30 jours), "
        "un coup raté revient vite (le lendemain). Basé sur l'ensemble de ton historique enregistré."
    )

    due_puzzles, all_puzzles_enriched = load_due_puzzles(username)

    if not all_puzzles_enriched:
        st.info("Les moments clés à rejouer s'afficheront ici automatiquement dès qu'une partie sera analysée.")
    else:
        box_counts = Counter(p["box"] for p in all_puzzles_enriched)
        box_cols = st.columns(5)
        for i in range(1, 6):
            box_cols[i - 1].metric(f"Boîte {i}", box_counts.get(i, 0))

        if not due_puzzles:
            st.success(f"🎉 Aucun puzzle à réviser aujourd'hui parmi les {len(all_puzzles_enriched)} enregistrés — "
                       f"reviens demain, ou analyse de nouvelles parties pour en ajouter.")
        else:
            st.write(f"**{len(due_puzzles)} puzzle(s) à réviser aujourd'hui** (sur {len(all_puzzles_enriched)} au total).")
            puzzle_data = due_puzzles[0]
            puzzle_key = f"{puzzle_data['game_id']}_{puzzle_data['ply']}"

            pgn_text, user_color_str = load_game_pgn_and_color(username, puzzle_data["game_id"])
            board_before, _ = board_before_ply(pgn_text, puzzle_data["ply"]) if pgn_text else (None, None)

            st.markdown(f"**{puzzle_data['date']} — {puzzle_data['white']} vs {puzzle_data['black']} — "
                        f"Coup {puzzle_data['move_no']}** · Boîte {puzzle_data['box']} · "
                        f"{puzzle_data['review_count']} révision(s) précédente(s)")
            st.caption(f"Thème : {puzzle_data.get('theme', puzzle_data['category'])} — "
                       f"tu avais joué {puzzle_data['san']} (perte de {puzzle_data['drop'] / 100:.2f} pions)")

            if board_before is not None:
                render_puzzle_board(board_before, user_color_str)
            else:
                st.warning("Position introuvable (la partie n'est peut-être plus dans l'historique).")

            with st.expander("💡 Révéler la solution", expanded=False):
                st.success(f"Le meilleur coup était : **{puzzle_data['best']}**")
                if board_before is not None:
                    render_puzzle_board(board_before, user_color_str, best_san=puzzle_data["best"])
                st.write(f"Ton coup : {puzzle_data['san']} (Éval après : {puzzle_data['eval_after']}) — "
                         f"Coup recommandé : {puzzle_data['best']} (Éval avant : {puzzle_data['eval_before']})")

                st.markdown("**As-tu retrouvé ce coup avant de regarder ?**")
                b1, b2 = st.columns(2)
                if b1.button("✅ Oui, trouvé", key=f"correct_{puzzle_key}", use_container_width=True):
                    record_review(username, puzzle_data["game_id"], puzzle_data["ply"], correct=True)
                    st.rerun()
                if b2.button("❌ Non, pas trouvé", key=f"wrong_{puzzle_key}", use_container_width=True):
                    record_review(username, puzzle_data["game_id"], puzzle_data["ply"], correct=False)
                    st.rerun()

        with st.expander("📚 Parcourir tous tes puzzles enregistrés"):
            selected_puzzle_idx = st.selectbox(
                "Choisis un moment à revoir librement :",
                range(len(all_puzzles_enriched)),
                format_func=lambda i: (
                    f"{all_puzzles_enriched[i]['date']} — {all_puzzles_enriched[i]['white']} vs "
                    f"{all_puzzles_enriched[i]['black']} — Coup {all_puzzles_enriched[i]['move_no']} "
                    f"(Boîte {all_puzzles_enriched[i]['box']})"
                ),
                key="browse_all_puzzles",
            )
            browse_data = all_puzzles_enriched[selected_puzzle_idx]
            pgn_text2, user_color_str2 = load_game_pgn_and_color(username, browse_data["game_id"])
            board_before2, _ = board_before_ply(pgn_text2, browse_data["ply"]) if pgn_text2 else (None, None)
            if board_before2 is not None:
                render_puzzle_board(board_before2, user_color_str2)
            st.write(f"Coup joué : **{browse_data['san']}** — Meilleur coup : **{browse_data['best']}**")

    st.divider()

    st.markdown("### 📚 Bibliothèque Pédagogique Permanente")
    cat_tactique, cat_ouvertures, cat_finales, cat_outils = st.tabs([
        "🧩 Tactique & Calcul", "📖 Ouvertures & Stratégie", "👑 Finales", "🛠️ Outils & Plateformes",
    ])

    with cat_tactique:
        st.markdown("""
        **Vidéos Thématiques :**
        * 🎥 [Julien Song — Les motifs tactiques indispensables](https://www.youtube.com/results?search_query=julien+song+tactique+echecs)
        * 🎥 [Éviter les gaffes aux échecs (Méthode de vérification)](https://www.youtube.com/results?search_query=eviter+les+gaffes+echecs)

        **Plateformes d'exercices :**
        * 🧩 [Lichess Puzzles](https://lichess.org/training) — Exercices gratuits illimités.
        * ⚔️ [Lichess Puzzle Racer](https://lichess.org/racer) — Entraînement à la vitesse de calcul.
        """)

    with cat_ouvertures:
        st.markdown("""
        **Vidéos Thématiques & Ton Répertoire :**
        * 🎥 [Les grands principes de l'ouverture](https://www.youtube.com/results?search_query=principes+des+ouvertures+echecs)
        * ⚪ [Maîtriser l'Ouverture Catalane](https://www.youtube.com/results?search_query=ouverture+catalane+echecs)
        * ⚫ [La Défense Française : Focus Variante d'Avance](https://www.youtube.com/results?search_query=defense+francaise+variante+avance+echecs)
        * ⚫ [Comprendre la Défense Slave](https://www.youtube.com/results?search_query=defense+slave+echecs)

        **Base de données :**
        * 📚 [Lichess Opening Explorer](https://lichess.org/analysis#explorer) — Statistiques et lignes théoriques.
        * 📦 [Chessable](https://www.chessable.com) — Apprentissage par répétition espacée.
        """)

    with cat_finales:
        st.markdown("""
        **Vidéos Thématiques :**
        * 🎥 [Les finales de Tours indispensables](https://www.youtube.com/results?search_query=finales+de+tours+echecs)
        * 🎥 [La règle de l'opposition dans les finales de pions](https://www.youtube.com/results?search_query=opposition+finales+pions+echecs)

        **Modules interactifs :**
        * 👑 [Lichess Practice - Finales](https://lichess.org/practice) — Entraînement guidé sur les finales clés.
        * 🧮 [Syzygy Endgame Tablebases](https://syzygy-tables.info) — Tablebases officielles des finales.
        """)

    with cat_outils:
        st.markdown("""
        **Outils de travail & Chaînes YouTube conseillées :**
        * 🔍 [Lichess Analysis Board](https://lichess.org/analysis) — Échiquier d'analyse gratuit avec Stockfish.
        * 🎥 [Blitzstream (YouTube)](https://www.youtube.com/@Blitzstream) — Analyses pédagogiques et parties commentées.
        * 🎥 [Chess.com France](https://www.youtube.com/@chesscomfr) — Cours et analyses en français.
        """)

# --- Rapport hebdomadaire ---
with tabs[6]:
    st.subheader("🗞️ Rapport hebdomadaire")
    st.caption(
        "Calculé en direct à partir de ton historique local à chaque ouverture de cet onglet — "
        "ce n'est pas une notification envoyée automatiquement, mais toujours à jour."
    )

    report = weekly_report(username)

    if report is None:
        st.info("Pas encore assez d'historique enregistré pour générer un rapport. "
                "Charge quelques parties dans les autres onglets pour commencer.")
    else:
        cur, prev = report["cur"], report["prev"]
        st.markdown(f"#### Semaine du {report['period_start'].date()} au {report['period_end'].date()}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Parties jouées", cur["n"], delta=cur["n"] - prev["n"])
        c2.metric("Score de la semaine", f"{cur['wins']}V {cur['draws']}N {cur['losses']}D")
        rating_delta = None
        if cur["rating_end"] is not None and prev["rating_end"] is not None:
            rating_delta = cur["rating_end"] - prev["rating_end"]
        c3.metric("Classement en fin de semaine", cur["rating_end"] if cur["rating_end"] is not None else "—",
                   delta=rating_delta)

        c4, c5, c6 = st.columns(3)
        note_delta = None
        if cur["avg_note"] is not None and prev["avg_note"] is not None:
            note_delta = round(cur["avg_note"] - prev["avg_note"], 2)
        c4.metric("Note moyenne (Stockfish)", cur["avg_note"] if cur["avg_note"] is not None else "—", delta=note_delta)
        c5.metric("Gaffes", cur["blunders"], delta=cur["blunders"] - prev["blunders"], delta_color="inverse")
        c6.metric("Erreurs", cur["errors"], delta=cur["errors"] - prev["errors"], delta_color="inverse")

        st.markdown("#### 📝 Résumé de la semaine")
        st.markdown(build_weekly_narrative(cur, prev, report["top_theme_week"]))

        st.divider()
        st.markdown("#### 📈 Tendance sur les dernières semaines")
        hist_weekly = report["hist"].copy()
        hist_weekly["Semaine"] = hist_weekly["DateJeu"].dt.to_period("W").apply(lambda p: p.start_time)
        weekly_grouped = hist_weekly.groupby("Semaine").agg(
            parties=("game_id", "count"),
            note_moy=("note", "mean"),
            gaffes=("blunders", "sum"),
        )
        if not weekly_grouped.empty:
            st.line_chart(weekly_grouped[["parties"]].rename(columns={"parties": "Parties jouées"}))
            note_weekly = weekly_grouped.dropna(subset=["note_moy"])
            if not note_weekly.empty:
                st.line_chart(note_weekly[["note_moy"]].rename(columns={"note_moy": "Note moyenne"}))

        st.divider()
        texte = generate_weekly_report_text(username, cur, prev, report["top_theme_week"],
                                             report["period_start"], report["period_end"])
        copyable_text_block(texte, label="📋 Copier le rapport", key="weekly_report")
