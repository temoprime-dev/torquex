# `server/` — развёртывание (RU)

**В репозитории только код и документы.** Прод-сервер torquex.live (nginx +
uvicorn) ведёт владелец: этот документ описывает, как поднять обёртку на своей
машине и как её подключить к nginx, но ничего на прод-сервере не меняет.

## 1. Что нужно

* Python **3.11+**, venv, зависимости: `pip install -r server/requirements.txt`
  (`fastapi`, `uvicorn`, `python-multipart`). Ядро `server/core.py` работает и
  без них — тесты `tests/test_server_core.py` идут без FastAPI.
* Само приложение **слушает только `127.0.0.1`** в production-примере: наружу его отдаёт nginx.
* Никаких внешних сетевых обращений приложение не делает; чужие пути/имена
  файлов не используются. `/logs` получает байты CSV/SQLite Test0, повторно
  запускает `tools/pack_log_analyzer.py` на сервере и не принимает VIN или путь
  как доказательство.
* `bench_required=true` — tracked default в `config/torquex.json`. До bench
  протокола API скрывает MS42/MS43 ignition/pedal/VANOS Stage 1 и возвращает
  `AWAITING_BENCH_VERIFICATION`; override для владельца —
  `TORQUEX_BENCH_REQUIRED=false`.

## 2. Переменные окружения

| Переменная | По умолчанию | Зачем |
| --- | --- | --- |
| `TORQUEX_HOST` | `127.0.0.1` | адрес прослушивания; наружу не выставлять |
| `TORQUEX_PORT` | `8081` | порт приложения |
| `TORQUEX_IP_SALT` | случайный на процесс | **задать в проде**: соль для хеша IP; без неё хеш меняется после рестарта |
| `TORQUEX_ADMIN_TOKEN` | не задан | доступ к `GET /journal`; без него журнал закрыт |
| `TORQUEX_JOURNAL_PATH` | не задан | файл журнала (JSON Lines); строки — только хеши и хвост VIN |
| `TORQUEX_JOB_TTL_SECONDS` | `900` | сколько секунд результат живёт в памяти |
| `TORQUEX_DONATE_URL` | пусто | заглушка доната: ссылка в ответах (`donate_url`) |
| `TORQUEX_GENERATOR_VERSION` | `dev` | версия генератора для журнала |
| `TORQUEX_BENCH_REQUIRED` | из `config/torquex.json` (`true`) | release gate новых MS42/MS43 пакетов; владелец снимает после стенда |
| `TORQUEX_LIMIT_VIN_PER_DAY` | `5` | лимит сборок на VIN в сутки |
| `TORQUEX_LIMIT_ACCOUNT_PER_HOUR` | `10` | лимит сборок на учётную запись в час |
| `TORQUEX_LIMIT_ACCOUNT_PER_DAY` | `20` | лимит сборок на учётную запись в сутки |
| `TORQUEX_LOG_DONATION` | выкл. | `on` — принимать добровольно переданные очищенные логи (`share_consent`); см. `docs/LOG_DONATION_RU.md` |
| `TORQUEX_LOG_STORE` | `data/logs` в корне репозитория | каталог очищенных копий; **не** внутри `web/` и не в корне nginx |
| `TORQUEX_LOG_RETENTION_DAYS` | `365` | срок хранения копий; старше — удаляются при старте и `tools/purge_logs.py` |
| `TORQUEX_LOG_STORE_MAX_BYTES` | `2147483648` | потолок каталога (2 ГиБ) → `LOG_STORE_FULL` |
| `TORQUEX_LOG_MAX_STORED_BYTES` | `20971520` | предел одной копии (20 МиБ) → `LOG_SHARE_TOO_LARGE` |
| `TORQUEX_LOG_SHARES_PER_IP_PER_DAY` | `5` | передач на хеш IP в сутки → `LOG_RATE_LIMITED` (счётчик в памяти) |

Платежей нет: донат — только ссылка из `TORQUEX_DONATE_URL`.

## 3. Запуск вручную

```bash
cd /opt/torquex
python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt
TORQUEX_IP_SALT=$(head -c 16 /dev/urandom | xxd -p) \
TORQUEX_ADMIN_TOKEN=$(head -c 24 /dev/urandom | xxd -p) \
TORQUEX_DONATE_URL=https://torquex.live/donate \
TORQUEX_JOURNAL_PATH=/var/lib/torquex/journal.jsonl \
.venv/bin/uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8081
```

## 4. systemd (пример; применяет владелец)

```ini
# /etc/systemd/system/torquex-api.service
[Unit]
Description=TorqueX API (FastAPI)
After=network.target

[Service]
User=torquex
Group=torquex
WorkingDirectory=/opt/torquex
Environment=TORQUEX_HOST=127.0.0.1
Environment=TORQUEX_PORT=8081
Environment=TORQUEX_JOB_TTL_SECONDS=900
Environment=TORQUEX_DONATE_URL=https://torquex.live/donate
EnvironmentFile=/etc/torquex/api.env
ExecStart=/opt/torquex/.venv/bin/uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/torquex

[Install]
WantedBy=multi-user.target
```

`/etc/torquex/api.env` (права `0600`, владелец `torquex`) держит секреты:

```
TORQUEX_IP_SALT=…
TORQUEX_ADMIN_TOKEN=…
TORQUEX_JOURNAL_PATH=/var/lib/torquex/journal.jsonl
TORQUEX_GENERATOR_VERSION=…
# Передача логов — только после чек-листа docs/LOG_DONATION_RU.md §8:
# TORQUEX_LOG_DONATION=on
TORQUEX_LOG_STORE=/var/lib/torquex/logs
TORQUEX_LOG_RETENTION_DAYS=365
```

Каталог данных: `install -d -o torquex -g torquex -m 0700 /var/lib/torquex/logs`.
Он уже покрыт `ReadWritePaths=/var/lib/torquex`; файлы пишутся с правами `0600`.

## 5. nginx (пример; применяет владелец)

```nginx
limit_req_zone $binary_remote_addr zone=torquex_api:10m rate=5r/s;

server {
    listen 443 ssl http2;
    server_name torquex.live;

    client_max_body_size 2m;          # у нас жёсткий предел 1 МиБ внутри (и для /logs)
    limit_req zone=torquex_api burst=10 nodelay;

    location /api/ {
        proxy_pass         http://127.0.0.1:8081/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    # Журнал наружу не открываем: только с админ-токеном и только из офиса/VPN.
    location /journal { deny all; }
}
```

Адрес в `X-Forwarded-For` приложение превращает в хеш; сам IP нигде не хранится.

## 6. Проверка после запуска

```bash
curl -s http://127.0.0.1:8081/health
curl -s http://127.0.0.1:8081/versions | head

# определение ЭБУ (любой сток из репозитория)
curl -s -F "file=@MS45/firmware/MS45.0_456BO00S_E83/7561448_0044560BO00S0CO0R.bin" \
     http://127.0.0.1:8081/detect

# анализ Stage 2 лога (не передавай VIN/путь в JSON; сервер вернёт opaque evidence_id)
curl -s -F "file=@MS43/examples/logs/wot_wideband.csv" \
     -F "package=fuel" -F "consent=true" \
     http://127.0.0.1:8081/logs

# сборка варианта 1 (заявка — JSON-строкой в поле request).
# Для защищённых MS42/MS43 пакетов сначала нужен bench override/протокол.
curl -s -F "file=@…/сток.bin" -F "account=demo" -F "vin=WBA…" \
     -F 'request={"variant":"v1","intents":[{"kind":"ignition","delta_deg":1.0}],"log_evidence_id":"<id-from-/logs>"}' \
     http://127.0.0.1:8081/build | head -c 400

# добровольная передача очищенной копии (нужен TORQUEX_LOG_DONATION=on);
# ответ содержит share.log_id и share.delete_token — показать пользователю один раз
curl -s -F "file=@MS43/examples/logs/wot_wideband.csv"      -F "package=fuel" -F "consent=true" -F "share_consent=true"      http://127.0.0.1:8081/logs
# удаление копии по коду
curl -s -X DELETE -H "X-Delete-Token: <delete_token>" http://127.0.0.1:8081/logs/<log_id>
# проверка, что в копиях нет VIN-подобных строк (должно быть пусто)
grep -rlE '[A-HJ-NPR-Z0-9]{17}' /var/lib/torquex/logs/*.csv

# журнал (только админ)
curl -s -H "X-Admin-Token: $TORQUEX_ADMIN_TOKEN" http://127.0.0.1:8081/journal
```

Ошибки приходят как JSON: `{"code": "...", "title_ru": "...", "title_en": "..."}`
— те же коды, что в CLI (`python3 tools/torquex.py explain <КОД>`).

## 7. Что важно помнить

* **Не выставлять приложение напрямую в интернет** — только через nginx.
* Стоки и результаты не хранятся: временный файл на время сборки, результат — в
  памяти до `TORQUEX_JOB_TTL_SECONDS`. Журнал — только хеши.
* VIN не пишется в открытом виде: `sha256(vin)` + последние 7 символов.
* Лимиты (VIN/учётная запись) — стартовые; меняются переменными окружения.
* Никакой платёжной части нет, донат — заглушка-ссылка.
* В журнале видно код отказа (`LIMIT_EXCEEDED`, `CAL_VERSION_UNSUPPORTED`,
  `UPLOAD_TOO_LARGE`, …) — по нему клиенту объясняется, что не так.

## 8. Переданные логи: очистка по сроку и бэкапы

Переданные копии (`docs/LOG_DONATION_RU.md`) — единственные данные
пользователей, которые хранятся дольше 15 минут. Поэтому:

* **Автоудаление.** Сервис чистит просроченные копии при старте; ежедневно —
  cron/timer:

  ```cron
  # /etc/cron.d/torquex-logs (пример; применяет владелец)
  17 4 * * * torquex cd /opt/torquex && TORQUEX_LOG_STORE=/var/lib/torquex/logs       TORQUEX_LOG_RETENTION_DAYS=365 .venv/bin/python tools/purge_logs.py
  ```

  `tools/purge_logs.py --dry-run` — только посчитать.
* **Бэкапы.** Копии даны под согласие со сроком и правом удаления по коду.
  Либо исключить `/var/lib/torquex/logs` из бэкапов, либо держать бэкапы не
  дольше срока хранения и помнить, что удалённая по коду копия живёт в
  бэкапе до его ротации (написать это в тексте согласия).
* **nginx.** Каталог не должен попадать в `root`/`alias`; наружу отдаются
  только `POST /logs` и `DELETE /logs/{id}` через `/api/`. Отдельный
  `client_max_body_size` не нужен (тот же предел 1 МиБ).
