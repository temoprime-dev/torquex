# -*- coding: utf-8 -*-
"""Человекочитаемые отказы генератора (RU и EN) вместо одних лишь кодов.

Задание `docs/TASK_FINISH_RU.md` §4.2: «коды ошибок сохранить, добавить
человекочитаемый текст». Модуль ничего не решает и не пишет: он только
переводит код отказа в понятную фразу «что случилось» + «что делать» на двух
языках. Если кода в таблице нет, возвращается ``None`` — вызывающий код обязан
показать исходный текст пакета, а не выдумывать объяснение.

Коды — единственный источник истины в самих ядрах (MS41..MS45); здесь только
представление. Поэтому расхождение словаря с ядром не ломает логику:
неизвестный код честно доходит до пользователя как есть.
"""

from __future__ import annotations

#: Языки, поддерживаемые `render`.
LANGUAGES = ("ru", "en")

#: Код → {"ru": (заголовок, что делать), "en": (…)}.
MESSAGES: dict[str, dict[str, tuple[str, str]]] = {
    # --- версия и образ -----------------------------------------------------
    "CAL_VERSION_UNSUPPORTED": {
        "ru": ("Версия ЭБУ не поддерживается генератором",
               "Ничего не записано. Для MS43 обнови ЭБУ до 430069 (это делает MS4x "
               "Flasher) либо выбери другую машину; для MS45 возьми тот же блок, что "
               "уже поддержан (список — в наряде)."),
        "en": ("This ECU version is not supported",
               "Nothing was written. On MS43 update the DME to 430069 (MS4x Flasher) "
               "or pick another car; on MS45 use a supported DME family (list in the "
               "delivery note)."),
    },
    "IMAGE_SIZE": {
        "ru": ("Не тот файл: нужно полное чтение ЭБУ",
               "Сделай полное чтение (MS43 — 512 КиБ, MS45 — 1 МиБ или калибровка "
               "116 КиБ) и повтори; частичное чтение калибровки для записи не годится."),
        "en": ("Wrong file: a full ECU read is required",
               "Take a full read (MS43 — 512 KiB, MS45 — 1 MiB or the 116 KiB "
               "calibration) and retry; a partial calibration read is not enough."),
    },
    "SOURCE_CRC_INVALID": {
        "ru": ("Сток не прошёл проверку контрольных сумм",
               "Прочитай ЭБУ заново и не меняй файл. Записывать по такому стоку нельзя."),
        "en": ("The stock image failed its checksums",
               "Read the ECU again and do not edit the file. Never flash from this "
               "stock."),
    },
    "SOURCE_CHECKSUM_INVALID": {
        "ru": ("Сток не прошёл проверку контрольных сумм",
               "Прочитай ЭБУ заново; файл повреждён или это не заводское чтение."),
        "en": ("The stock image failed its checksums",
               "Read the ECU again; the file is damaged or was edited."),
    },
    "OUTSIDE_CAL_REGION": {
        "ru": ("Правка вышла за калибровочную область",
               "Запись отменена. Программа, область VIN/ISN и подпись защищены: "
               "оставь как есть и сообщи об этом — это ошибка генератора, а не машины."),
        "en": ("The change left the calibration region",
               "Write cancelled. Program code, VIN/ISN area and signature are "
               "protected: report this, it is a generator bug, not a car problem."),
    },
    "OUTSIDE_CRC_COVERAGE": {
        "ru": ("Ячейка вне области контрольной суммы",
               "Запись отменена. Такой объект в первой версии не пишется."),
        "en": ("A cell lies outside checksum coverage",
               "Write cancelled. Such an object is not written in this version."),
    },
    # --- allowlist и ядро ---------------------------------------------------
    "SYMBOL_NOT_ALLOWED": {
        "ru": ("Объект не разрешён политикой версии",
               "Генератор пишет только проверенные объекты; запрос отклонён целиком."),
        "en": ("This object is not allowed by the version policy",
               "The generator only writes verified objects; the request was rejected."),
    },
    "READ_ONLY": {
        "ru": ("Объект защищён: только для чтения",
               "Штатная защита (детонация, overrev, контроль момента) не ослабляется "
               "ни за какие подтверждения."),
        "en": ("Protected object: read-only",
               "Stock protection (knock, overrev, torque monitoring) is never "
               "weakened, no acknowledgement changes that."),
    },
    "VALUE_NOT_REPRESENTABLE": {
        "ru": ("Значение не выражается выбранной калибровкой",
               "Попробуй ближайшее допустимое значение из наряда."),
        "en": ("The value cannot be represented by this calibration",
               "Pick the nearest allowed value shown in the delivery note."),
    },
    "QUANTIZATION_TOO_COARSE": {
        "ru": ("Шаг калибровки слишком грубый для запрошенного значения",
               "Запрос отклонён, чтобы не писать «похожее» вместо заказанного."),
        "en": ("The calibration step is too coarse for the requested value",
               "Rejected so that something 'similar' is not written instead."),
    },
    "DELTA_TOO_LARGE": {
        "ru": ("Отклонение от стока больше разрешённого",
               "Первая версия двигает значения только в консервативных границах; "
               "нужна отдельная аргументация и лог."),
        "en": ("The change is larger than the allowed delta",
               "This version moves values only inside conservative bounds; a log and "
               "a separate justification are required."),
    },
    "XDF_RANGE_VIOLATION": {
        "ru": ("Значение вне границ карты",
               "Запроси значение внутри границ, они перечислены в наряде."),
        "en": ("Value outside the table bounds",
               "Request a value inside the bounds listed in the delivery note."),
    },
    "OVERLAPPING_WRITES": {
        "ru": ("Две правки пытаются попасть в одну ячейку",
               "Убери конфликтующий запрос и повтори."),
        "en": ("Two changes target the same cell",
               "Remove the conflicting request and retry."),
    },
    "TOO_MANY_CELLS": {
        "ru": ("Запрос трогает слишком много ячеек",
               "Это защита от «переписать полторы карты»: сократи запрос."),
        "en": ("The request touches too many cells",
               "Guard against rewriting half a table: narrow the request."),
    },
    "UNPLANNED_BYTE_CHANGES": {
        "ru": ("В файле изменились байты вне плана",
               "Файл не выпускается: это внутренняя ошибка, пришли его разработчику."),
        "en": ("Bytes changed outside the plan",
               "The file is withheld: internal error, send it to the developer."),
    },
    # --- подтверждения и ворота --------------------------------------------
    "ACKNOWLEDGE_RISK_REQUIRED": {
        "ru": ("Нужно подтверждение риска",
               "Отсечка/лимитеры двигаются только с явным подтверждением "
               "(acknowledge_risk) — без него ничего не пишется."),
        "en": ("Risk acknowledgement required",
               "Rev limits change only with an explicit acknowledge_risk; nothing is "
               "written without it."),
    },
    "ACKNOWLEDGE_REQUIRED": {
        "ru": ("Нужно подтверждение условия",
               "Посмотри, какое именно подтверждение просит наряд, и подтверди его "
               "осознанно: генератор не подставляет его за клиента."),
        "en": ("An acknowledgement is required",
               "See which acknowledgement the delivery note asks for and confirm it "
               "knowingly; the generator never sets it for the client."),
    },
    "ACKNOWLEDGE_LEGALITY_REQUIRED": {
        "ru": ("Нужно подтверждение правовой справки",
               "Хлопки и диагностический слой выпускаются только после прочтения "
               "справки (RU/EN в наряде) и явного подтверждения."),
        "en": ("Legal notice acknowledgement required",
               "Pops and the diagnostic layer are released only after the notice "
               "(RU/EN in the delivery note) is read and explicitly acknowledged."),
    },
    "NO_CATALYST_REQUIRED": {
        "ru": ("Хлопки — только для машины без катализатора",
               "Подтверди no_catalyst: true, если катализатор физически снят; "
               "на машине с катализатором хлопки не выпускаются."),
        "en": ("Pops are only for a car without a catalytic converter",
               "Set no_catalyst: true if the converter is physically removed; pops "
               "are never released for a car that still has one."),
    },
    "LEGALITY_WARNING": {
        "ru": ("Правовая справка",
               "Отключение диагностики катализатора/лямбды в ряде стран незаконно "
               "для дорожной машины. Решение и ответственность — на владельце."),
        "en": ("Legal notice",
               "Disabling catalyst/lambda diagnostics is illegal for road use in many "
               "countries. The owner carries the decision and the responsibility."),
    },
    "HARDWARE_REQUIRED": {
        "ru": ("Нужны данные железа (Stage 2)",
               "Заполни блок hardware; без него Stage 2 не собирается — иначе это "
               "было бы угадывание."),
        "en": ("Hardware data required (Stage 2)",
               "Fill the hardware block; Stage 2 is not built without it, otherwise it "
               "would be guesswork."),
    },
    "VARIANT_UNKNOWN": {
        "ru": ("Неизвестный вариант поставки",
               "Выбери вариант 1, 2 или 3 (см. «torquex variants»)."),
        "en": ("Unknown delivery variant",
               "Pick variant 1, 2 or 3 (see 'torquex variants')."),
    },
    "VARIANT_SCOPE_VIOLATION": {
        "ru": ("Запрос не входит в выбранный вариант",
               "Либо убери этот запрос, либо выбери вариант, в который он входит."),
        "en": ("The request is outside the chosen variant",
               "Either drop this request or choose the variant that includes it."),
    },
    "INTENT_UNKNOWN": {
        "ru": ("Непонятный запрос",
               "Такой функции у генератора нет. Список доступного — в наряде "
               "(раздел «Пакеты»)."),
        "en": ("Unknown request",
               "The generator has no such function. See the delivery note ('Packages')."),
    },
    "NOTHING_REQUESTED": {
        "ru": ("В заявке нет ни одного запроса",
               "Добавь хотя бы одну функцию или выбери вариант 1."),
        "en": ("No requests in the order",
               "Add at least one function or choose variant 1."),
    },
    "NOTHING_TO_DO": {
        "ru": ("Все запросы уже совпадают со стоком",
               "Писать нечего: файл не выпускается, сток и есть результат."),
        "en": ("All requests already match the stock",
               "Nothing to write: no file is released, the stock is the result."),
    },
    # --- «ждём данных» ------------------------------------------------------
    "REQUIRES_DATA": {
        "ru": ("Для этой функции не хватает данных",
               "В наряде перечислено, что именно измерить/приложить (лог, профиль "
               "железа, механика). Догадок генератор не делает."),
        "en": ("Data is missing for this function",
               "The delivery note lists exactly what to measure or attach (log, "
               "hardware profile, mechanics). The generator never guesses."),
    },
    "EVIDENCE_REQUIRED": {
        "ru": ("Нужен лог-доказательство",
               "Зажигание/топливо/фазы правятся только по логу из Test0. Сними лог по "
               "инструкции и приложи его как evidence."),
        "en": ("Log evidence required",
               "Ignition/fuel/cam changes require a Test0 log. Take it per the guide "
               "and attach it as evidence."),
    },
    "LOG_DATA_REQUIRED": {
        "ru": ("Нужен лог из Test0",
               "Без лога значения не пишутся: смотри гайд по Test0 в docs/."),
        "en": ("A Test0 log is required",
               "Without a log nothing is written: see the Test0 guide in docs/."),
    },
    "ZONES_UNAVAILABLE": {
        "ru": ("В логе нет нужных зон (обороты/нагрузка)",
               "Пересними лог с полным прогревом и разгонами до отсечки, чтобы "
               "попали все зоны из требований."),
        "en": ("The log misses the required zones",
               "Retake the log with a warm engine and pulls to the limiter so that all "
               "required zones are covered."),
    },
    "PATCHLIST_FEATURE_UNSUPPORTED": {
        "ru": ("Эта фича патчлиста недоступна на твоей версии",
               "Фичи патчлиста работают только на 430069; обнови ЭБУ или убери запрос."),
        "en": ("This patchlist feature is unavailable on your version",
               "Patchlist features work on 430069 only; update the DME or drop the "
               "request."),
    },
    # --- откат и манифест ---------------------------------------------------
    "MANIFEST_MISMATCH": {
        "ru": ("Файл не соответствует манифесту",
               "Прошивать нельзя. Скорее всего файл повреждён при пересылке — "
               "запроси сборку заново."),
        "en": ("The file does not match its manifest",
               "Do not flash it. The file was probably damaged in transit — request a "
               "fresh build."),
    },
    "STOCK_MISMATCH": {
        "ru": ("Это не тот сток, из которого собран файл",
               "sha256 не совпал. Возьми сток именно с этой машины (или из манифеста "
               "этого файла); чужой сток не возвращай."),
        "en": ("This is not the stock the file was built from",
               "sha256 mismatch. Use the stock from this very car (or the manifest of "
               "this file); never restore someone else's stock."),
    },
    "STOCK_UNVERIFIED": {
        "ru": ("Сток не найден или не проверен",
               "Укажи файл стока ключом --stock; откат без него невозможен."),
        "en": ("Stock file missing or unverified",
               "Pass the stock file with --stock; a rollback is impossible without it."),
    },
    "NO_OUTPUT": {
        "ru": ("Нет готового файла",
               "Статус наряда — не BUILT: прочитай причины выше, ничего не записано."),
        "en": ("No output file",
               "The note status is not BUILT: read the reasons above, nothing was "
               "written."),
    },
    "UNSUPPORTED_IMAGE": {
        "ru": ("Файл не похож на чтение ЭБУ MS41/42/43/45",
               "Нужен заводской сток целиком (или калибровка известного размера). "
               "Проверь, что это .bin из программатора, а не тюнерский файл."),
        "en": ("The file does not look like an MS41/42/43/45 ECU read",
               "A full stock read (or a calibration of a known size) is required. Make "
               "sure it is a flasher .bin, not a tuner file."),
    },
    "VARIANT_TEMPLATE_MISSING": {
        "ru": ("Для этого ЭБУ вариант 2/3 пока не собирается автоматически",
               "Вариант 1 выпускается из коробки; для расширенного состава передай "
               "свою заявку --request (в MS41/MS42 сначала нужен профиль железа)."),
        "en": ("Variants 2/3 are not automated for this ECU yet",
               "Variant 1 works out of the box; for the extended composition pass your "
               "own --request (MS41/MS42 additionally need the hardware profile)."),
    },
    # --- серверная обёртка (часть 5) ---------------------------------------
    "LIMIT_EXCEEDED": {
        "ru": ("Лимит сборок исчерпан",
               "Это защита от потока заявок: подожди период (час или сутки) или "
               "напиши нам — лимит по VIN и учётной записи настраивается."),
        "en": ("Build limit reached",
               "This guards against request floods: wait for the period (hour or "
               "day) or contact us — per-VIN and per-account limits are configurable."),
    },
    "UPLOAD_TOO_LARGE": {
        "ru": ("Файл больше предела 1 МиБ",
               "Загрузи чтение ЭБУ целиком (MS45 — 1 МиБ, MS43 — 512 КиБ) или "
               "калибровку известного размера; ничего другого сервис не принимает."),
        "en": ("The file exceeds the 1 MiB limit",
               "Upload a full ECU read (MS45 — 1 MiB, MS43 — 512 KiB) or a "
               "calibration of a known size; nothing else is accepted."),
    },
    "REQUEST_INVALID": {
        "ru": ("Заявка непонятна",
               "Проверь JSON: нужен объект с полем intents (и, если есть, variant). "
               "Примеры — в MS41..MS45/examples/."),
        "en": ("The request is not understood",
               "Check the JSON: an object with an intents field (and optionally "
               "variant). Examples live in MS41..MS45/examples/."),
    },
    "VIN_REQUIRED": {
        "ru": ("Укажите VIN автомобиля",
               "VIN нужен для учёта и лимитов (17 символов, он написан в техпаспорте и на кузове). "
               "На сервере хранится только его хеш и последние 7 символов."),
        "en": ("Enter the car's VIN",
               "The VIN is needed for accounting and limits (17 characters, it is on the registration "
               "papers and on the body). The server keeps only its hash and the last 7 characters."),
    },
    "VIN_INVALID": {
        "ru": ("VIN не похож на VIN",
               "VIN — до 32 символов, буквы/цифры, '-' и '_'. VIN нужен только как "
               "метаданные учёта: генератор его не пишет и не показывает."),
        "en": ("The VIN does not look like a VIN",
               "A VIN is up to 32 chars, letters/digits plus '-' and '_'. It is used "
               "as accounting metadata only: the generator never writes or reveals it."),
    },
    "JOB_NOT_FOUND": {
        "ru": ("Результат не найден",
               "Файлы не хранятся на сервере долго (по умолчанию 15 минут) — это "
               "сделано специально. Загрузи сток и собери заново."),
        "en": ("Result not found",
               "Files are not kept on the server for long (15 minutes by default) — "
               "that is deliberate. Upload the stock and build again."),
    },
    "FILE_NOT_FOUND": {
        "ru": ("Файл не найден",
               "Проверь путь к файлу (для сервера — это ошибка сервиса, напиши нам)."),
        "en": ("File not found",
               "Check the path (inside the server this is a service error — report it)."),
    },
    "INTERNAL_ERROR": {
        "ru": ("Внутренняя ошибка сервиса",
               "Ничего не записано. Попробуй ещё раз и пришли нам время и журнал "
               "сборки — по нему видно код отказа."),
        "en": ("Internal service error",
               "Nothing was written. Retry and send us the time and the build log — "
               "it carries the refusal code."),
    },
    "VARIANT_REQUIRES_LOGS": {
        "ru": ("Вариант 2/3 требует логов и профиля железа",
               "Это не ошибка: сначала сними лог Test0 (и заполни профиль для "
               "MS41/MS42), затем передай заявку с путями к доказательствам "
               "--request. Сам генератор лог не выдумывает."),
        "en": ("Variants 2/3 need logs and a hardware profile",
               "Not an error: first take a Test0 log (and fill the profile on "
               "MS41/MS42), then pass the request with server-issued evidence. The "
               "generator never invents a log."),
    },
    "AWAITING_BENCH_VERIFICATION": {
        "ru": ("Нужна стендовая проверка",
               "Пакет собран и проверен по байтам, но API не выпускает его при "
               "bench_required=true. После реального протокола владелец может "
               "снять флаг; до этого сайт его скрывает."),
        "en": ("Bench verification is required",
               "The package passed byte-level checks, but the API with "
               "bench_required=true will not release it. The owner can clear the "
               "flag after a real bench report; the website hides it meanwhile."),
    },
    "LOG_CONSENT_REQUIRED": {
        "ru": ("Нужно согласие на обработку лога",
               "Передай consent=true. Лог нужен только для анализа пакета и "
               "хранится кратко в памяти."),
        "en": ("Log consent is required",
               "Send consent=true. The log is used only for package analysis and "
               "is kept briefly in memory."),
    },
    "LOG_ANALYSIS_REJECTED": {
        "ru": ("Лог не прошёл ворота",
               "Проверь частоту, каналы, физические пределы и полноту заездов. "
               "Ручной JSON вместо лога не принимается."),
        "en": ("The log failed the analysis gates",
               "Check rate, channels, physical limits and complete runs. A hand-"
               "written JSON is not accepted instead of the log."),
    },
    "LOG_INVALID": {
        "ru": ("Лог не разобран",
               "Загрузи CSV с каналами Test0 или родную SQLite .db; мусор и "
               "поддельные форматы отклоняются."),
        "en": ("The log could not be parsed",
               "Upload a Test0 CSV or native SQLite .db; junk and forged formats "
               "are rejected."),
    },
    "LOG_PATH_FORBIDDEN": {
        "ru": ("Путь к логу запрещён",
               "Передавай байты загрузкой /logs, а не путь к файлу сервера."),
        "en": ("Log paths are forbidden",
               "Upload the bytes to /logs instead of sending a server file path."),
    },
    "LOG_FORMAT_UNSUPPORTED": {
        "ru": ("Формат лога не поддерживается",
               "Разрешены только CSV и родная база Test0 с расширением .db."),
        "en": ("Unsupported log format",
               "Only CSV and the native Test0 .db format are accepted."),
    },
    "LOG_EVIDENCE_NOT_FOUND": {
        "ru": ("Доказательство лога не найдено",
               "Срок короткого хранения истёк или evidence_id неверен; загрузи лог снова."),
        "en": ("Log evidence was not found",
               "The short retention period expired or evidence_id is wrong; upload it again."),
    },
    "LOG_PACKAGE_MISMATCH": {
        "ru": ("Лог не соответствует пакету",
               "Выбери тот же пакет, для которого сервер анализировал лог."),
        "en": ("The log does not match the package",
               "Use the same package that the server analyzed for this log."),
    },
    # --- добровольная передача лога проекту (docs/LOG_DONATION_RU.md) -------
    "LOG_SHARE_DISABLED": {
        "ru": ("Передача логов проекту сейчас выключена",
               "Разбор лога выполнен, но копия не сохранена: на сервере передача "
               "выключена. Ничего делать не нужно."),
        "en": ("Sharing logs with the project is turned off",
               "Your log was analysed, but no copy was kept: sharing is turned off "
               "on the server. Nothing else to do."),
    },
    "LOG_SCRUB_FAILED": {
        "ru": ("Не удалось надёжно очистить лог — копия не сохранена",
               "Разбор лога доступен как обычно. Мы не храним копию, если не "
               "уверены, что в ней нет VIN и других личных данных."),
        "en": ("The log could not be cleaned reliably — no copy was kept",
               "The analysis is still available. We never keep a copy unless we "
               "are sure it has no VIN or other personal data."),
    },
    "LOG_STORE_FULL": {
        "ru": ("Хранилище логов проекта заполнено",
               "Разбор лога выполнен, копия не сохранена. Попробуй передать лог позже."),
        "en": ("The project's log storage is full",
               "Your log was analysed, but no copy was kept. Try sharing it later."),
    },
    "LOG_RATE_LIMITED": {
        "ru": ("Слишком много переданных логов за сутки",
               "Разбор лога выполнен, копия не сохранена. Передать ещё можно завтра."),
        "en": ("Too many shared logs today",
               "Your log was analysed, but no copy was kept. You can share more tomorrow."),
    },
    "LOG_SHARE_TOO_LARGE": {
        "ru": ("Очищенная копия лога слишком большая",
               "Разбор лога выполнен, копия не сохранена. Запиши поездку покороче."),
        "en": ("The cleaned log copy is too large",
               "Your log was analysed, but no copy was kept. Record a shorter drive."),
    },
    "LOG_DELETE_NOT_FOUND": {
        "ru": ("Лог не найден или код удаления неверный",
               "Проверь код удаления. Если срок хранения истёк, лог уже стёрт."),
        "en": ("Log not found or the deletion code is wrong",
               "Check the deletion code. If the retention period has passed, the log "
               "is already erased."),
    },
    "BENCH_VERIFIED": {
        "ru": ("Стендовая проверка включена владельцем",
               "Манифест получил уровень доверия bench verified; это не обещание мощности."),
        "en": ("Bench verification enabled by the owner",
               "The manifest is marked bench verified; this is not a power promise."),
    },
}


def explain(code: str, lang: str = "ru") -> dict[str, str] | None:
    """Понятное объяснение кода: ``{title, action}`` или ``None``."""
    entry = MESSAGES.get(str(code))
    if not entry:
        return None
    lang = lang if lang in LANGUAGES else "ru"
    title, action = entry.get(lang) or entry["ru"]
    return {"title": title, "action": action}


def render(code: str, lang: str = "ru", message: str = "") -> str:
    """Одна строка для человека: ``КОД: заголовок — что делать``.

    Если кода нет в таблице, возвращается исходное сообщение ядра с кодом —
    придумывать объяснение задним числом запрещено.
    """
    code = str(code)
    explained = explain(code, lang)
    if explained is None:
        return f"{code}: {message}" if message else code
    text = f"{code}: {explained['title']} — {explained['action']}"
    if message:
        text += f" [{message}]"
    return text


def known_codes() -> tuple[str, ...]:
    return tuple(sorted(MESSAGES))
