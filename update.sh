#!/usr/bin/env bash
# Доставка обновлений кода на сервер одной командой (запуск под root):
#   bash /home/mas/mas-platform/update.sh
# Забирает свежий код из GitHub, ставит новые библиотеки, перезапускает службы
# и показывает, какая версия теперь работает.
set -uo pipefail

APP_DIR=/home/mas/mas-platform

echo "== Было =="
BEFORE=$(sudo -u mas git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
grep -E '^VERSION' "$APP_DIR/app/version.py" 2>/dev/null || true
echo "коммит: $BEFORE"

echo ""
echo "== Забираю свежий код =="
if ! sudo -u mas git -C "$APP_DIR" pull --ff-only; then
    echo ""
    echo "⚠ Не удалось забрать код. Частые причины:"
    echo "   • репозиторий приватный и серверу нечем авторизоваться —"
    echo "     см. «Доступ к приватному репозиторию» в RUNBOOK.md (токен в remote URL);"
    echo "   • на сервере правили файлы руками — отмените правку:"
    echo "     sudo -u mas git -C $APP_DIR checkout -- <файл>"
    exit 1
fi

echo ""
echo "== Обновляю библиотеки =="
sudo -u mas "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo ""
echo "== Перезапускаю службы =="
systemctl restart mas-web mas-scheduler
sleep 2
systemctl is-active mas-web mas-scheduler

AFTER=$(sudo -u mas git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
echo ""
echo "== Стало =="
grep -E '^VERSION' "$APP_DIR/app/version.py" 2>/dev/null || true
echo "коммит: $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then
    echo ""
    echo "ℹ Код не изменился (коммит тот же). Если ждали обновление —"
    echo "  проверьте, что на компьютере сделан git push."
else
    echo ""
    echo "✅ Обновлено. Версию видно в подвале панели — сверьте, что она сменилась."
fi
