# Client guide: recording a Test0 log (MS41 / MS42 / MS43 / MS45)

This document is for the vehicle owner. The goal is to record a log that **proves**
how the file works and unlocks the next tuning step. Test0 has no official preset
file format, so you build the channel set once by hand from a preset file — after
that it starts with a single button.

All channel names come from BMW PRG (SGBD) dumps: nothing in the presets is
invented, every name is a real diagnostics field. Format details:
`docs/TEST0_WEB_ANALYSIS_RU.md` (Russian).

## 1. What to install

1. Download Test0 v2 from `https://phenoboy.kapsi.fi/Test0-2.0/downloads.html`
   (installer `test0-2.0-setup-*.exe`, Windows 10/11 x64).
2. Download the ECU bundle (`test0-2.0-ecu-bundle-*.exe`) and install it — it
   places the ECU files into `<Test0 install dir>\Ecu` by default. Without them
   no jobs are available.
3. Test0 settings live in `testo.ini` next to `testo.exe`; no changes are needed
   for the first run.

## 2. Connecting to the car

* Use any adapter that works through **EdiabasLib** (default) or the original BMW
  EDIABAS (switchable in the settings).
* No connection: check the adapter COM port and that `api64.exe` / `ebas32.dll`
  are present in the Test0 folder (without them EDIABAS requests time out).
* Ignition on; engine off for the connection check.

## 3. Selecting the ECU and version

* File → Detect ECU: make sure your ECU **and version** are detected:
  MS41 (M52), MS42 (M52TU), MS43 (M54), MS45/MS45.1 (M54/M56).
* If a similar family is detected — select the correct one manually; a log from
  a wrong ECU is not accepted.

## 4. Building a preset (once per function)

Preset files live in the repository: `docs/test0_presets/<ECU>_<function>.json`
(for example `MS41_stage0.json`, `MS43_ignition.json`, `MS45_service.json`).

1. Open the JSON and look at `jobs` — pairs of “job → results”.
2. In Test0 select the job, find the result from the preset and drag it into the
   graph. Repeat for every row.
3. Settings → Logging: keep the default log folder and CSV separator.
4. Save the set as a Test0 preset named `TorqueX_<ECU>_<function>`
   (e.g. `TorqueX_MS43_ignition`).
5. Before the run, start recording and verify that **all** columns are filling in
   (an empty column means the channel did not get into the log).

Channels your ECU does not expose are listed in the same JSON
(`unavailable_ru`); substitutions are allowed only where documented
(`docs/TEST0_WEB_ANALYSIS_RU.md` §6).

## 5. What to record

| Function (file) | What we need from you |
|---|---|
| `stage0` | Cold start, warm-up to 90 °C, 10–15 minutes of city driving. Write down air temperature, fuel (station, RON), date. |
| `ignition` | One gear (usually 3rd): **3 full-throttle pulls** from 2000 to 6000+ rpm, with 30–60 seconds of calm driving in between. Fuel must be the one the file was ordered for (RON 98 for ignition evidence). |
| `fuel` | Connect the external wideband lambda (see §6). 2 full-throttle pulls + 10 seconds of steady part-load driving. |
| `vanos` | Warm engine: hold 2000/3000/4000/5000/6000 rpm for 3–5 seconds each (where safe) + one control pull. |
| `limits` | Rev-limiter check: 2–3 short approaches; speed-limiter check only on a closed course. |
| `service` | 2 minutes of warm idle; let the engine heat up until the fan engages; check DISA/ISA in the log. |
| `octane` | Fuel of the octane the file was ordered for: 3 full-throttle pulls in one gear. The log shows how ignition behaves on this fuel. |
| `disa_off` | After DISA removal/disable: 2 smooth pulls 1500→3500 rpm at 40–60 % pedal, 10 seconds steady driving at 2000 rpm, one minute of idle. |
| `isa_off` | After disabling the idle control valve: cold start, 2 minutes of warm idle (1 minute with A/C on), 3 short throttle blips. |
| `idle` | Changed idle speed: warm up to 90 °C, 2 minutes of idle, A/C on/off, add electrical load (lights, rear defroster). |
| `fan_thermostat` | Fan and thermostat: warm idle until the fan engages, note the on/off temperatures, repeat the cycle (two stages). |

Rules: pulls on a safe road and within the traffic rules; stop the run if you hear
knock, feel a power drop or get an error, and note it; the number of pulls matters —
fewer than three (for ignition) will not be accepted.

### 5.1. Why logs for the “service” functions

`disa_off`, `isa_off`, `idle`, `fan_thermostat`, `octane` verify that the car
behaves as expected after the change:

* preset files: `docs/test0_presets/<ECU>_<function>.json` (for example
  `MS43_idle.json`);
* each preset lists the driving recipe in `drive_ru` and the channels your ECU
  does not expose in `unavailable_ru`;
* these logs do not “prove” power and are not a promise: they record behaviour
  (idle speed, temperatures, intake) before/after and are needed for the report
  and for troubleshooting;
* attach a note to the log: what was done to the car (file, date), fuel,
  weather, what you felt (hesitation, unstable idle, noise).

## 6. External wideband lambda

* The stock MS41–MS45 lambdas are narrowband — the fuel package requires an
  external controller (Innovate, AEM, PLX, 14Point7, Techedge).
* `testo.ini` has `[external_source]` (device/COM port) and `[14point7]`
  (`lambda2afr=14.7`). Check whether the log records **λ** or **AFR** — the
  analyzer accepts both and converts AFR to λ using 14.7 (the conversion is shown
  in the report).
* Calibrate the controller (free air) before the run.

## 7. Where logs are stored and what to send

* CSV logs are in `<Desktop>\Test0_Logs` (or the folder from Settings → Logging).
* The native Test0 database is a per-day file: `YYYY_MM_DD.db` in the same folder.
* Send **either format** (both are supported): CSV or `YYYY_MM_DD.db`. If you have
  both — send both; CSV is easier to inspect, `.db` is more precise.
* In your message specify: ECU and version, function (which preset), date, fuel
  (station/RON), air temperature, and anything you noticed (knock, power drop,
  errors).
* **Personal data:** do not send the VIN or anything containing it (screenshots,
  paperwork). Logs and VINs never enter the project repository.

## 8. What happens next

1. The log is processed by the analyzer:
   `python3 tools/pack_log_analyzer.py --pack ignition --csv log.csv --ron 98 --out evidence.json`
   (for a `.db` use the same `--csv` key — the analyzer detects the format).
2. The report shows: sample rate, detected pulls, package metrics, remarks, and
   the **exact list of missing channels** with the jobs to enable in Test0.
3. If channels are missing — update the preset and record again. If everything is
   there — the log becomes evidence (`evidence*.json`); the generator verifies its
   sha256 and analyzer version and only then produces the file / next step.

No “+X hp” promises: steps are unlocked by evidence, one at a time, with knock and
temperature protection preserved.

## 9. Troubleshooting

| Symptom | What to check |
|---|---|
| Empty columns in the log | Channels were selected **before** recording; jobs belong to your ECU. |
| Timeouts, no connection | COM port; `api64.exe`/`ebas32.dll` present; EdiabasLib ↔ BMW EDIABAS switch. |
| “Log too slow” | Do not enable extra jobs: each channel costs polling time; the presets use 8–10. |
| Analyzer says “нет обязательных каналов” (missing mandatory channels) | The report lists the channels and jobs; enable them and record again. |
| DISA/ISA channel missing | MS42/MS43 have no DISA measurement in diagnostics (control only) — see `unavailable_ru` in the preset. |
