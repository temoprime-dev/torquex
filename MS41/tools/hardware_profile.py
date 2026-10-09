#!/usr/bin/env python3
"""Validate an MS41 Stage 2 hardware profile and produce a calibration work order.

Stage 2 is not one calibration: it is a preset for an exact combination of
engine, manifold, cams, exhaust, MAF, injectors, fuel and transmission. This tool
refuses to guess. Unknown hardware never becomes a default; every referenced
calibration object is resolved against the real XDF before it is listed.

Measurements are optional. They are only used when supplied, so a profile can be
checked before any logging exists, but headroom claims require real values.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"

ENGINES = {
    "m52b20": "M52B20",
    "m52b25": "M52B25",
    "m52b28": "M52B28",
    "s52b32": "S52B32 (US E36 M3)",
}
# M52TU / M54 belong to later ECUs and must not be offered for MS41.
FOREIGN_ENGINES = {
    "m52tub20": "MS42", "m52tub25": "MS42", "m52tub28": "MS42",
    "m54b22": "MS43", "m54b25": "MS43", "m54b30": "MS43",
}
DME_VARIANTS = {"ms410": "MS41.0", "ms411": "MS41.1"}
TRANSMISSIONS = {"manual": "manual", "automatic": "automatic"}
MANIFOLDS = {"stock_m52": "stock M52", "m50": "M50", "known_other": "known other"}
CAMS = {"stock": "stock", "known_aftermarket": "known aftermarket"}
EXHAUSTS = {"stock": "stock", "cat_back": "high-flow cat-back",
            "headers_and_cat_back": "headers + cat-back", "decat": "no catalyst"}
INTAKES = {"stock_airbox": "stock airbox",
           "performance_stock_housing": "performance intake, stock MAF housing"}
MAFS = {"stock_hfm": "stock Siemens HFM", "known_aftermarket": "known replacement"}
INJECTORS = {"stock": "stock", "known_replacement": "known replacement"}
CATALYSTS = {"present": "present", "removed": "removed"}
RON = (91, 95, 98)

# Change key -> calibration objects in the 1429861 XDF. Symbol titles are
# resolved against the definition file, never trusted blindly.
CALIBRATION_WORK: dict[str, dict[str, Any]] = {
    "vanos_switch_manual": {
        "tool": "MS41/tools/vanos_switch.py",
        "title": "VANOS switching threshold, manual transmission",
        "reason": "Manifold or cams change the load curve where VANOS must switch.",
        "symbols": [
            "ip_maf_min_ivvt__n__tco",
            "ldp_n__maf_min_ivvt",
            "ldp_tco__maf_min_ivvt",
            "id_maf_hys_ivvt__n",
            "id_pvs_ivvt__n",
            "id_ivvt_n_fl__n",
        ],
        "logging": ["wot", "stage0"],
        "validation": [
            "Log air mass at the switch point with VANOS enabled and disabled.",
            "Set the new threshold at the load-curve intersection, not at a copied RPM.",
            "Re-check idle quality and part-throttle response after the change.",
        ],
    },
    "vanos_switch_automatic": {
        "tool": "MS41/tools/vanos_switch.py",
        "title": "VANOS switching threshold, automatic transmission",
        "reason": "Automatic cars use a separate VANOS threshold table in this XDF.",
        "symbols": [
            "ip_dri_maf_min_ivvt__n__tco",
            "ldp_n__dri_maf_min_ivvt",
            "ldp_tco__dri_maf_min_ivvt",
            "id_maf_hys_ivvt__n",
            "id_pvs_ivvt__n",
        ],
        "logging": ["wot", "stage0"],
        "validation": [
            "Confirm the threshold against logged air mass on the automatic variant.",
            "Verify shift quality and torque-converter behaviour under part load.",
        ],
    },
    "vanos_safety_limits": {
        "title": "VANOS activation safety limits",
        "reason": "Temperature, speed and dead-time limits bound when VANOS may move.",
        "symbols": [
            "c_vs_min_ivvt",
            "c_t_max_ivvt",
            "c_dly_ivvt",
            "c_n_min_dly_ivvt",
            "c_ivvt_ang",
            "c_ivvt_ang_hys",
            "c_n_hys_ivvt",
            "c_t_ast_ivvt",
        ],
        "logging": ["stage0"],
        "validation": [
            "Keep the stock limits unless a logged fault proves they block a valid state.",
            "VANOS angle and hysteresis define the diagnostic window; changing them hides faults.",
        ],
    },
    "maf_transfer": {
        "tool": "MS41/tools/maf_curve.py",
        "title": "MAF transfer function",
        "reason": "A replacement MAF or housing needs a monotonic voltage-to-airflow curve.",
        "symbols": [
            "id_maf_tab__v_maf_1__v_maf_2 (1x256)",
            "id_maf_tab__v_maf_1__v_maf_2 (16x16)",
        ],
        "logging": ["fueling", "wot"],
        "validation": [
            "Verify the transfer curve is smooth and strictly monotonic before flashing.",
            "Validate against fuel trims and wideband lambda, not against the old curve.",
            "Do not rescale load axes unless the logged operating range proves it necessary.",
        ],
    },
    "maf_limits": {
        "tool": "MS41/tools/maf_limits.py",
        "title": "Air mass limits and diagnostics",
        "reason": "Diagnostic and plausibility limits must follow a new MAF range.",
        "symbols": [
            "c_maf_max",
            "c_maf_max_diag",
            "c_maf_min_diag",
            "c_maf_vs_min_diag",
            "c_maf_max_ch_3",
        ],
        "logging": ["wot", "stage0"],
        "validation": [
            "Raise a limit only if a log shows the engine legitimately exceeding it.",
            "Never raise c_maf_max_diag just to silence a real MAF fault.",
            "Catalyst-heating air mass stays within the thermal limits of the exhaust.",
        ],
    },
    "injector_base": {
        "tool": "MS41/tools/injector_scaling.py",
        "title": "Injector base fuel map",
        "reason": "Replacement injectors change fuel mass per millisecond.",
        "symbols": [
            "ip_tib__n__maf",
            "ip_ti_ivvt__n__maf",
            "c_ti_ad_add_max",
            "c_ti_ad_fac_max",
        ],
        "logging": ["fueling", "wot"],
        "validation": [
            "Scale from the documented flow ratio, then confirm with fuel trims and wideband.",
            "Keep lambda adaptation limits stock until the base map is validated.",
            "Never combine an unknown injector with an unknown MAF in one calibration.",
        ],
    },
    "injector_cranking": {
        "tool": "MS41/tools/injector_scaling.py",
        "title": "Cranking and cold-start injection",
        "reason": "Cranking fuel must be rescaled together with the base map.",
        "symbols": ["ip_ti_cst__n__tco", "ip_tipr_cst__tco"],
        "logging": ["stage0"],
        "validation": [
            "Check cold start and hot restart at both temperature extremes.",
            "Cranking time should stay close to stock; long cranking means the map is wrong.",
        ],
    },
    "ignition_knock_review": {
        "title": "Ignition and knock protection review",
        "reason": "Different airflow, cams and exhaust acoustics invalidate a copied ignition map.",
        "symbols": [
            "ip_iga_ivvt__n__maf",
            "ip_igab__n__maf",
            "ip_iga_max_knk__n__maf",
        ],
        "logging": ["ignition", "wot"],
        "validation": [
            "Re-run the ignition sweep; no universal advance value exists.",
            "Keep the knock limiter and its retard authority active.",
            "Investigate false knock from changed exhaust acoustics instead of masking it.",
        ],
    },
    "hardcut_stock": {
        "tool": "MS41/tools/generate_patches.py",
        "title": "Stock rev limiter (fuel cut)",
        "reason": "Optional hardcut works inside the stock fuel-cut limiter.",
        "symbols": ["c_n_max", "c_n_max_hys", "c_n_max_hys_max", "c_n_max_max"],
        "logging": ["wot"],
        "validation": [
            "Stock MS41 cuts fuel; it is not an ignition-retard limiter.",
            "Verify with STATUS_SCHUB_AB_EIN that cut and restore still behave correctly.",
            "A smaller hysteresis gives a harder limiter but must not create a fuel-cut cycle.",
        ],
    },
    "popcorn_overrun": {
        "title": "Overrun and deceleration behaviour",
        "reason": "Popcorn is produced by overrun fuel-cut and retard behaviour.",
        "symbols": [
            "c_n_hys_max_puc", "c_n_hys_min_puc", "c_n_min_lgrd_pu",
            "c_tco_min_pu", "c_pvs_grd_pu",
            "ip_n_min_puc__tco", "ip_iga_puc__n", "ip_iga_lgrd_pu__n__maf_mmv",
        ],
        "logging": ["ignition", "wot"],
        "validation": [
            "Reject when a catalyst is fitted: unburnt fuel destroys it thermally.",
            "Validate every value on the individual engine and exhaust, on a closed course.",
            "Confirm overrun fuel cut still engages and recovers cleanly.",
        ],
    },
}

# Work items implied by each hardware choice.
TRIGGERS: dict[str, tuple[str, ...]] = {
    "manifold_non_stock": ("vanos_switch_manual", "vanos_switch_automatic",
                           "vanos_safety_limits", "ignition_knock_review"),
    "cams_non_stock": ("vanos_switch_manual", "vanos_switch_automatic",
                       "ignition_knock_review"),
    "exhaust_non_stock": ("ignition_knock_review",),
    "maf_non_stock": ("maf_transfer", "maf_limits", "injector_base"),
    "injectors_non_stock": ("injector_base", "injector_cranking"),
    "hardcut_requested": ("hardcut_stock",),
    "popcorn_requested": ("popcorn_overrun",),
}

BOOLEAN_OPTIONS = ("hardcut", "popcorn")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


@dataclass
class HardwareProfile:
    engine: str | None = None
    dme_variant: str | None = None
    transmission: str | None = None
    manifold: str | None = None
    cams: str | None = None
    exhaust: str | None = None
    intake: str | None = None
    maf: str | None = None
    maf_part_number: str | None = None
    maf_curve_documented: bool = False
    maf_ceiling_kgh: float | None = None
    injectors: str | None = None
    injector_part_number: str | None = None
    injector_flow_cc_min: float | None = None
    injector_stock_flow_cc_min: float | None = None
    injector_dead_time_documented: bool = False
    compression: str = "stock"
    displacement: str = "stock"
    catalyst: str | None = None
    fuel_ron: int | None = None
    hardcut: bool = False
    popcorn: bool = False


@dataclass
class ProfileReport:
    status: str
    stage: str
    findings: list[Finding]
    preset_key: str | None
    work_order: list[dict[str, Any]]
    required_logging: list[str]
    notes: list[str]


def load_xdf_symbols(path: Path = DEFAULT_XDF) -> dict[str, list[dict[str, Any]]]:
    """Return XDF object titles mapped to their definition and data addresses.

    For a constant the object address is the value itself. For a table the
    object address is the table header, while the values live in the z axis,
    which usually sits at a different address. Both are reported: a work order
    that names only the header would point at the axis block, not at the data.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r"<XDFCONSTANT\b.*?</XDFCONSTANT>|<XDFTABLE\b.*?</XDFTABLE>", re.S
    )
    symbols: dict[str, list[dict[str, Any]]] = {}
    for block in pattern.findall(text):
        title = re.search(r"<title>([^<]+)</title>", block)
        address = re.search(r'<EMBEDDEDDATA[^>]*mmedaddress="(0x[0-9A-Fa-f]+)"', block)
        bits = re.search(r'mmedelementsizebits="(\d+)"', block)
        if not title or not address:
            continue
        data = re.findall(
            r'<XDFAXIS id="z">\s*(?:<[^>]+>\s*)*?<EMBEDDEDDATA[^>]*mmedaddress="(0x[0-9A-Fa-f]+)"',
            block,
        )
        symbols.setdefault(title.group(1).strip(), []).append({
            "address": address.group(1),
            "element_size_bits": int(bits.group(1)) if bits else None,
            "data_addresses": data,
        })
    return symbols


def _finding(findings: list[Finding], code: str, severity: str, message: str) -> None:
    findings.append(Finding(code, severity, message))


def parse_profile(data: dict[str, Any]) -> HardwareProfile:
    profile = HardwareProfile()
    for key, value in (data or {}).items():
        if key in BOOLEAN_OPTIONS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            setattr(profile, key, value)
        elif hasattr(profile, key):
            setattr(profile, key, value)
        else:
            raise ValueError(f"unknown profile field: {key}")
    return profile


def _measurement(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def check_measurements(
    profile: HardwareProfile,
    measurements: dict[str, Any] | None,
    ipw_warning: float = 80.0,
    ipw_reject: float = 92.0,
) -> list[Finding]:
    """Headroom checks that require real logged values."""
    findings: list[Finding] = []
    if not measurements:
        _finding(
            findings, "MEASUREMENTS_MISSING", "warning",
            "No measurements supplied; MAF headroom and injector duty cannot be assessed.",
        )
        return findings

    rpm = _measurement(measurements.get("rpm_at_max_ipw"))
    ipw = _measurement(measurements.get("max_injector_pulse_width_ms"))
    if rpm and ipw:
        duty = ipw * rpm / 1200.0  # 4-stroke cycle = 2 revolutions = 120000/rpm ms
        if duty >= ipw_reject:
            _finding(
                findings, "INJECTOR_DUTY_REJECT", "reject",
                f"Injector duty is {duty:.1f}% at {rpm:.0f} rpm; injectors are out of "
                f"capacity (limit {ipw_reject:.0f}%).",
            )
        elif duty >= ipw_warning:
            _finding(
                findings, "INJECTOR_DUTY_WARNING", "warning",
                f"Injector duty is {duty:.1f}% at {rpm:.0f} rpm; headroom is nearly gone.",
            )
        else:
            _finding(
                findings, "INJECTOR_DUTY_OK", "info",
                f"Injector duty is {duty:.1f}% at {rpm:.0f} rpm.",
            )
    else:
        _finding(
            findings, "INJECTOR_DUTY_UNKNOWN", "warning",
            "Need max_injector_pulse_width_ms and rpm_at_max_ipw to compute duty cycle.",
        )

    voltage = _measurement(measurements.get("max_maf_voltage"))
    if voltage is None:
        _finding(
            findings, "MAF_VOLTAGE_MISSING", "warning",
            "max_maf_voltage was not supplied; MAF saturation cannot be assessed.",
        )
    else:
        ceiling = profile.maf_ceiling_kgh
        if voltage >= 4.7:
            _finding(
                findings, "MAF_VOLTAGE_NEAR_CEILING", "reject",
                f"MAF output reached {voltage:.2f} V. Typical HFM output approaches the "
                "ADC reference near 5 V; anything above ~4.7 V must be treated as "
                "saturated until the sensor datasheet proves otherwise.",
            )
        else:
            _finding(
                findings, "MAF_VOLTAGE_OK", "info",
                f"Maximum MAF voltage was {voltage:.2f} V.",
            )
        if ceiling:
            flow = _measurement(measurements.get("max_maf_airflow_kgh"))
            if flow is not None:
                headroom = (ceiling - flow) / ceiling * 100
                severity = "warning" if headroom < 10 else "info"
                _finding(
                    findings, "MAF_FLOW_HEADROOM", severity,
                    f"Peak airflow {flow:.1f} kg/h against a {ceiling:.1f} kg/h sensor "
                    f"ceiling leaves {headroom:.1f}% headroom.",
                )

    trims = measurements.get("fuel_trims_percent")
    if isinstance(trims, dict):
        for name, value in trims.items():
            number = _measurement(value)
            if number is None:
                continue
            if abs(number) > 15:
                _finding(
                    findings, f"TRIM_{name.upper()}_HIGH", "reject",
                    f"{name}={number:.1f}% exceeds ±15%: a mechanical or calibration "
                    "fault is present, not a tuning opportunity.",
                )
            elif abs(number) > 10:
                _finding(
                    findings, f"TRIM_{name.upper()}_WARNING", "warning",
                    f"{name}={number:.1f}% exceeds ±10%.",
                )
    else:
        _finding(findings, "TRIMS_MISSING", "warning",
                 "Fuel trims were not supplied; base fueling cannot be judged.")
    return findings


def evaluate(
    profile: HardwareProfile,
    measurements: dict[str, Any] | None = None,
    symbols: dict[str, list[dict[str, Any]]] | None = None,
    stage2_requested: bool = True,
) -> ProfileReport:
    symbols = symbols if symbols is not None else load_xdf_symbols()
    findings: list[Finding] = []
    notes: list[str] = []

    # --- identity and completeness -----------------------------------------
    if profile.engine in FOREIGN_ENGINES:
        _finding(
            findings, "ENGINE_NOT_MS41", "reject",
            f"{profile.engine.upper()} is controlled by {FOREIGN_ENGINES[profile.engine]}, "
            "not MS41. This profile cannot be generated here.",
        )
    elif profile.engine not in ENGINES:
        _finding(findings, "ENGINE_MISSING", "reject",
                 f"engine must be one of {sorted(ENGINES)}.")

    if profile.dme_variant not in DME_VARIANTS:
        _finding(findings, "DME_VARIANT_MISSING", "reject",
                 "dme_variant must be ms410 or ms411.")
    else:
        notes.append(
            "MS41.0 and MS41.1 cannot be told apart from a calibration binary alone; "
            "confirm the variant from the ECU label or an INPA identification before flashing."
        )

    if profile.transmission not in TRANSMISSIONS:
        _finding(findings, "TRANSMISSION_MISSING", "reject",
                 "transmission must be manual or automatic.")
    if profile.fuel_ron not in RON:
        _finding(findings, "FUEL_RON_MISSING", "reject",
                 "fuel_ron must be 91, 95 or 98.")
    for name, allowed in (("manifold", MANIFOLDS), ("cams", CAMS), ("exhaust", EXHAUSTS),
                          ("intake", INTAKES), ("maf", MAFS), ("injectors", INJECTORS),
                          ("catalyst", CATALYSTS)):
        if getattr(profile, name) not in allowed:
            _finding(findings, f"{name.upper()}_MISSING", "reject",
                     f"{name} must be one of {sorted(allowed)}.")

    # --- hardware risk rules -----------------------------------------------
    if profile.maf != "stock_hfm" and not profile.maf_curve_documented:
        _finding(
            findings, "MAF_CURVE_UNDOCUMENTED", "reject",
            "A replacement MAF without a documented, monotonic transfer curve cannot be "
            "calibrated. Provide the curve before generating anything.",
        )
    if profile.injectors != "stock":
        if not profile.injector_flow_cc_min or not profile.injector_stock_flow_cc_min:
            _finding(
                findings, "INJECTOR_FLOW_MISSING", "reject",
                "Replacement injectors need both injector_flow_cc_min and "
                "injector_stock_flow_cc_min to compute a starting scale.",
            )
        if not profile.injector_dead_time_documented:
            _finding(
                findings, "INJECTOR_DEAD_TIME_MISSING", "reject",
                "Replacement injectors need a documented dead-time/voltage correction.",
            )
        if profile.maf != "stock_hfm":
            _finding(
                findings, "MAF_AND_INJECTORS_UNKNOWN", "reject",
                "Unknown injectors and a non-stock MAF must never be calibrated in one "
                "step: a trim or lambda error cannot be attributed to either component.",
            )

    if profile.popcorn:
        if profile.catalyst == "present":
            _finding(
                findings, "POPCORN_WITH_CATALYST", "reject",
                "Popcorn is rejected when a catalyst is fitted: unburnt fuel ignites in "
                "the brick and destroys it thermally.",
            )
        if profile.exhaust == "stock":
            _finding(
                findings, "POPCORN_STOCK_EXHAUST", "warning",
                "A stock exhaust gives little control over afterfire and no acoustic result; "
                "expect no useful effect.",
            )
        _finding(
            findings, "POPCORN_VALIDATION_REQUIRED", "warning",
            "Popcorn values are always individual: every value needs road validation and "
            "no public aggressive preset will be produced.",
        )

    if profile.hardcut:
        notes.append(
            "Stock MS41 hardcut is a fuel cut. It is not an ignition-retard limiter; "
            "ignition-based limiting requires a separate MS41.3 product."
        )

    if profile.compression != "stock" and profile.fuel_ron == 91:
        _finding(
            findings, "RON91_HIGH_COMPRESSION", "reject",
            "An increased compression ratio is not compatible with a RON 91 calibration.",
        )
    if profile.catalyst == "removed":
        _finding(
            findings, "CATALYST_REMOVED", "warning",
            "Catalyst removal is a legal/roadworthiness decision outside this tool; the "
            "generator will not silently disable catalyst diagnostics.",
        )
    if profile.displacement != "stock":
        _finding(
            findings, "DISPLACEMENT_CHANGED", "warning",
            "A changed displacement invalidates load-based tables; expect a full "
            "re-calibration rather than a Stage 1 baseline.",
        )

    # --- work order ---------------------------------------------------------
    triggers: list[str] = []
    if profile.manifold not in (None, "stock_m52"):
        triggers.append("manifold_non_stock")
    if profile.cams not in (None, "stock"):
        triggers.append("cams_non_stock")
    if profile.exhaust not in (None, "stock"):
        triggers.append("exhaust_non_stock")
    if profile.maf not in (None, "stock_hfm"):
        triggers.append("maf_non_stock")
    if profile.injectors not in (None, "stock"):
        triggers.append("injectors_non_stock")
    if profile.hardcut:
        triggers.append("hardcut_requested")
    if profile.popcorn:
        triggers.append("popcorn_requested")

    work_keys: list[str] = []
    for trigger in triggers:
        for key in TRIGGERS[trigger]:
            if key not in work_keys:
                work_keys.append(key)
    # A car has one transmission: keep only the matching VANOS threshold table.
    if profile.transmission == "manual":
        work_keys = [key for key in work_keys if key != "vanos_switch_automatic"]
    elif profile.transmission == "automatic":
        work_keys = [key for key in work_keys if key != "vanos_switch_manual"]
        _finding(
            findings, "VANOS_AUTOMATIC_TABLE", "warning",
            "This car uses the separate automatic-transmission VANOS threshold table; do "
            "not copy the manual threshold values.",
        )

    work_order: list[dict[str, Any]] = []
    required_logging: list[str] = ["stage0"]
    for key in work_keys:
        entry = CALIBRATION_WORK[key]
        resolved = []
        for name in entry["symbols"]:
            found = symbols.get(name)
            if not found:
                _finding(
                    findings, "CALIBRATION_SYMBOL_MISSING", "reject",
                    f"Calibration object {name!r} for {key} is not present in the XDF; "
                    "the work order is not trustworthy.",
                )
                continue
            for item in found:
                resolved.append({"symbol": name, **item})
        for item in resolved:
            if item.get("data_addresses"):
                data = item["data_addresses"][0]
                if data.lower() != item["address"].lower():
                    item["data_address"] = data
        work_order.append({
            "key": key,
            "title": entry["title"],
            "reason": entry["reason"],
            "tool": entry.get("tool"),
            "objects": resolved,
            "validation": entry["validation"],
        })
        for logging in entry["logging"]:
            if logging not in required_logging:
                required_logging.append(logging)

    # --- stage decision -----------------------------------------------------
    known = (
        profile.engine in ENGINES
        and profile.transmission in TRANSMISSIONS
        and profile.manifold in MANIFOLDS
        and profile.cams in CAMS
        and profile.exhaust in EXHAUSTS
        and profile.intake in INTAKES
        and profile.maf in MAFS
        and profile.injectors in INJECTORS
        and profile.catalyst in CATALYSTS
        and profile.fuel_ron in RON
    )
    stage = "stage2" if (stage2_requested and triggers) else "stage1"

    if not known:
        _finding(
            findings, "PRESET_KEY_INCOMPLETE", "reject",
            "The preset key (engine + manifold + cams + exhaust + MAF + injectors + "
            "fuel RON + transmission) is incomplete; no baseline can be attributed.",
        )
    elif profile.maf_curve_documented is False and profile.injectors != "stock":
        _finding(
            findings, "PRESET_NOT_VALIDATED", "warning",
            "This combination is not in the validated preset database: the generator "
            "will emit a baseline and require logging, and will not claim a final Stage 2 tune.",
        )

    findings.extend(check_measurements(profile, measurements))
    severities = {finding.severity for finding in findings}
    if "reject" in severities:
        status = "REJECTED"
    elif "warning" in severities:
        status = "NEEDS_DATA_OR_VALIDATION"
    else:
        status = "ACCEPTED_WITH_LOGGING"

    preset_key = None
    if known:
        preset_key = " + ".join([
            profile.engine or "?", profile.manifold or "?", profile.cams or "?",
            profile.exhaust or "?", profile.maf or "?", profile.injectors or "?",
            f"RON{profile.fuel_ron}", profile.transmission or "?",
        ])

    return ProfileReport(
        status=status,
        stage=stage,
        findings=findings,
        preset_key=preset_key,
        work_order=work_order,
        required_logging=required_logging,
        notes=notes,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True, help="hardware profile JSON")
    parser.add_argument("--measurements", type=Path, help="optional logged measurements JSON")
    parser.add_argument("--xdf", type=Path, default=DEFAULT_XDF)
    parser.add_argument("--bin", type=Path, help="optional BIN to confirm MS41 identity")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        data = json.loads(args.profile.read_text(encoding="utf-8"))
        profile = parse_profile(data)
        measurements = (
            json.loads(args.measurements.read_text(encoding="utf-8"))
            if args.measurements else None
        )
        report = evaluate(profile, measurements, load_xdf_symbols(args.xdf))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    if args.bin:
        from MS41.tools.analyze_firmware import analyze

        analysis = analyze(args.bin)
        verdict = "SUPPORTED" if analysis.supported else "REJECTED"
        print(f"BIN {verdict}: {args.bin} ({analysis.image_type}, crc={analysis.crc_valid})")

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(f"{report.status} [{report.stage}] preset={report.preset_key or '-'}")
        for finding in report.findings:
            print(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
        for note in report.notes:
            print(f"  [NOTE] {note}")
        for entry in report.work_order:
            print(f"  WORK {entry['key']}: {entry['title']}")
            for item in entry["objects"]:
                target = item["address"]
                if item.get("data_address"):
                    target = f"{item['address']} (data {item['data_address']})"
                print(f"     {target:26} {item['symbol']}")
        if report.required_logging:
            print("  LOGGING: " + ", ".join(report.required_logging))
    return 0 if report.status != "REJECTED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
