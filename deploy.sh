#!/usr/bin/env bash
# ============================================================================
# Автонастройка сервера MAS Platform (Ubuntu 24.04, запуск под root).
#
# Запуск из веб-консоли сервера ОДНОЙ командой (подставьте свой логин GitHub):
#   curl -sL https://raw.githubusercontent.com/ЛОГИН/mas-platform/main/deploy.sh | bash -s -- ЛОГИН
#
# Что делает: ставит пакеты, московское время, создаёт пользователя mas,
# клонирует репозиторий, ставит библиотеки, регистрирует службы systemd
# (панель + планировщик), пробрасывает порт 80 -> 8000 и включает файрвол.
# Скрипт можно запускать повторно — ничего не сломает (идемпотентный).
# ============================================================================
set -euo pipefail

GH_USER="${1:?Ошибка: укажите логин GitHub, пример: ... | bash -s -- myloginname}"
REPO_URL="https://github.com/${GH_USER}/mas-platform.git"
APP_DIR=/home/mas/mas-platform

echo "== [1/7] Системные пакеты =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3-venv python3-pip git nano curl iptables-persistent ufw

echo "== [2/7] Часовой пояс Москва (для расписаний) =="
timedatectl set-timezone Europe/Moscow

echo "== [3/7] Пользователь приложения =="
id -u mas &>/dev/null || useradd -m -s /bin/bash mas

echo "== [4/7] Код из GitHub =="
if [ -d "$APP_DIR/.git" ]; then
    sudo -u mas git -C "$APP_DIR" pull
else
    sudo -u mas git clone "$REPO_URL" "$APP_DIR"
fi

echo "== [5/7] Python-окружение и библиотеки =="
[ -d "$APP_DIR/.venv" ] || sudo -u mas python3 -m venv "$APP_DIR/.venv"
sudo -u mas "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u mas "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# заготовка .env, чтобы осталось только вписать значения
sudo -u mas bash -c "test -f '$APP_DIR/.env' || printf 'TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nTELEGRAM_CHANNEL_ID=\n' > '$APP_DIR/.env'"

echo "== [6/7] Службы systemd =="
cat > /etc/systemd/system/mas-web.service <<'UNIT'
[Unit]
Description=MAS Platform web panel
After=network.target

[Service]
User=mas
WorkingDirectory=/home/mas/mas-platform
ExecStart=/home/mas/mas-platform/.venv/bin/uvicorn app.web.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/mas-scheduler.service <<'UNIT'
[Unit]
Description=MAS Platform scheduler
After=network.target

[Service]
User=mas
WorkingDirectory=/home/mas/mas-platform
ExecStart=/home/mas/mas-platform/.venv/bin/python scheduler.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable mas-web mas-scheduler

echo "== [7/7] Сеть: панель на порту 80, файрвол =="
# редирект 80 -> 8000, чтобы панель открывалась по обычному http://IP без порта
iptables -t nat -C PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000 2>/dev/null || \
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000
netfilter-persistent save >/dev/null

ufw allow 22/tcp    >/dev/null
ufw allow 2222/tcp  >/dev/null
ufw allow 8443/tcp  >/dev/null
ufw allow 80/tcp    >/dev/null
ufw allow 8000/tcp  >/dev/null
ufw --force enable  >/dev/null

echo ""
echo "============================================================"
echo " ГОТОВО. Осталось три ручных шага:"
echo ""
echo " 1) Впишите секреты Telegram:"
echo "      nano $APP_DIR/.env"
echo "    (Ctrl+O, Enter - сохранить; Ctrl+X - выйти)"
echo ""
echo " 2) База и пользователь панели:"
echo "      cd $APP_DIR && sudo -u mas .venv/bin/python seed.py"
echo "      cd $APP_DIR && sudo -u mas .venv/bin/python create_user.py"
echo ""
echo " 3) Запуск служб:"
echo "      systemctl start mas-web mas-scheduler"
echo ""
echo " Панель: http://ВАШ_IP  (просто IP в браузере, без порта)"
echo "============================================================"