# -*- coding: utf-8 -*-
"""TorqueX — единая точка входа для новичка (задание `docs/TASK_FINISH_RU.md` §4).

Что делает:

* ``detect`` — по файлу сам определяет ЭБУ и версию (MS41/42/43/45, код версии
  или семейство), ничего не спрашивая про адреса и числа;
* ``variants`` — показывает три варианта поставки: что получишь и чего не
  получишь;
* ``build`` — собирает файл по варианту (или по своей заявке ``--request``) и
  пишет готовый файл, наряд и манифест; отказы печатаются понятным текстом на
  русском и английском (коды сохраняются), без выдуманных шагов;
* ``rollback`` — возврат на сток одной командой: сверяет sha256 стока с
  манифестом (``source_sha256``), при необходимости проверяет и выпущенный
  файл, и отдаёт восстановленный образ только после успешной сверки;
* ``explain`` — человекочитаемое объяснение кода отказа (RU/EN).

Модуль сознательно не содержит адресов, карт и порогов: всё это — в политиках
и ядрах MS41..MS45, а здесь только диспетчер и тексты. Жёсткие значения по
умолчанию (задание §4.5): защита по детонации включена (её никто не трогает),
хлопки — только при ``--no-catalyst`` **и** ``--legality`` (иначе отказ), прирост
мощности нигде не обещается.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import ignition_caps  # noqa: E402
from tools import refusals  # noqa: E402
from tools import variant1  # noqa: E402

#: Размеры образов, по которым идёт определение ЭБУ.
MS45_CAL_SIZE = 0x1D000
MS45_FULL_SIZE = 0x100000
MS45_MPC_SIZE = 0x70000
MS43_FULL_SIZE = 0x80000
MS42_PARTIAL_SIZE = 0x10000
MS42_CAL_SIZE = 0x8000
MS41_CAL_SIZE = 0x6000
MS41_FULL_SIZE = 0x40000

#: Плагинный реестр ЭБУ.  Новая версия регистрирует только свой builder;
#: диспетчер не импортирует все семейства на старте и не смешивает их policy.
ECU_PLUGINS: dict[str, Any] = {}


def register_ecu(platform: str, builder) -> None:
    """Register a platform builder with the common CLI dispatcher."""
    key = str(platform).upper().strip()
    if not key or not callable(builder):
        raise ValueError("platform and callable builder are required")
    ECU_PLUGINS[key] = builder


#: Готовые составы вариантов (те же примеры заявок, что проверены тестами).
VARIANT_TEMPLATES: dict[str, dict[str, Path]] = {
    "MS45": {
        "v1": ROOT / "MS45" / "examples" / "stage1_variant_v1.example.json",
        "v2": ROOT / "MS45" / "examples" / "stage1_variant_v2.example.json",
        "v3": ROOT / "MS45" / "examples" / "stage1_variant_v3.example.json",
    },
    "MS43": {
        "v1": ROOT / "MS43" / "examples" / "stage1_variant_v1.example.json",
        "v2": ROOT / "MS43" / "examples" / "stage1_variant_v2.example.json",
        "v3": ROOT / "MS43" / "examples" / "stage1_variant_v3.example.json",
    },
    "MS42": {"v1": ROOT / "MS42" / "examples" / "stage1_request_variant1.example.json"},
    "MS41": {
        "v1": ROOT / "MS41" / "examples" / "stage1_request_variant1.example.json"
    },
}

#: Что обещает каждый вариант; «чего не получишь» — отдельной строкой.
#: Потолки зажигания подставляются из единой спецификации
#: (``tools/ignition_caps.py``), текст не может разойтись с воротами.
_V1_IGN_RU = (
    "зажигание до +{cap:g}° на карте RON98 (профиль RON98; RON95 — до "
    "{cap95:g}°), раскладка по запасу до MBT, лимитеры и разрешённые функции "
    "отдельными запросами"
).format(cap=ignition_caps.cap_no_log_deg("ron98"), cap95=ignition_caps.cap_no_log_deg("ron95"))
_V1_IGN_EN = (
    "ignition up to +{cap:g}deg on the RON98 map (RON98 profile; RON95 up to "
    "{cap95:g}deg), laid out by the margin to MBT, limiters and approved "
    "functions as separate requests"
).format(cap=ignition_caps.cap_no_log_deg("ron98"), cap95=ignition_caps.cap_no_log_deg("ron95"))
_V2_IGN_RU = (
    "до +{cap:g}° с логом детонации"
).format(cap=ignition_caps.cap_with_log_deg("ron98"))
_V2_IGN_EN = (
    "up to +{cap:g}deg with a knock log"
).format(cap=ignition_caps.cap_with_log_deg("ron98"))

VARIANT_NOTES: dict[str, dict[str, dict[str, str]]] = {
    "v1": {
        "ru": {
            "title": "Вариант 1 — базовый (самый осторожный, без логов)",
            "yes": _V1_IGN_RU,
            "no": "топлива по логу нет, ВАНоса нет, хлопков нет (они будут в "
                  "варианте 3 и только без катализатора)",
        },
        "en": {
            "title": "Variant 1 — basic (the most conservative, no logs)",
            "yes": _V1_IGN_EN,
            "no": "no log-based fuel, no VANOS, no pops (variant 3 only, and only "
                  "without a catalytic converter)",
        },
    },
    "v2": {
        "ru": {
            "title": "Вариант 2 — расширенный (нужен лог Test0)",
            "yes": "вариант 1 + зажигание " + _V2_IGN_RU + ", топливо/зажигание "
                   "по логу, ВАНос по логу фаз, обороты с подтверждениями",
            "no": "хлопков и диагностического слоя нет — они в варианте 3",
        },
        "en": {
            "title": "Variant 2 — extended (a Test0 log is required)",
            "yes": "variant 1 + ignition " + _V2_IGN_EN + ", fuel from the log, "
                   "VANOS from the cam log, rev limiters with acknowledgements",
            "no": "no pops and no diagnostic layer — those are variant 3",
        },
    },
    "v3": {
        "ru": {
            "title": "Вариант 3 — машина без катализатора (нужны лог и подтверждения)",
            "yes": "вариант 2 + хлопки и слой диагностики по существующим воротам",
            "no": "без подтверждения «катализатора нет» и правовой справки не "
                  "выпускается; защита по детонации не отключается никогда",
        },
        "en": {
            "title": "Variant 3 — car without a catalytic converter (log and "
                     "acknowledgements required)",
            "yes": "variant 2 + pops and the diagnostic layer under the existing gates",
            "no": "never released without no_catalyst and the legal notice; knock "
                  "protection is never disabled",
        },
    },
}


class TorquexError(ValueError):
    """Ошибка единой точки входа с кодом для человекочитаемого текста."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class Detection:
    platform: str
    version: str
    supported: bool
    image_type: str
    detail: str
    software: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "version": self.version,
            "supported": self.supported,
            "image_type": self.image_type,
            "detail": self.detail,
            "software": list(self.software),
            "facts": self.facts,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Определение ЭБУ
# ---------------------------------------------------------------------------


def detect(path: Path) -> Detection:
    """Определить ЭБУ и версию по самому файлу (без имени файла)."""
    if not path.exists():
        raise TorquexError("FILE_NOT_FOUND", f"нет файла {path}")
    data = path.read_bytes()
    size = len(data)
    if size in (MS45_CAL_SIZE, MS45_FULL_SIZE, MS45_MPC_SIZE):
        return _detect_ms45(data)
    if size == MS43_FULL_SIZE:
        found = _detect_ms43(data)
        if found is not None:
            return found
        found = _detect_ms42(path, data)
        if found is not None:
            return found
    elif size in (MS42_PARTIAL_SIZE, MS42_CAL_SIZE):
        found = _detect_ms42(path, data)
        if found is not None:
            return found
    elif size in (MS41_CAL_SIZE, MS41_FULL_SIZE):
        found = _detect_ms41(path, data)
        if found is not None:
            return found
    if size in (262144, 524288):
        found = _detect_gs8(data)
        if found is not None:
            return found
    raise TorquexError(
        "UNSUPPORTED_IMAGE",
        f"размер {size} байт не похож ни на одно чтение MS41/42/43/45 или GS8.60",
    )


def _detect_gs8(data: bytes) -> Detection | None:
    """Блок АКПП Bosch GS8.60.x (ZF 5HP): только определение, сборки нет."""
    from tools import gs8_state

    result = gs8_state.identify(data)
    if result["system"] is None:
        return None
    return Detection(
        platform="GS8",
        version=str(result["system"]),
        supported=True,
        image_type=f"полное чтение {len(data) // 1024} КиБ",
        detail=(f"программа {result['program']}; калибровка: {result['state']}"
                + (f"; {result['vehicle']}" if result.get("vehicle") else "")),
        software=(str(result["program"]),),
        facts={"gs8": result, "read_only": True},
    )


def _detect_ms45(data: bytes) -> Detection:
    from MS45.tools import checksums as ck
    from MS45.tools import policy as pl

    kind = ck.image_kind(data)
    family = ck.read_family(data)
    supported = bool(family) and family in pl.SUPPORTED_FAMILIES
    return Detection(
        platform="MS45",
        version=family or "не прочитано",
        supported=supported,
        image_type="калибровка 116 КиБ" if kind == "cal" else "полное чтение 1 МиБ",
        detail=(
            f"семейство {family}: {'поддержано' if supported else 'не поддерживается'}; "
            f"известны {', '.join(pl.SUPPORTED_FAMILIES)}"
        ),
        software=(family,) if family else (),
        facts={"family": family, "image_kind": kind},
    )


def _detect_ms43(data: bytes) -> Detection | None:
    from MS43.tools import calibration as cal
    from MS43.tools import policy as ms43_policy

    policy_versions = ms43_policy.covered_software()
    try:
        version = cal.read_cal_version(data)
    except Exception:  # pragma: no cover - защита от чужих форматов
        return None
    if not version or version not in policy_versions:
        # Не MS43 — пусть попробует MS42 (та же длина 512 КиБ).
        return None
    return Detection(
        platform="MS43",
        version=version,
        supported=True,
        image_type="полное чтение 512 КиБ",
        detail=f"версия калибровки {version}; поддерживаются {', '.join(policy_versions)}",
        software=tuple(policy_versions),
        facts={"cal_version": version},
    )


def _detect_ms42(path: Path, data: bytes) -> Detection | None:
    try:
        from MS42.tools import analyze_firmware as af
    except Exception:  # pragma: no cover
        return None
    try:
        analysis = af.analyze(path)
    except Exception:  # pragma: no cover - чужие файлы
        return None
    if not analysis.supported:
        return None
    return Detection(
        platform="MS42",
        version=str(analysis.osid or "не прочитано"),
        supported=True,
        image_type=str(analysis.image_type or ""),
        detail=f"OSID {analysis.osid}; тип чтения {analysis.image_type}",
        facts={
            "osid": analysis.osid,
            "dataset_id": getattr(analysis, "dataset_id", None),
            "image_type": analysis.image_type,
        },
    )


def _detect_ms41(path: Path, data: bytes) -> Detection | None:
    try:
        from MS41.tools import analyze_firmware as af
    except Exception:  # pragma: no cover
        return None
    try:
        analysis = af.analyze(path)
    except Exception:  # pragma: no cover
        return None
    if not analysis.supported:
        return None
    return Detection(
        platform="MS41",
        version=str(analysis.calibration_variant or analysis.software_family or "не прочитано"),
        supported=True,
        image_type=str(analysis.image_type or ""),
        detail=(
            f"семейство {analysis.software_family}; вариант калибровки "
            f"{analysis.calibration_variant}; тип чтения {analysis.image_type}"
        ),
        facts={
            "software_family": analysis.software_family,
            "calibration_variant": analysis.calibration_variant,
            "image_type": analysis.image_type,
            "crc_blocks": analysis.crc_blocks,
        },
    )


# ---------------------------------------------------------------------------
# Печать отказов
# ---------------------------------------------------------------------------


def print_finding(code: str, severity: str, message: str, lang: str = "ru") -> None:
    """Код сохраняется, текст — на выбранном языке и вторым языком для reject."""
    primary = refusals.render(code, lang, message)
    print(f"  [{severity}] {primary}")
    if severity in ("reject", "warning"):
        other = "en" if lang == "ru" else "ru"
        second = refusals.render(code, other)
        if second != primary:
            print(f"           {second}")


def _print_result(status: str, findings: list[dict[str, Any]], lang: str) -> None:
    for finding in findings:
        print_finding(
            str(finding.get("code", "?")),
            str(finding.get("severity", "info")),
            str(finding.get("message", "")),
            lang,
        )


# ---------------------------------------------------------------------------
# Сборка
# ---------------------------------------------------------------------------


def _load_request(args: argparse.Namespace, detection: Detection) -> dict[str, Any]:
    """Собрать заявку: своя (``--request``) или готовый состав варианта."""
    request: dict[str, Any] = {}
    if args.request:
        request = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise TorquexError("REQUEST_INVALID", "заявка должна быть JSON-объектом")
    elif args.variant:
        template = VARIANT_TEMPLATES.get(detection.platform, {}).get(args.variant)
        if template is None:
            code = ("VARIANT_REQUIRES_LOGS" if args.variant in ("v2", "v3")
                    else "VARIANT_TEMPLATE_MISSING")
            raise TorquexError(code, f"для {detection.platform} нет состава {args.variant}")
        request = json.loads(template.read_text(encoding="utf-8"))
    if args.variant:
        request["variant"] = args.variant
    if args.no_catalyst:
        request["no_catalyst"] = True
    if args.legality:
        request["acknowledge_legality"] = True
    if args.customer:
        request["customer"] = args.customer
    if args.notes:
        request["notes"] = args.notes
    if not request.get("intents"):
        raise TorquexError("NOTHING_REQUESTED", "в заявке нет ни одного намерения")
    return request


def _check_hard_defaults(request: dict[str, Any]) -> None:
    """Жёсткие значения по умолчанию (§4.5): хлопки и DTC — только «без катализатора»."""
    kinds = {str(item.get("kind")) for item in request.get("intents") or [] if item}
    legal_kinds = set(variant1.LEGAL_GATE_KINDS) & kinds
    if not legal_kinds:
        return
    if not request.get("no_catalyst"):
        raise TorquexError(
            "NO_CATALYST_REQUIRED",
            "запросы " + ", ".join(sorted(legal_kinds)) + " выпускаются только с --no-catalyst",
        )
    if not request.get("acknowledge_legality"):
        raise TorquexError(
            "ACKNOWLEDGE_LEGALITY_REQUIRED",
            "нужно прочитать правовую справку и передать --legality",
        )


def build(args: argparse.Namespace) -> int:
    path = Path(args.file)
    detection = detect(path)
    print(f"ЭБУ: {detection.platform}, версия {detection.version} ({detection.image_type})")
    if not detection.supported:
        print_finding("CAL_VERSION_UNSUPPORTED", "reject", detection.detail, args.lang)
        return 2
    try:
        request = _load_request(args, detection)
        _check_hard_defaults(request)
    except TorquexError as error:
        print_finding(error.code, "reject", str(error), args.lang)
        return 2
    return _build_platform(args, detection, request, path)


def _write_note(args: argparse.Namespace, text: str) -> None:
    if args.note:
        Path(args.note).write_text(text, encoding="utf-8", newline="\n")
        print(f"наряд: {args.note}")


def _write_manifest(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.manifest:
        Path(args.manifest).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"манифест: {args.manifest}")


def _build_platform(args: argparse.Namespace, detection: Detection,
                    request: dict[str, Any], path: Path) -> int:
    builder = ECU_PLUGINS.get(str(detection.platform).upper())
    if builder is None:
        raise TorquexError(
            "ECU_PLUGIN_MISSING",
            f"для ЭБУ {detection.platform} не зарегистрирован плагин сборки",
        )
    return int(builder(args, request, path))


def _build_ms45(args: argparse.Namespace, request: dict[str, Any], path: Path) -> int:
    from MS45.tools import stages

    image = path.read_bytes()
    result = stages.build("stage1", image, request, source_name=path.name)
    print(f"стадия {result.stage}: {result.status}, семейство {result.family}")
    for item in result.packages:
        if item.status not in ("not_requested",):
            print(f"  пакет {item.id:14s} {item.status:14s} {item.detail}")
    _print_result(result.status, result.findings, args.lang)
    if args.note:
        _write_note(args, stages.build_note(result, path.name))
    if args.manifest:
        _write_manifest(args, result.manifest)
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 2 if result.status == "REJECTED" else 0
    if result.status != "BUILT" or result.result is None:
        print_finding("NO_OUTPUT", "reject", f"статус {result.status}", args.lang)
        return 2
    out = Path(args.out) if args.out else path.with_name(path.stem + "_torquex.bin")
    out.write_bytes(result.result.image)
    print(f"готовый файл: {out} (sha256 {result.result.sha256[:16]}…)")
    print("памятка: перед записью сохрани заводской сток, после записи сверь файл "
          "по манифесту и сними лог Test0")
    return 0


def _build_ms43(args: argparse.Namespace, request: dict[str, Any], path: Path) -> int:
    from MS43.tools import stages

    image = path.read_bytes()
    result = stages.build("stage1", image, request, source_name=path.name)
    print(f"стадия {result.stage}: {result.status}, версия {result.cal_version}")
    for item in result.packages:
        print(f"  пакет {item.id:14s} {item.status:14s} {item.detail}")
    _print_result(result.status, result.findings, args.lang)
    if args.note:
        _write_note(args, stages.build_note(result, path.name))
    if args.manifest:
        _write_manifest(args, result.manifest)
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 2 if result.status == "REJECTED" else 0
    if result.status != "BUILT" or result.result is None:
        print_finding("NO_OUTPUT", "reject", f"статус {result.status}", args.lang)
        return 2
    out = Path(args.out) if args.out else path.with_name(path.stem + "_torquex.bin")
    out.write_bytes(result.result.image)
    print(f"готовый файл: {out} (sha256 {result.result.sha256[:16]}…)")
    print("памятка: перед записью сохрани заводской сток, после записи сверь файл "
          "по манифесту и сними лог Test0")
    return 0


def _build_ms42(args: argparse.Namespace, request: dict[str, Any], path: Path) -> int:
    from MS42.tools import stages
    from MS42.tools.calibration import Policy
    from MS42.tools.delivery_note import build_note

    policy = Policy.load(stages.DEFAULT_POLICY)
    out = Path(args.out) if args.out else path.with_name(path.stem + "_torquex.bin")
    result = stages.build("stage1", path, request, policy,
                          out=None if args.dry_run else out, customer=args.customer)
    print(f"стадия {result.stage}: {result.status}, OSID {result.osid}, "
          f"dataset {result.dataset_id}")
    for package in result.packages:
        print(f"  пакет {package.id:14s} {package.status:14s} {package.summary}")
    _print_result(result.status, result.findings, args.lang)
    payload = stages.build_payload(result)
    if args.note:
        note = build_note(stages.generation_payload(result),
                          stages.patch_payload(result), args.customer)
        _write_note(args, note)
    if args.manifest:
        _write_manifest(args, payload)
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 2 if result.status in ("BLOCKED", "REJECTED") else 0
    # MS42 пишет файл сам и помечает стадию READY/WRITTEN; пустой план — не отказ.
    if not result.output or not Path(result.output).exists():
        print_finding("NO_OUTPUT", "reject", f"статус {result.status}", args.lang)
        return 2
    if result.status in ("BLOCKED", "REJECTED"):
        print_finding("NO_OUTPUT", "reject", f"статус {result.status}", args.lang)
        return 2
    print(f"готовый файл: {result.output} (sha256 {str(result.output_sha256)[:16]}…)")
    print("памятка: перед записью сохрани заводской сток, после записи сверь файл "
          "по манифесту и сними лог Test0")
    return 0


def _build_ms41(args: argparse.Namespace, request: dict[str, Any], path: Path) -> int:
    from MS41.tools import calibration as ms41_cal
    from MS41.tools import generate_patches as generator

    result = generator.generate(path, request)
    print(f"MS41: статус {result.status}")
    for outcome in result.outcomes:
        print(f"  запрос {outcome.kind:12s} {outcome.status:14s} {outcome.detail}")
    for reason in result.blocked_reasons:
        code = str(reason).split(":", 1)[0]
        print_finding(code, "reject", str(reason), args.lang)
    if result.notes:
        for note in result.notes:
            print(f"  примечание: {note}")
    if args.manifest:
        _write_manifest(args, result.to_json())
    if args.dry_run:
        print("(dry-run: файл не записан)")
        return 2 if result.status == generator.STATUS_BLOCKED else 0
    if not result.patches:
        print_finding(
            "NO_OUTPUT", "reject",
            "готовых правок нет: MS41 выпускает только то, что прошло allowlist "
            "(дорожный лимитер, отсечка с подтверждением риска, МАФ/форсунки по "
            "профилю железа)", args.lang,
        )
        return 2
    out = Path(args.out) if args.out else path.with_name(path.stem + "_torquex.bin")
    patches_file = out.with_name(out.name + ".patches.json")
    patches_file.write_text(
        json.dumps(generator.patches_payload(result, request, None),
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    namespace = argparse.Namespace(
        bin=path, patches=patches_file, out=out, report=None,
        policy=ms41_cal.DEFAULT_POLICY, xdf=ms41_cal.DEFAULT_XDF, json=False,
    )
    code = ms41_cal.run(namespace)
    if code != 0 or not out.exists():
        print_finding("NO_OUTPUT", "reject", "ядро MS41 не выпустило файл", args.lang)
        return 2
    print(f"готовый файл: {out} (sha256 {_sha256(out.read_bytes())[:16]}…)")
    print(f"разбор правок: {patches_file}")
    print("памятка: перед записью сохрани заводской сток, после записи сверь файл "
          "по манифесту и сними лог Test0")
    return 0


# Built-in ECU plugins.  They are registered after their lazy builders are
# defined; external integrations may register another family before calling
# ``build`` without changing this dispatcher.
register_ecu("MS45", _build_ms45)
register_ecu("MS43", _build_ms43)
register_ecu("MS42", _build_ms42)
register_ecu("MS41", _build_ms41)


# ---------------------------------------------------------------------------
# Откат на сток
# ---------------------------------------------------------------------------


def _manifest_result_sha(manifest: dict[str, Any]) -> str:
    """sha256 выпущенного файла из манифеста: у каждого ЭБУ своё поле."""
    result = manifest.get("result")
    if isinstance(result, dict) and result.get("sha256"):
        return str(result["sha256"])
    return str(manifest.get("result_sha256") or manifest.get("output_sha256") or "")


def rollback(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TorquexError("MANIFEST_MISMATCH", "манифест должен быть JSON-объектом")
    stock_sha = str(manifest.get("source_sha256") or "")
    result_sha = _manifest_result_sha(manifest)
    if not stock_sha:
        raise TorquexError("STOCK_UNVERIFIED", "в манифесте нет source_sha256")
    stock_path = Path(args.stock)
    if not stock_path.exists():
        raise TorquexError("STOCK_UNVERIFIED", f"нет файла стока {stock_path}")
    stock = stock_path.read_bytes()
    if _sha256(stock) != stock_sha:
        raise TorquexError(
            "STOCK_MISMATCH",
            f"sha256 стока {_sha256(stock)[:16]}… не равен source_sha256 {stock_sha[:16]}…",
        )
    print(f"сток подтверждён: sha256 {stock_sha[:16]}… "
          f"({len(stock)} байт, {stock_path.name})")
    if args.written:
        written_path = Path(args.written)
        if not written_path.exists():
            raise TorquexError("MANIFEST_MISMATCH", f"нет файла {written_path}")
        if not result_sha:
            raise TorquexError(
                "MANIFEST_MISMATCH",
                "в манифесте нет sha256 выпущенного файла: проверить нечем",
            )
        written_sha = _sha256(written_path.read_bytes())
        if written_sha != result_sha:
            raise TorquexError(
                "MANIFEST_MISMATCH",
                f"выпущенный файл {written_sha[:16]}… не равен {result_sha[:16]}… из манифеста",
            )
        print(f"выпущенный файл соответствует манифесту: sha256 {written_sha[:16]}…")
    out = Path(args.out) if args.out else Path("stock_restored.bin")
    out.write_bytes(stock)
    print(f"восстановленный сток: {out}")
    _report_rollback_crc(out)
    print("порядок возврата: записать этот файл тем же способом, что и тюн "
          "(полная запись), затем сверить sha256 и снять лог Test0; "
          "адаптации после отката сбросить штатной диагностикой")
    return 0


def _report_rollback_crc(path: Path) -> None:
    """Проверить суммы восстановленного стока тем же детектором, что и сборка."""
    data = path.read_bytes()
    try:
        detection = detect(path)
    except TorquexError as error:
        print(f"проверку сумм выполнить не удалось: {error}")
        return
    if detection.platform == "MS45":
        from MS45.tools import checksums as ck

        report = ck.verify_bytes(data, str(path))
        print(f"контрольные суммы MS45: {'ok' if report.ok else 'ОШИБКА'}")
        return
    if detection.platform == "MS43":
        from MS43.tools import calibration as cal

        ok, problems = cal.verify_image_bytes(data)
        print(f"контрольные суммы MS43: {'ok' if ok else 'ОШИБКА: ' + '; '.join(problems)}")
        return
    if detection.platform == "MS42":
        from MS42.tools import analyze_firmware as af42

        analysis = af42.analyze(path)
        print(f"контрольные суммы MS42: "
              f"{'ok' if analysis.supported else 'ОШИБКА: ' + '; '.join(analysis.reasons)}")
        return
    from MS41.tools import analyze_firmware as af41

    analysis = af41.analyze(path)
    print(f"контрольные суммы MS41: "
          f"{'ok' if analysis.supported else 'ОШИБКА: ' + '; '.join(analysis.reasons)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def variants(args: argparse.Namespace) -> int:
    lang = args.lang
    for key in ("v1", "v2", "v3"):
        note = VARIANT_NOTES[key][lang if lang in ("ru", "en") else "ru"]
        print(f"{key}: {note['title']}")
        print(f"  получишь: {note['yes']}")
        print(f"  не получишь: {note['no']}")
    print("\nСборка: torquex build --file <сток.bin> --variant v1 --out <файл.bin> "
          "[--note <наряд.md>] [--manifest <манифест.json>]")
    return 0


def explain(args: argparse.Namespace) -> int:
    text = refusals.render(args.code, args.lang)
    print(text)
    return 0 if refusals.explain(args.code, args.lang) else 1


def _prepare_console() -> None:
    """Keep ``torquex --help`` usable on a cp1251 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _prepare_console()
    parser = argparse.ArgumentParser(
        prog="torquex",
        description="TorqueX: сток → определение ЭБУ → вариант → готовый файл и наряд",
    )
    parser.add_argument("--lang", choices=("ru", "en"), default="ru",
                        help="язык текстов и отказов (по умолчанию ru)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="определить ЭБУ и версию по файлу")
    p_detect.add_argument("file")
    p_detect.add_argument("--json", action="store_true", help="вывести результат как JSON")
    p_detect.set_defaults(func=_cmd_detect)

    p_variants = sub.add_parser("variants", help="что даёт каждый вариант")
    p_variants.set_defaults(func=variants)

    p_build = sub.add_parser("build", help="собрать файл по варианту или заявке")
    p_build.add_argument("--file", required=True, help="стоковое чтение ЭБУ")
    p_build.add_argument("--variant", choices=("v1", "v2", "v3"))
    p_build.add_argument("--request", help="своя заявка JSON (вместо состава варианта)")
    p_build.add_argument("--out", help="куда записать готовый файл")
    p_build.add_argument("--note", help="куда записать наряд (Markdown)")
    p_build.add_argument("--manifest", help="куда записать манифест (JSON)")
    p_build.add_argument("--no-catalyst", action="store_true",
                         help="подтвердить, что катализатор физически снят")
    p_build.add_argument("--legality", action="store_true",
                         help="подтвердить, что правовая справка прочитана")
    p_build.add_argument("--customer", help="подпись заявки (имя/ник)")
    p_build.add_argument("--notes", help="примечание к заявке")
    p_build.add_argument("--dry-run", action="store_true",
                         help="только разбор, без записи файла")
    p_build.set_defaults(func=build)

    p_rollback = sub.add_parser("rollback", help="вернуть сток и проверить его")
    p_rollback.add_argument("--manifest", required=True, help="манифест выпущенного файла")
    p_rollback.add_argument("--stock", required=True, help="заводской сток, снятый до записи")
    p_rollback.add_argument("--written", help="выпущенный файл (проверить по манифесту)")
    p_rollback.add_argument("--out", help="куда записать восстановленный сток")
    p_rollback.set_defaults(func=rollback)

    p_explain = sub.add_parser("explain", help="объяснить код отказа")
    p_explain.add_argument("code")
    p_explain.set_defaults(func=explain)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TorquexError as error:
        print_finding(error.code, "reject", str(error), args.lang)
        return 2
    except FileNotFoundError as error:
        print_finding("FILE_NOT_FOUND", "reject", str(error), args.lang)
        return 2


def _cmd_detect(args: argparse.Namespace) -> int:
    detection = detect(Path(args.file))
    if getattr(args, "json", False):
        print(json.dumps(detection.as_dict(), ensure_ascii=False, indent=2))
        return 0 if detection.supported else 2
    print(f"ЭБУ: {detection.platform}")
    print(f"версия: {detection.version}")
    print(f"чтение: {detection.image_type}")
    print(f"поддержано: {'да' if detection.supported else 'нет'}")
    print(f"подробности: {detection.detail}")
    if not detection.supported:
        print_finding("CAL_VERSION_UNSUPPORTED", "reject", detection.detail, args.lang)
        return 2
    print("варианты: v1 (базовый), v2 (расширенный, нужен лог), v3 (без катализатора)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
