#!/usr/bin/env bash
# Construction du service web. Toute etape qui echoue arrete le deploiement :
# mieux vaut ne pas deployer qu'exposer une version a moitie migree.
set -o errexit
set -o nounset
set -o pipefail

pip install --upgrade pip
pip install -e .
pip install "gunicorn==23.0.*"

python manage.py collectstatic --no-input
python manage.py migrate --no-input

# Jeu de demonstration : la page d'accueil d'une demonstration vide ne montre
# rien. La commande est idempotente, elle ne duplique pas a chaque deploiement.
python manage.py seed_demo

# Les scores sont calcules a la construction plutot qu'a la demande : le
# moteur est deterministe et coute quelques millisecondes, mais un visiteur qui
# arrive sur un classement vide repart sans avoir rien vu.
python manage.py score_all --quiet || true
