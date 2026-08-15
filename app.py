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
import pandas as pd

def extraire_stats_ouvertures(parties_pgn, pseudo):
    """
    Parcourt une liste d'objets chess.pgn.Game et extrait les statistiques
    de victoire/nulle/défaite par ouverture pour un joueur donné.
    """
    donnees = []
    
    for partie in parties_pgn:
        headers = partie.headers
        eco = headers.get("ECO", "Inconnu")
        
        # Sur Chess.com, l'URL de l'ECO contient souvent le nom lisible de l'ouverture
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
            continue # Si le joueur n'est pas dans la partie

        donnees.append({
            "Couleur": couleur,
            "Ouverture": nom_ouverture,
            "Résultat": issue
        })
        
    return pd.DataFrame(donnees)
    
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
        return True

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

def identifier_theme_coup(before_board, move, drop):
    """
    Identifie le thème tactique ou stratégique du coup joué
    en analysant l'état de l'échiquier avant et après le coup.
    """
    if drop < 50:
        return "Coup solide"

    mover = before_board.turn
    piece_moved = before_board.piece_at(move.from_square)
    
    # Évaluation après le coup joué
    after_board = before_board.copy()
    after_board.push(move)

    # 1. Pièce pendante / non protégée laissée en prise
    if after_board.is_capture(move) is False:
        to_square = move.to_square
        if after_board.is_attacked_by(not mover, to_square) and not after_board.is_attacked_by(mover, to_square):
            return "Pièce suspendue (non protégée)"

    # 2. Échec évitable / Roi exposé
    if after_board.is_check():
        return "Roi exposé / Échec subi"

    # 3. Raté d'une capture ou d'un gain de pièce (si la case d'arrivée n'est pas une capture)
    captures_visibles = list(before_board.generate_legal_captures())
    if captures_visibles and not before_board.is_capture(move):
        return "Tactique ou capture ratée"

    # 4. Développement précoce de la Dame / Perte de tempo
    if piece_moved and piece_moved.piece_type == chess.QUEEN and before_board.fullmove_number <= 10:
        return "Sortie de Dame précoce / Perte de tempo"

    # 5. Défense passive ou manque de contrôle du centre
    if drop >= 250:
        return "Gaffe tactique majeure"
    elif drop >= 100:
        return "Erreur de calcul / Structure"
    
    return "Imprécision positionnelle"

def analyze_game(game, username, engine, depth=12, max_plies=160):
    if engine is None:
        return []

    # Vérification insensible à la casse de la couleur
    white = game.headers.get("White", "").strip().lower()
    black = game.headers.get("Black", "").strip().lower()
    user = username.strip().lower()

    if user in white:
        u_color = chess.WHITE
    elif user in black:
        u_color = chess.BLACK
    else:
        # Par défaut, si non trouvé, on analyse les Blancs
        u_color = chess.WHITE

    board = game.board()
    records = []

    for ply, move in enumerate(game.mainline_moves()):
        if ply >= max_plies:
            break

        mover = board.turn
        before = board.copy()
        san = board.san(move)

        # Calcul coup avant
        try:
            info_before = engine.analyse(before, chess.engine.Limit(depth=depth))
            pv_before = info_before.get("pv", [])
            best_move = pv_before[0] if pv_before else None
            eval_before = score_cp(info_before["score"], u_color)
        except Exception:
            best_move = None
            eval_before = 0

        board.push(move)

        # Calcul coup après
        try:
            info_after = engine.analyse(board, chess.engine.Limit(depth=depth))
            eval_after = score_cp(info_after["score"], u_color)
        except Exception:
            eval_after = eval_before

       # Enregistrement des coups du joueur
        if mover == u_color:
            drop = max(0, eval_before - eval_after)
            theme = identifier_theme_coup(before, move, drop)
            
            records.append({
                "ply": ply + 1,
                "move_no": ply // 2 + 1,
                "san": san,
                "drop": round(drop, 1),
                "category": classify_drop(drop),
                "theme": theme,  # <-- NOUVEAU CHAMP THÉMATIQUE
                "best": before.san(best_move) if best_move else "",
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
            # On parcourt les mois du plus récent au plus ancien (Août -> Juillet -> Juin)
            for url in reversed(selected_archives):
                month_games = get_month_games(url)
                # On inverse chaque mois pour avoir les jours de fin de mois en premier
                month_games.reverse()
                raw_games.extend(month_games)
            
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

tabs = st.tabs(["🏠 Tableau de bord", "♟️ Mes parties", "🔎 Analyse", "📈 Progression", "📊 Statistiques", "🎯 Entraînement"])

# Dashboard
with tabs[0]:
    st.subheader("Ton tableau de bord")
    analyses = st.session_state.get("analyses_map", {})
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
        st.info("Aucune partie disponible.")
    else:
        labels = [
            f"{i} — {g.headers.get('Date','')} — {g.headers.get('White','')} vs {g.headers.get('Black','')} — {outcome_label(result_for_user(g, username))}"
            for i, g in enumerate(games)
        ]
        selected = st.selectbox("Choisis une partie", range(len(labels)), format_func=lambda i: labels[i], key="select_game_analyse")

        if st.button("🧠 Analyser avec Stockfish", type="primary"):
            if engine is None:
                st.error("⚠️ Stockfish n'est pas disponible sur le serveur.")
            else:
                with st.spinner("Analyse approfondie en cours…"):
                    res = analyze_game(games[selected], username, engine, depth=depth)
                    if "analyses_map" not in st.session_state:
                        st.session_state["analyses_map"] = {}
                    st.session_state["analyses_map"][selected] = res

        analyses_map = st.session_state.get("analyses_map", {})
        recs = analyses_map.get(selected)

        if recs is not None:
            if len(recs) == 0:
                st.warning("Aucun coup n'a pu être analysé. Vérifie que le pseudo dans la barre latérale correspond à l'un des deux joueurs.")
            else:
                st.markdown("### 📊 Fiche de performance")
                note = calculer_note_partie(recs)
                st.metric("Note du Coach", f"{note}/10")
                
                # Sélection des pires coups (drop > 0)
                erreurs = [r for r in recs if r['drop'] >= 0]
                severe = sorted(erreurs, key=lambda x: x['drop'], reverse=True)[:3]
                
                st.markdown("### 🔴 Les 3 moments décisifs (Analyse Pédagogique)")
                for r in severe:
                    with st.expander(f"Coup {r['move_no']} ({r['san']}) — {r['theme']} (-{r['drop']/100:.2f} pions)"):
                        st.write(f"🏷️ **Thème identifié :** `{r['theme']}`")
                        st.write(f"❌ **Ton coup :** **{r['san']}** (Éval : {r['eval_after']})")
                        st.write(f"💡 **Recommandation Stockfish :** **{r['best']}** (Éval : {r['eval_before']})")
                        
                        # Explications personnalisées selon le thème
                        if "Pièce suspendue" in r['theme']:
                            st.warning("🧠 **Conseil du Coach :** Vérifie toujours si la case où tu déplaces ta pièce est attaquée et si ton coup laisse une pièce sans défense.")
                        elif "Tactique" in r['theme']:
                            st.warning("🧠 **Conseil du Coach :** Prends 5 secondes pour scanner les échecs, prises et menaces directes avant de jouer.")
                        elif "Sortie de Dame" in r['theme']:
                            st.warning("🧠 **Conseil du Coach :** Développe d'abord tes Cavaliers et Fous avant de sortir ta Dame.")
                        else:
                            st.info("🧠 **Conseil du Coach :** Analyse la réponse adverse la plus forcée sur ce coup.")
        else:
            st.info("Clique sur le bouton ci-dessus pour lancer l'analyse de cette partie.")
# Progress
with tabs[3]:
    st.subheader("📈 Progression")
    analyses = st.session_state.get("analyses_map", {})
    
    # Filtrer uniquement les parties ayant une analyse valide
    analyses_valides = {idx: recs for idx, recs in analyses.items() if recs}
    
    if not analyses_valides:
        st.info("Analyse quelques parties dans l'onglet 'Analyse' pour commencer à construire ton suivi de progression.")
    else:
        timeline = []
        for idx, recs in analyses_valides.items():
            g = games[int(idx)]
            date_str = g.headers.get("Date", "Inconnue")
            
            gaffes = sum(1 for r in recs if r["category"] == "Gaffe")
            erreurs = sum(1 for r in recs if r["category"] == "Erreur")
            imprecisions = sum(1 for r in recs if r["category"] == "Imprécision")
            note = calculer_note_partie(recs)
            
            timeline.append({
                "Partie": f"P{idx} ({date_str})",
                "Date": date_str,
                "Note Coach": note,
                "Gaffes": gaffes,
                "Erreurs": erreurs,
                "Imprécisions": imprecisions,
            })
            
        if timeline:
            df = pd.DataFrame(timeline).sort_values("Date")
            
            st.markdown("### 📊 Évolution des erreurs par partie")
            st.line_chart(df.set_index("Partie")[["Gaffes", "Erreurs", "Imprécisions"]])
            
            st.markdown("### 📈 Évolution de la Note du Coach")
            st.line_chart(df.set_index("Partie")[["Note Coach"]])
            
            st.markdown("### 📋 Historique détaillé")
            st.dataframe(df, use_container_width=True, hide_index=True)

        st.markdown("### 🎯 Axes de progression actuels")
        st.markdown(
            "- **1. Menaces adverses :** avant chaque décision critique, vérifier les échecs, captures et menaces.\n"
            "- **2. Calcul :** chercher 2–3 coups candidats avant de choisir.\n"
            "- **3. Conversion :** lorsqu'une position est meilleure, identifier le plan le plus simple.\n"
            "- **4. Gestion du temps :** réserver la réflexion longue aux positions réellement critiques."
        )

# Statistiques
with tabs[4]:
    st.subheader("📈 Mes performances par ouverture")
    
    # Appel de ta fonction d'extraction (games et username sont déjà définis dans ton code)
    df_stats = extraire_stats_ouvertures(games, username)

    if not df_stats.empty:
        # Sélecteur pour analyser spécifiquement tes parties avec les Blancs ou les Noirs
        couleur_choisie = st.radio("Analyser les parties avec les :", ["Blancs", "Noirs"], horizontal=True)
        
        # On filtre le dataframe selon la couleur choisie
        df_filtre = df_stats[df_stats["Couleur"] == couleur_choisie]
        
        if not df_filtre.empty:
            # On regroupe les données pour compter les victoires, nulles et défaites
            tableau_resume = df_filtre.groupby("Ouverture")["Résultat"].value_counts().unstack().fillna(0)
            
            # On s'assure que les 3 colonnes existent même s'il n'y a pas eu ce résultat
            for col in ["Victoire", "Nulle", "Défaite"]:
                if col not in tableau_resume.columns:
                    tableau_resume[col] = 0
                    
            # On calcule le total et on trie pour avoir les ouvertures les plus jouées en haut
            tableau_resume["Total"] = tableau_resume.sum(axis=1)
            tableau_resume = tableau_resume.sort_values("Total", ascending=False).astype(int)
            
            # Réorganisation de l'ordre des colonnes pour un affichage plus logique
            tableau_resume = tableau_resume[["Victoire", "Nulle", "Défaite", "Total"]]
            
            # Affichage du tableau interactif
            st.dataframe(tableau_resume, use_container_width=True)
            
            # Graphique en barres
            st.bar_chart(tableau_resume[["Victoire", "Nulle", "Défaite"]])
        else:
            st.info(f"Aucune partie trouvée avec les {couleur_choisie} dans l'échantillon analysé.")
    else:
        st.warning("Pas assez de données pour générer des statistiques sur les ouvertures.")

# Training & Resources
with tabs[5]:
    st.subheader("🎯 Entraînement & Ressources Pédagogiques")

    # ------------------------------------
    # SECTION 1 : BILAN THÉMATIQUE & VIDÉOS DYNAMIQUES
    # ------------------------------------
    st.markdown("### 📊 Tes Thèmes de Travail Prioritaires")
    
    analyses = st.session_state.get("analyses_map", {})
    themes_counter = Counter()
    
    for recs in analyses.values():
        if recs:
            for r in recs:
                if r["category"] in ["Gaffe", "Erreur"]:
                    themes_counter[r.get("theme", "Non classé")] += 1

    # Dictionnaire des vidéos adaptées à chaque faiblesse
    VIDEOS_PAR_THEME = {
        "Pièce suspendue (non protégée)": [
            ("🎥 Méthode pour ne plus donner de pièces", "https://www.youtube.com/results?search_query=eviter+les+gaffes+echecs+pieces+pendantes"),
            ("🎥 Vision tactique et sécurité des pièces", "https://www.youtube.com/results?search_query=vision+tactique+echecs+debutant")
        ],
        "Roi exposé / Échec subi": [
            ("🎥 La sécurité du Roi et le roque", "https://www.youtube.com/results?search_query=securite+du+roi+echecs"),
            ("🎥 Défendre contre une attaque directe", "https://www.youtube.com/results?search_query=defendre+une+attaque+echecs")
        ],
        "Tactique ou capture ratée": [
            ("🎥 Les motifs tactiques clés (Julien Song)", "https://www.youtube.com/results?search_query=motifs+tactiques+echecs+julien+song"),
            ("🎥 Comment calculer les coups candidats", "https://www.youtube.com/results?search_query=calculer+les+coups+candidats+echecs")
        ],
        "Sortie de Dame précoce / Perte de tempo": [
            ("🎥 Pourquoi il ne faut pas sortir la Dame trop tôt", "https://www.youtube.com/results?search_query=sortir+la+dame+trop+tot+echecs"),
            ("🎥 Les grands principes de l'ouverture", "https://www.youtube.com/results?search_query=principes+de+l+ouverture+echecs")
        ]
    }
                    
    if themes_counter:
        st.write("Répartition de tes erreurs par thématique :")
        df_themes = pd.DataFrame(list(themes_counter.items()), columns=["Thème", "Fréquence"]).sort_values("Fréquence", ascending=False)
        st.dataframe(df_themes, use_container_width=True, hide_index=True)
        
        top_theme = df_themes.iloc[0]["Thème"]
        st.error(f"🎯 **Axe d'entraînement principal :** `{top_theme}` (apparu {df_themes.iloc[0]['Fréquence']} fois).")
        
        # Affichage dynamique des cours vidéos selon l'axe principal
        st.markdown("#### 🎬 Vidéos recommandées pour corriger ce point :")
        cours_suggeres = VIDEOS_PAR_THEME.get(top_theme, [
            ("🎥 Travailler sa vision du jeu et sa régularité", "https://www.youtube.com/results?search_query=progression+echecs+conseils")
        ])
        for titre, url in cours_suggeres:
            st.markdown(f"* [{titre}]({url})")
    else:
        st.info("Analyse quelques parties dans l'onglet 'Analyse' pour générer ton bilan thématique et tes vidéos sur mesure.")

    st.divider()

    # ------------------------------------
    # SECTION 2 : EXERCICES DYNAMIQUES
    # ------------------------------------
    st.markdown("### 🧩 Tes Puzzles Personnalisés (issus de tes parties)")

    puzzles = []
    for idx, recs in analyses.items():
        if recs:
            for r in recs:
                if r["category"] in ["Gaffe", "Erreur"] and r.get("best"):
                    puzzles.append((idx, r))

    if not puzzles:
        st.info("Les moments clés à rejouer s'afficheront ici automatiquement dès qu'une partie sera analysée.")
    else:
        st.write(f"**{len(puzzles)} moment(s) critique(s)** détecté(s) :")
        
        selected_puzzle_idx = st.selectbox(
            "Choisis un moment à rejouer :",
            range(len(puzzles)),
            format_func=lambda i: f"Partie {puzzles[i][0]} — Coup {puzzles[i][1]['move_no']} ({puzzles[i][1].get('theme', puzzles[i][1]['category'])} : -{puzzles[i][1]['drop']/100:.2f} pions)"
        )

        p_idx, puzzle_data = puzzles[selected_puzzle_idx]

        st.markdown(f"**Situation (Coup {puzzle_data['move_no']}) :**")
        st.write(f"Tu as joué : **{puzzle_data['san']}** (Évaluation : {puzzle_data['eval_after']})")

        with st.expander("💡 Révéler la solution de Stockfish", expanded=False):
            st.success(f"Le meilleur coup recommandé était : **{puzzle_data['best']}**")
            st.write(f"Évaluation possible : **{puzzle_data['eval_before']}**")
            st.info("🎯 **Exercice mental :** Visualise l'échiquier et cherche pourquoi ce coup était supérieur.")

    st.divider()

    # ------------------------------------
    # SECTION 3 : MÉDIATHÈQUE PÉDAGOGIQUE
    # ------------------------------------
    st.markdown("### 📚 Bibliothèque Pédagogique Permanente")

    cat_tactique, cat_ouvertures, cat_finales, cat_outils = st.tabs([
        "🧩 Tactique & Calcul", 
        "📖 Ouvertures & Stratégie", 
        "👑 Finales", 
        "🛠️ Outils & Plateformes"
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

   
