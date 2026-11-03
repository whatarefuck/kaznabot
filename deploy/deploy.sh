#!/usr/bin/env bash
# Выкладка на сервер: deploy/deploy.sh root@host
# Настройки на сервере лежат в /etc/kaznabot.env (локальный .env не копируется), базы — в /var/lib/kaznabot.
set -euo pipefail
HOST="${1:?usage: deploy/deploy.sh root@host}"
cd "$(dirname "$0")/.."

rsync -az --delete \
    --exclude .venv --exclude .git --exclude .env --exclude data --exclude tests \
    --exclude __pycache__ --exclude '.*_cache' --exclude .claude \
    ./ "$HOST:/opt/kaznabot/"

# UV_PYTHON_INSTALL_DIR: Python должен лежать вне /root, иначе сервис не от root его не прочитает
ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/kaznabot
UV_PYTHON_INSTALL_DIR=/opt/uv/python uv sync --locked --no-dev --compile-bytecode
install -m 644 deploy/kaznabot.service /etc/systemd/system/kaznabot.service
systemctl daemon-reload
systemctl enable --quiet kaznabot
systemctl restart kaznabot
# Прогрев кэша шрифтов matplotlib, чтобы первый /chart не ждал минуту (каталог создаёт systemd при старте)
MPLCONFIGDIR=/var/lib/kaznabot/matplotlib .venv/bin/python -c "import matplotlib.font_manager"
sleep 5
systemctl --no-pager --lines=15 status kaznabot
REMOTE
