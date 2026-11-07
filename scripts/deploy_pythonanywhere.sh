#!/usr/bin/env bash
# Update the live site on PythonAnywhere. Run from a PythonAnywhere Bash console:
#   bash ~/Finance-Tracker-Zuuvi/scripts/deploy_pythonanywhere.sh
set -euo pipefail

APP_DIR="$HOME/Finance-Tracker-Zuuvi"
VENV="$HOME/.virtualenvs/zuuvi"
WSGI_FILE="/var/www/${USER}_pythonanywhere_com_wsgi.py"

cd "$APP_DIR"
source "$VENV/bin/activate"

# Same settings as the live site (secret key etc.), so manage.py commands match production.
set -a
source "$HOME/.zuuvi_env"
set +a
export DJANGO_DEBUG=False
export DJANGO_ALLOWED_HOSTS="${USER}.pythonanywhere.com"

git pull --ff-only
pip install --quiet -r requirements.txt
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Touching the WSGI file reloads the web app.
touch "$WSGI_FILE"
echo "Deployed. Site reloaded: https://${USER}.pythonanywhere.com"
