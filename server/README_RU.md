# `server/` — что это

* `core.py` — всё, что не зависит от фреймворка: определение ЭБУ, сборка,
  лимиты на VIN/учётную запись, журнал (хеши вместо VIN и IP), короткое
  хранение результатов (по умолчанию 15 минут). Покрыто тестами
  `tests/test_server_core.py` и работает без FastAPI.
* `app.py` — тонкий FastAPI-слой: `/health`, `/versions`, `/detect`, `/logs`,
  `/build`, `/jobs/{id}`, `/jobs/{id}/file`, `/journal` (с админ-токеном).
  `/logs` принимает только байты CSV/SQLite Test0, `consent=true` и пакет;
  возвращает короткоживущий `evidence_id`, а не путь/VIN или доверенный клиентский
  JSON. Слушает `127.0.0.1`; наружу выставляет nginx.
* `config/torquex.json` по умолчанию держит `bench_required=true`: новые
  MS42/MS43 ignition, pedal-response и осторожный VANOS Stage 1 собраны в коде,
  но публичный API отвечает `AWAITING_BENCH_VERIFICATION` до явного снятия
  флага владельцем. Снятие флага даёт манифесту `trust_level: bench verified`,
  а не обещание мощности.
* `DEPLOY_RU.md` — запуск, переменные окружения, unit для systemd и блок nginx.

В репозитории только код и документы: прод-сервер torquex.live ведёт владелец,
здесь ничего не разворачивается и не настраивается.
