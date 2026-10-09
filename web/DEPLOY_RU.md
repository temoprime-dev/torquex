# Развёртывание сайта (`web/`) на nginx

Сайт — статика без сборки: HTML, CSS и несколько `.js`. Ни сборщика, ни npm,
ни внешних запросов. Достаточно скопировать каталог и настроить один
`server {}`-блок. Разворачивает **владелец сервера**; этот файл — инструкция
и пример конфигурации, ничего здесь не выполняется автоматически.

Парный документ про бэкенд — `server/DEPLOY_RU.md` (systemd-юнит, переменные
окружения, журнал). Здесь — только статика и проксирование `/api/`.

## 0. Что уже учтено в коде

* Все пути **относительные**: `style.css`, `i18n.js`, `versions.html` и т. д.
  Сайт одинаково работает и в корне домена, и в подкаталоге `/torquex/`.
* Префикс API задан в `web/config.js` как `apiBase: "api"` — тоже
  относительный. Из `https://site/torquex/index.html` запрос уйдёт на
  `https://site/torquex/api/versions`, из корня — на `https://site/api/versions`.
  Поэтому один и тот же бандл годится и для домена, и для префикса.
* Внешних запросов нет: ни CDN, ни шрифтов, ни аналитики, ни пикселей.
  Проверяется тестом `tests/test_web_static.py`.
* Заголовки безопасности сайт **не ставит** и не может ставить: это статика.
  Их задаёт nginx (пример ниже).
* Ссылка на донат приходит с сервера полем `donate_url`; в репозитории
  `donateUrl` пустой, платёжных форм на сайте нет.

> `apiBase: "api"` (без ведущей косой черты) — осознанный выбор. Пустая строка
> дала бы корневые адреса `/versions`, и сайт перестал бы работать из
> подкаталога. Это проверяет `node web/tools/check_build_states.mjs`: в
> `config.js` не должно быть ни схемы, ни хоста.

## 1. Куда класть файлы

```bash
sudo mkdir -p /var/www/torquex
sudo rsync -a --delete \
    --exclude 'tools/' --exclude 'screenshots/' --exclude '*.md' \
    web/ /var/www/torquex/
sudo chown -R root:www-data /var/www/torquex
sudo find /var/www/torquex -type f -exec chmod 0644 {} +
sudo find /var/www/torquex -type d -exec chmod 0755 {} +
```

Наружу уходят только `*.html`, `*.css`, `*.js`. Каталоги `tools/` (служебные
проверки) и `screenshots/`, а также `.md` на боевой машине не нужны.

После копирования правится **один** файл — `/var/www/torquex/config.js`:

```js
window.TORQUEX_CONFIG = {
  apiBase: "api",      // nginx проксирует /api/ → 127.0.0.1:8081
  donateUrl: "",       // адрес доната отдаёт сервер полем donate_url
};
```

## 2. На сервере уже есть другие приложения

По `server/DEPLOY_RU.md` и текущей конфигурации владельца на машине заняты:

| Путь | Куда идёт | Чьё |
|---|---|---|
| `/api/` | `127.0.0.1:8000` | существующее приложение |
| `/` | `127.0.0.1:8100` | существующее приложение |

Если добавить `location /api/ → 127.0.0.1:8081` в тот же `server {}`, он
перехватит `/api/` у старого приложения и сломает его. Поэтому TorqueX ставится
**в стороне**. Два безопасных варианта:

### Вариант А (рекомендуемый): отдельный поддомен

`torquex.example.com` — свой `server {}`, ничего общего с текущими путями.
Нужны DNS-запись и сертификат на поддомен.

### Вариант Б: префикс `/torquex/` на том же домене

Трогаем только пути, начинающиеся с `/torquex/`. Остальное (`/api/`, `/`)
остаётся как было. Сертификат и DNS не нужны.

Оба варианта работают без правки `config.js`: пути относительные.

## 3. nginx — вариант А (отдельный поддомен)

```nginx
limit_req_zone $binary_remote_addr zone=torquex_site:10m rate=5r/s;

server {
    listen 443 ssl http2;
    server_name torquex.example.com;

    ssl_certificate     /etc/letsencrypt/live/torquex.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/torquex.example.com/privkey.pem;

    root /var/www/torquex;
    index index.html;
    charset utf-8;

    # Заголовки безопасности ставит nginx: сайт — статика и сам их выставить не может.
    add_header X-Content-Type-Options    "nosniff"            always;
    add_header X-Frame-Options           "DENY"               always;
    add_header Referrer-Policy           "no-referrer"        always;
    add_header Permissions-Policy        "geolocation=(), camera=(), microphone=(), interest-cohort=()" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    # Внешних ресурсов у сайта нет, поэтому политика максимально узкая.
    add_header Content-Security-Policy
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'" always;

    client_max_body_size 2m;          # внутри генератора жёсткий предел 1 МиБ

    location = /config.js {
        add_header Cache-Control "no-store" always;   # настройки развёртывания не кэшируем
    }

    location ~* \.(css|js)$ {
        add_header Cache-Control "public, max-age=3600" always;
    }

    location / {
        try_files $uri $uri/ =404;    # SPA-роутинга нет: страницы настоящие
        add_header Cache-Control "no-cache" always;
    }

    location /api/ {
        limit_req zone=torquex_site burst=10 nodelay;
        proxy_pass         http://127.0.0.1:8081/;   # косая черта на конце срезает /api
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $remote_addr;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;                     # сборка MS45 занимает секунды, запас большой
        proxy_request_buffering on;
    }

    # Журнал наружу не открываем ни при каких условиях (нужен админ-токен).
    location /api/journal { deny all; }
}

server {
    listen 80;
    server_name torquex.example.com;
    return 301 https://$host$request_uri;
}
```

Косая черта в конце `proxy_pass http://127.0.0.1:8081/;` обязательна: без неё
бэкенд получит `/api/versions` вместо `/versions`.

## 4. nginx — вариант Б (префикс на общем домене)

Добавляется в **существующий** `server {}`; блоки `location /api/` и
`location /` текущих приложений не трогаются.

```nginx
# TorqueX: статика по /torquex/, API по /torquex/api/.
location /torquex/ {
    alias /var/www/torquex/;
    index index.html;
    try_files $uri $uri/ =404;

    add_header X-Content-Type-Options "nosniff"     always;
    add_header X-Frame-Options        "DENY"        always;
    add_header Referrer-Policy        "no-referrer" always;
    add_header Content-Security-Policy
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'" always;
}

location = /torquex/config.js {
    alias /var/www/torquex/config.js;
    add_header Cache-Control "no-store" always;
}

location /torquex/api/ {
    proxy_pass         http://127.0.0.1:8081/;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-For $remote_addr;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
    client_max_body_size 2m;
}

location /torquex/api/journal { deny all; }
```

Порты `127.0.0.1:8000` и `127.0.0.1:8100` остаются за прежними приложениями:
TorqueX слушает только `127.0.0.1:8081`.

## 5. Проверка после развёртывания

Подставьте свой адрес: `BASE=https://torquex.example.com` либо
`BASE=https://существующий-домен/torquex`.

```bash
# 1. конфигурация nginx синтаксически верна и перечитана
sudo nginx -t && sudo systemctl reload nginx

# 2. бэкенд жив (локально, мимо nginx)
curl -s http://127.0.0.1:8081/health

# 3. статика отдаётся
curl -sI "$BASE/index.html" | head -5
curl -s  "$BASE/config.js"

# 4. API виден сайту по относительному пути
curl -s "$BASE/api/health"
curl -s "$BASE/api/versions" | head -c 200

# 5. заголовки безопасности на месте
curl -sI "$BASE/index.html" | grep -iE 'content-security-policy|x-frame-options|x-content-type-options|referrer-policy'

# 6. журнал закрыт снаружи
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/api/journal"     # ожидается 403

# 7. старые приложения не задеты (только для варианта Б)
curl -s -o /dev/null -w '%{http_code}\n' https://существующий-домен/api/
curl -s -o /dev/null -w '%{http_code}\n' https://существующий-домен/
```

## 6. Чеклист после развёртывания

- [ ] `nginx -t` без ошибок, nginx перезапущен.
- [ ] `$BASE/index.html` открывается, в консоли браузера нет ошибок.
- [ ] `$BASE/api/health` отвечает `{"status":"ok", …}`.
- [ ] На странице «Поддерживаемые версии» таблица заполнена из `/versions`,
      а не показывает пустое состояние.
- [ ] В `config.js` на сервере `apiBase` без схемы и хоста, `donateUrl` пустой
      (или боевой адрес, если владелец его задал).
- [ ] Вкладка «Сеть» в браузере: все запросы идут на свой домен, внешних нет.
- [ ] Заголовки `Content-Security-Policy`, `X-Frame-Options`,
      `X-Content-Type-Options`, `Referrer-Policy` приходят на `index.html`.
- [ ] `$BASE/api/journal` снаружи даёт 403.
- [ ] Загрузка файла больше 1 МиБ отклоняется (сайт — сразу, сервер — 413/422).
- [ ] Для варианта Б: `/` и `/api/` старых приложений отвечают как раньше.
- [ ] Проверен откат: `rsync` предыдущей копии `web/` обратно и `nginx -s reload`.

## 7. Чего здесь нет

* Автоматического развёртывания: ни CI, ни скриптов с `ssh` — разворачивает
  владелец вручную.
* HTTP-заголовков из кода: статика их не ставит, всё в nginx.
* Кэша с длинным сроком: файлы без хеша в имени, долгий `max-age` приведёт к
  тому, что пользователь увидит старый `i18n.js` после обновления.
* Платёжных форм: донат — это ссылка из `donate_url`, не больше.
