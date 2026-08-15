# ♟️ Mon Coach d'Échecs — V1

Prototype gratuit d'un coach personnel d'échecs.

## Fonctionnalités de la V1

- Connexion aux parties publiques Chess.com via le pseudo.
- Récupération des derniers mois de parties.
- Sélection d'une partie.
- Analyse locale avec Stockfish.
- Détection des pertes d'évaluation : imprécision / erreur / gaffe.
- Tableau de bord.
- Première couche de suivi dans le temps.
- Première proposition d'entraînement.
- Interface Streamlit responsive, utilisable sur téléphone.

## Lancer sur ordinateur

Installer Python 3.11 ou plus récent.

```bash
pip install -r requirements.txt
streamlit run app.py
```

Il faut également avoir Stockfish installé et accessible.

### Windows

Installer Stockfish depuis le site officiel :
https://stockfishchess.org/download/

Puis renseigner le chemin de l'exécutable dans la barre latérale de l'application, par exemple :

`C:\...\stockfish-windows-x64-avx2.exe`

### Linux

Le fichier `packages.txt` est prévu pour un déploiement Streamlit Community Cloud.
En local :

```bash
sudo apt install stockfish
```

## Déploiement gratuit

Le projet peut être envoyé sur GitHub puis déployé sur Streamlit Community Cloud.

1. Créer un dépôt GitHub.
2. Ajouter `app.py`, `requirements.txt`, `packages.txt` et `README.md`.
3. Créer une application Streamlit depuis `app.py`.
4. L'URL obtenue pourra être ouverte depuis un téléphone.

## Limites de la V1

Cette version est volontairement une base de travail.

Elle ne fait pas encore :

- l'analyse approfondie des positions critiques avec explication de plusieurs variantes ;
- le calcul d'une note de performance globale robuste ;
- la détection fiable des thèmes tactiques ;
- l'analyse des finales ;
- la reconnaissance fine des erreurs de gestion du temps ;
- la génération automatique d'exercices à partir des erreurs ;
- le stockage persistant complet de l'historique ;
- la génération de commentaires par un LLM ;
- l'authentification Chess.com.

Ces éléments sont prévus pour les versions suivantes.

## Philosophie du coach

Stockfish répond à la question :

> Quel coup est objectivement meilleur ?

Le Coach doit répondre à :

> Pourquoi ce joueur a-t-il probablement joué ce coup ?

et surtout :

> Est-ce un comportement qui se répète ?

L'objectif est donc de construire progressivement un véritable profil d'apprentissage plutôt qu'un simple analyseur Stockfish.
