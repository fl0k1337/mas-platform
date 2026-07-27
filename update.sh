#!/usr/bin/env bash
# Доставка обновлений кода на сервер одной командой (запуск под root):
#   bash /home/mas/mas-platform/update.sh
# Забирает свежий код из GitHub, ставит новые библиотеки, перезапускает службы.
set -euo pipefail

APP_DIR=/home/mas/mas-platform

echo "== Забираю свежий код =="
sudo -u mas git -C "$APP_DIR" pull

echo "== Обновляю библиотеки =="
sudo -u mas "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "== Перезапускаю службы =="
systemctl restart mas-web mas-scheduler
sleep 2
systemctl --no-pager --lines=0 status mas-web mas-scheduler | grep -E "service|Active" || true

echo "Обновление применено."
