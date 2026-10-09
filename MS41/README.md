# Siemens MS41 stock firmware

## Where the code side stands

The generator covers all ten request kinds and refuses to guess: a request is
either planned (values, provenance, CRC-checked diff) or answered with an
evidence plan naming the tables, their addresses, the log sections and the bench
gates it is waiting on. Nothing is ever written without a source for the number.

```bash
python3 -m unittest discover -s MS41/tests -t .   # 352 tests
```

What is left is not code: bench measurements for ignition, fuelling and popcorn,
road confirmation of the launch limiter after flashing, and the web interface
(a separate session). Flashing itself is a hardware step (QuickFlash), not
something these tools do. `docs/EVIDENCE_REQUIREMENTS_RU.md` lists the
procedures, the deliberately closed symbols, and the order of work once a dyno
is available.

The official `ms4x-net/ms41` repository currently contains definitions only;
the stock fixtures here were independently sourced from the EUROTUN archive and
validated by binary structure and checksums.

The `definitions/` directory contains the official MS4X English and German
TunerPro XDF files for hardware/software 1429861 in 24 KiB and 256 KiB layouts.

`firmware/` contains verified 24 KiB calibration and 256 KiB full-flash fixtures.
Despite `Bosch` appearing in some source-database filenames, binary-content
analysis confirms these are **Siemens MS41** images: they have the MS41 24K
calibration layout, `064101` software-family identity, calibration at `0x14000`
in full images, and a valid 15-block MS41 CRC chain.

Run the content-based detector against one or more files:

```bash
python3 MS41/tools/analyze_firmware.py MS41/firmware/*.bin
python3 MS41/tools/analyze_firmware.py --json MS41/firmware/example.bin
```

The detector never trusts manufacturer text in a filename. Unsupported sizes,
missing MS41 signatures, or invalid calibration CRCs are rejected.

Run the first generator component, the conservative Stage 0 health check:

```bash
cp MS41/examples/stage0_evidence.example.json /tmp/my-car.json
# Replace the example measurements and confirmations with real vehicle evidence.
python3 MS41/tools/health_check.py \
  MS41/firmware/example.bin --evidence /tmp/my-car.json
python3 MS41/tools/health_check.py \
  MS41/firmware/example.bin --evidence /tmp/my-car.json --json
```

The result is `READY`, `NEEDS_DIAGNOSIS`, or `REJECTED`. A valid BIN alone is
never enough for `READY`: the tool also requires explicit DTC scans, both-bank
fuel trims, fuel grade, and Stage 0 confirmations. Default fuel-trim policy is
±10% warning and ±15% rejection and is recorded in every report; both limits
can be overridden on the command line when a different documented workshop
policy applies.

Test0/TestO is the recommended BMW logger. Inspect an exported CSV before it is
accepted for a tuning workflow:

```bash
python3 MS41/tools/analyze_testo_log.py drive.csv --profile stage0
python3 MS41/tools/analyze_testo_log.py drive.csv --profile wot --json
```

The parser supports legacy TestO and Test0 v2 CSV conventions, preserves every
source column, maps common English/German EDIABAS channel names, and reports
missing required signals, duration, average row rate and basic channel quality.
See [`docs/TEST0_LOGGING_RU.md`](docs/TEST0_LOGGING_RU.md) for setup, presets,
synchronisation limitations and safe logging procedure.

Exact channel names come from BMW's own ECU description files (SGBD `.prg`).
The pre-parsed bundle in [`diagnostics/`](diagnostics/README.md) lists every
MS41 diagnostic job, result, unit, fast-RAM address/scaling and fault code:

```bash
python3 MS41/tools/fetch_ms41_sgbd.py                 # decoded sources into MS41/.cache
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/*.md --outdir MS41/diagnostics
python3 MS41/tools/parse_sgbd.py MS41/.cache/sgbd/MS411DS1.md --emit channels --pretty
```

No BMW binary is committed; raw `.prg` files stay on the user's machine or in a
git-ignored cache. See [`docs/DIAGNOSTICS_JOBS_RU.md`](docs/DIAGNOSTICS_JOBS_RU.md)
for the variant inventory, fast-RAM scaling table and the `STATUS_SCHUB_AB_EIN`
overrun check.

Turn a customer request into a patch plan, then patch safely. Nothing is written
until the request is fully resolved, and every planned change carries a
justification and a post-flash validation hint:

```bash
python3 MS41/tools/generate_patches.py --bin stock.bin \
  --request MS41/examples/stage1_request.example.json --out patches.json
python3 MS41/tools/calibration.py --bin stock.bin --patches patches.json \
  --out patched.bin --report report.json
python3 MS41/tools/delivery_note.py --generation generation_report.json \
  --patch report.json --out note.md
```

`generate_patches.py` refuses popcorn on a catalyst-equipped car, needs an
explicit acknowledgement before raising the engine speed limit, and reports
ignition/fuelling/MAF rescaling as NOT SUPPORTED rather than guessing values.
`calibration.py` applies only allowlisted objects, rebuilds the 15-block CRC
chain and re-verifies every changed byte. `delivery_note.py` renders the
customer-facing note from the two reports, with every unverified item marked as
outstanding. See
[`docs/WORKFLOW_RU.md`](docs/WORKFLOW_RU.md) and
[`docs/CALIBRATION_RU.md`](docs/CALIBRATION_RU.md).

Replacing the MAF sensor means replacing its transfer curve. The voltage grid is
declared in the XDF (256 columns, 0.000 ... 5.018 V), so a documented supplier
curve can be resampled onto the ECU's own columns:

```bash
python3 MS41/tools/maf_curve.py --stock stock.bin --curve sensor_curve.json \
  --out maf_patches.json --report maf_report.json
```

The curve file must name the sensor, the source and the provenance kind, and must
be a non-decreasing function of voltage. Columns beyond the supplier curve are
held at its edge values and counted in the report, so the extrapolated part is
visible rather than implied. See
[`docs/CALIBRATION_RU.md`](docs/CALIBRATION_RU.md).

A map is only written to after its layout has been proved from the definition
itself: address, element size, the two axis companions (matched by point count
and units), and the orientation of load against engine speed:

```bash
python3 MS41/tools/table_layout.py --bin stock.bin           # the three key maps
python3 MS41/tools/table_layout.py --bin stock.bin --all      # every map with axes
```

Families without a declared trend report `orientation: not checked` instead of a
guess, and maps whose axis companion is missing from the XDF are reported as
problems. See [`docs/CALIBRATION_RU.md`](docs/CALIBRATION_RU.md).

Decisions that need logs read one documented file instead of scattered
hand-waving. It ships as a stub in which **every** value is null, and a stub that
carries numbers is refused:

```bash
python3 MS41/tools/log_evidence.py --check MS41/examples/log_evidence.stub.json
python3 MS41/tools/generate_patches.py --bin stock.bin --request req.json \
  --evidence MS41/examples/log_evidence.stub.json
```

While the file is a stub every log-dependent decision stays `REQUIRES_DATA`;
once real logs fill it, the same sections unlock them without code changes. See
[`docs/WORKFLOW_RU.md`](docs/WORKFLOW_RU.md).

Replacing the injectors means rescaling the base injection map. Two independent
estimates of the same factor have to agree: the documented flow ratio
(`stock_flow / new_flow`) and the logged long-term fuel trim (`1 + LTFT/100`).
Without a log the tool reports the factor, the 192 cells it would touch and what
it deliberately leaves alone, and writes nothing:

```bash
python3 MS41/tools/injector_scaling.py --bin stock.bin \
  --profile MS41/examples/stage2_profile_injectors.example.json \
  --evidence MS41/examples/fuel_scaling.stub.json
```

A disagreement above 10% is refused rather than averaged, a duty cycle above 92%
is refused rather than rescaled, and the minimum injection time, cranking maps
and adaptation limits stay stock and are named as outstanding. See
[`docs/WORKFLOW_RU.md`](docs/WORKFLOW_RU.md).

A higher MAF ceiling is only useful if the load limits follow it. The same
`maf_curve` intent plans `c_maf_max` and `c_maf_max_diag` at exactly the ratio
by which the ceiling rose - but only when the log shows the stock sensor was
saturated, because the XDF documents no `kg/h -> mg/stroke` conversion and the
ratio is the one scale-free rule available:

```bash
python3 MS41/tools/maf_limits.py --bin stock.bin --curve curve.json \
  --evidence MS41/examples/log_evidence.stub.json
```

Without that proof the curve is still written and the limits stay stock, with a
warning that the extra range is unreachable; a rise above 30% is refused as a
different project, and catalyst-heating and adaptation thresholds are never
moved. See [`docs/WORKFLOW_RU.md`](docs/WORKFLOW_RU.md).

A manifold or cam change moves the load curve the VANOS switch threshold was
written for. The threshold is therefore taken from a *measured* comparison of
the two cam positions - never copied, never shared between the manual and the
automatic table, and never guessed because the four numbers in `examples/` look
plausible:

```bash
python3 MS41/tools/vanos_switch.py --bin stock.bin --dump-stock -      # no data needed
python3 MS41/tools/vanos_switch.py --bin stock.bin --transmission manual \
  --curves measured_comparison.json --out vanos_patches.json
```

The tool checks the documented crossing against the curves, refuses a threshold
the measured load could never reach, writes only temperature columns at or above
the measurement temperature, and keeps the table's own "never switch" rows. The
shipped example file is deliberately marked `derived` and is refused - there is
no measured data in this repository. See
[`docs/WORKFLOW_RU.md`](docs/WORKFLOW_RU.md).

What still needs a bench is not a black box: each such request comes back with
the tables it would touch (with addresses), the log sections that must be filled
and the bench steps that must precede it - never with a guessed value:

```bash
python3 MS41/tools/generate_patches.py --bin stock.bin \
  --request <(echo '{"intents":[{"id":"i","kind":"ignition"}]}')
```

`docs/EVIDENCE_REQUIREMENTS_RU.md` is the full list: ignition, fuelling and
popcorn procedures, the deliberately closed symbols, and the order of work once
a dyno is available. Launch limiting is implemented, but MS41 does it by
shutting cylinders off - a road-speed limit, not an rpm limiter - so it is
refused on a catalyst car below 100 km/h.

Stage 2 is keyed to an exact hardware combination. Validate a profile and get a
calibration work order before anything is generated:

```bash
python3 MS41/tools/hardware_profile.py \
  --profile MS41/examples/stage2_profile.example.json \
  --measurements MS41/examples/stage2_measurements.example.json
```

The tool rejects undocumented MAFs, undocumented injectors, unknown
injector-plus-MAF combinations, popcorn with a catalyst, RON 91 with raised
compression, and non-MS41 engines. Every object in the work order is resolved
against the real XDF, including the difference between a table header address and
its data address. See [`docs/STAGE2_HARDWARE_RU.md`](docs/STAGE2_HARDWARE_RU.md).

Validate the 24K/256K XDF address relationship against every fixture:

```bash
python3 MS41/tools/validate_xdf_offsets.py \
  MS41/definitions/Siemens_MS41_1429861_ENG_24K_v1.0.xdf \
  MS41/definitions/Siemens_MS41_1429861_ENG_256K_v1.0.xdf \
  MS41/firmware/*.bin
```

The expected relationship is `full address = partial address + 0x14000`.

Generator research and requirements:

- [`docs/GENERATOR_REQUIREMENTS_RU.md`](docs/GENERATOR_REQUIREMENTS_RU.md) — Russian technical specification for Stage 1, Stage 2, selectable RON, hardcut, and controlled overrun burble.
- [`docs/STAGE1_WEB_ANALYSIS_RU.md`](docs/STAGE1_WEB_ANALYSIS_RU.md) — earlier Stage 1 research notes.

References:
- https://github.com/ms4x-net/ms41
- https://www.ms4x.net/index.php?title=Firmware_Files
