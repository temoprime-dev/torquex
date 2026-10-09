#!/bin/bash
# Подключение уведомлений Telegram для обратной связи (запускает владелец на сервере).
#
#   1. В Telegram откройте @BotFather -> /newbot -> получите токен вида 123456:ABC...
#   2. Откройте своего нового бота и нажмите Start (или напишите ему любое сообщение).
#   3. Положите токен в файл /root/tg_token.txt (или введите вручную) и запустите:
#        bash /opt/torquex-gen/app/server/setup_telegram.sh
#
# Скрипт сам находит ваш chat_id, сохраняет резервную копию api.env, прописывает
# переменные, перезапускает сервис и шлёт тестовое сообщение. Файл с токеном
# удаляется только после успешной настройки.
# Откат: cp /root/api.env.bak-tg /etc/torquex-gen/api.env && systemctl restart torquex-gen
set -uo pipefail
ENVF=/etc/torquex-gen/api.env
TOKEN_FILE=/root/tg_token.txt

if [ -s "$TOKEN_FILE" ]; then
  TOKEN=$(python3 -c 'import re,sys; print(re.sub(r"[^0-9A-Za-z:_-]", "", open(sys.argv[1], encoding="utf-8-sig").read()))' "$TOKEN_FILE")
  echo "Токен прочитан из файла (длина ${#TOKEN} символов)."
else
  read -r -s -p "Токен бота от BotFather: " TOKEN
  echo
  TOKEN=$(printf '%s' "$TOKEN" | python3 -c 'import re,sys; print(re.sub(r"[^0-9A-Za-z:_-]", "", sys.stdin.read()))')
fi
[ -n "$TOKEN" ] || { echo "Токен пустой"; exit 1; }

ME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe")
echo "$ME" | grep -q '"ok":true' || { echo "Telegram не принял токен. Ответ: $ME"; exit 1; }
echo "Бот найден: $(printf '%s' "$ME" | python3 -c 'import sys,json; print(json.load(sys.stdin)["result"]["username"])')"

CHAT=""
for i in 1 2 3 4 6 8; do
  CHAT=$(curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c '
import sys, json
data = json.load(sys.stdin)
ids = [u["message"]["chat"]["id"] for u in data.get("result", []) if "message" in u]
print(ids[-1] if ids else "")')
  [ -n "$CHAT" ] && break
  echo "Жду, пока вы напишете боту (нажмите Start)... ($i)"
  sleep 5
done
[ -n "$CHAT" ] || { echo "Сообщений боту не найдено: откройте бота, нажмите Start и запустите скрипт снова (файл токена сохранён)"; exit 1; }

cp "$ENVF" /root/api.env.bak-tg
sed -i '/^TORQUEX_TG_TOKEN=/d;/^TORQUEX_TG_CHAT_ID=/d' "$ENVF"
printf 'TORQUEX_TG_TOKEN=%s\nTORQUEX_TG_CHAT_ID=%s\n' "$TOKEN" "$CHAT" >> "$ENVF"
chown root:torquex-gen "$ENVF"; chmod 0640 "$ENVF"
systemctl restart torquex-gen
sleep 2
curl -s -o /dev/null "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=TorqueX: уведомления об обратной связи подключены."
[ -f "$TOKEN_FILE" ] && { shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"; }
echo "Готово: chat_id=${CHAT}. Тестовое сообщение отправлено."
