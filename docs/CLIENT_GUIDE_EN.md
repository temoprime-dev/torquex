# TorqueX client guide (EN) — read, flash, log, roll back

Date: **2026-10-07**. This guide is for someone with no previous experience. It
contains only what is confirmed by public instructions (sources in §9) and by our
own tools. There are no invented steps: if something does not match your ECU,
stop and ask instead of improvising.

> **Trust level.** Our files are verified **byte-by-byte and by checksum**
> ("bytes+crc verified") but have **never been tested on a live car**
> ("field verified" — not yet). The first drive follows
> `docs/FIELD_TEST_PROTOCOL_RU.md`, starting with the most conservative variant.

## 1. What you need

* A Windows laptop (WinKFP/BMW Standard Tools, MS4x Flasher, MS45-Flasher are
  Windows-only) and **stable power**: a charger or jumper from a second car
  supplying **at least 10 A** for the whole flash
  ([WinKFP guide](https://www.e46fanatics.com/threads/diy-how-to-flash-update-dme-or-egs.1324566/)).
* Cable: an INPA-compatible K-line/OBD2 adapter. For MS45-Flasher set the port
  latency to **1 ms** ([MS45-Flasher](https://github.com/terraphantm/MS45-Flasher)).
* The generator: this repository (`python3 tools/torquex.py …`).
* Logs: **Test0** (`docs/TEST0_CLIENT_GUIDE_EN.md`).

## 2. Step 0. A full stock read comes first

Take a **full read** of the ECU and keep it as a backup:

| ECU | Tool | Result |
| --- | --- | --- |
| MS42, MS43 | MS4x Flasher | "Partial Read" — 64 KiB calibration (~20 s), "Full Read" — 512 KiB (~120 s) |
| MS45 | MS45-Flasher | "Read DME"; enable "full binary" for a full backup — you get `_Flash` and `_mpc`, **both** are required |
| MS41 | your own proven read tool / boot mode | 24 KiB calibration or a 256 KiB full read |

Facts worth knowing ([MS4x Flasher](https://www.ms4x.net/index.php?title=MS4x_Flasher)):

* a 512 KiB full read **excludes the ISN area** — that is normal;
* "Partial Read" contains the calibration only: enough to write a calibration,
  not enough as an ECU backup;
* if the ECU is DS2 read-locked, reading is done in **boot mode** (JMGarage
  Flasher) — follow that tool's own instructions;
* MS4x Flasher can **correct checksums** and reset adaptations; our files already
  carry corrected checksums, but verifying them costs nothing.

**Rule:** the file written to an ECU must always be built from **your** stock.
Never flash someone else's stock.

## 3. Step 1. Identify the ECU and pick a variant

```bash
python3 tools/torquex.py detect /path/to/your_stock.bin
python3 tools/torquex.py variants --lang en
```

`detect` reports the ECU, the version and whether it is supported. An
unsupported version produces no file: on MS43 the DME is first updated to
**430069** (MS4x Flasher), because all addresses and bounds are proven against
verified versions only.

Variants:

* **v1 — basic** (start here): ignition up to +1.5° on proven maps, limiters and
  approved functions as separate requests; no logs needed;
* **v2 — extended**: needs a Test0 log (ignition/fuel from the log, VANOS from
  the cam log, rev limiters with acknowledgements);
* **v3 — no catalytic converter**: v2 + pops and the diagnostic layer; only with
  the converter physically removed and the legal notice acknowledged.

## 4. Step 2. Build the file

```bash
python3 tools/torquex.py build \
  --file /path/to/stock.bin \
  --variant v1 \
  --out  /path/to/result.bin \
  --note /path/to/delivery_note.md \
  --manifest /path/to/manifest.json
```

What you get:

* `result.bin` — only when the status is **BUILT** (the core writes nothing
  outside the allowed cells and recomputes the checksums itself);
* `delivery_note.md` — what changed and why, the bounds, what was *not* done and
  which data is still missing;
* `manifest.json` — sha256 of the stock and of the result, generator version, for
  verification and rollback.

Refusals are printed as human-readable text (codes are kept), in English and
Russian:

```
[CAL_VERSION_UNSUPPORTED]: This ECU version is not supported — …
```

Explain any code separately: `python3 tools/torquex.py --lang en explain <CODE>`.

## 5. Step 3. Flashing

The order is common to MS41–MS45 (per public instructions, §9):

1. charger/jumper ≥ 10 A connected, all consumers off;
2. key to position 2 (ignition on, engine not started);
3. cable connected; **never touch the connector while flashing**;
4. flash:
   * MS42/MS43 — MS4x Flasher: "Load file" → "Full write" (if only calibration
     changed, "Partial write" skips the program code);
   * MS45 — MS45-Flasher: "Load file" → "Flash Tune"; for full files "Load file"
     + "Load file 2 (MPC)" → "Flash Program". **Never mix files from different
     ECUs**: the author warns the DME can become unbootable;
   * MS41 — with a proven tool for that exact calibration.
5. wait for the success message (WinKFP timings: MS43 ≈20 min, MS45.1 ≈43 min;
   MS4x Flasher 512 KiB ≈2 min);
6. switch the ignition off, wait a minute, switch it on again;
7. read fault codes and **reset adaptations** with standard diagnostics (INPA /
   MS4x Flasher / PA Soft);
8. start the engine, let it warm up, listen and observe.

Do not:

* interrupt power or unplug the cable;
* tick WinKFP's "bootsector update" unless you must — per
  [Bimmerlabs](https://www.bimmerlabs.com/help) it can brick certain DMEs;
* flash a file whose `sha256` does not match the manifest;
* attempt MS41/MS42 boot-mode flashing on a bench without experience — community
  guides (e.g. JMGarage) describe it, but we have not verified it and do not
  recommend it for a beginner;
* believe "+X hp" claims: we promise no power gain and measure none.

## 6. Step 4. Verify the produced file

```bash
python3 -m MS45.tools.stages --verify-manifest /path/to/manifest.json --image /path/to/result.bin
# MS43: python3 -m MS43.tools.stages --verify-manifest … --image …
```

Expected answer: **VERIFIED** — bytes, checksums, and "nothing changed outside
the plan".

## 7. Step 5. A Test0 log (required for variants 2/3 and for any report)

See `docs/TEST0_CLIENT_GUIDE_EN.md` (channels; presets for the new functions in
§5). In short: log **before and after** flashing, on a warm engine, with pulls to
the limiter and a steady part-load section. Analysis is done by our tools
(`tools/pack_log_analyzer.py`); the client does not compute anything by hand.

## 8. Step 6. Roll back with one command

```bash
python3 tools/torquex.py rollback \
  --manifest /path/to/manifest.json \
  --stock    /path/to/stock.bin \
  --written  /path/to/result.bin \
  --out      /path/to/stock_to_flash.bin
```

The tool checks the stock sha256 against the manifest, verifies the produced
file, and verifies the checksums of the restored image. Then flash
`stock_to_flash.bin` the same way as the tune and reset adaptations again.
Remember: the stock must be the very file the tune was built from.

## 9. Sources (checked 2026-10-07)

* MS4x Flasher official wiki: MS42/MS43 read/write, sizes and timing, EWS sync
  (beta), adaptation reset, checksum correction, boot mode for read-locked ECUs —
  <https://www.ms4x.net/index.php?title=MS4x_Flasher>
* MS45-Flasher (terraphantm): INPA-compatible cable, 1 ms latency, full backup =
  `_Flash` + `_mpc`, mismatched-file warning —
  <https://github.com/terraphantm/MS45-Flasher>
* WinKFP: ≥10 A supply, key position, MS43 ≈20 min / MS45.1 ≈43 min, adaptation
  reset afterwards —
  <https://www.e46fanatics.com/threads/diy-how-to-flash-update-dme-or-egs.1324566/>
  and <https://www.bimmerlabs.com/help> (bootsector update, program → data order)
* MS41/MS42: boot-mode read/write, EWS sync after a "virgin" flash — community
  practice (JMGarage; forum threads). Not verified by us; not recommended for a
  beginner.
* Test0 logging — the project's official pages plus our documents
  (`docs/TEST0_CLIENT_GUIDE_EN.md`, `docs/TEST0_WEB_ANALYSIS_RU.md`).

Limitations and everything still waiting for data are listed in
`docs/RELEASE_CHECKLIST_RU.md` and `docs/HANDOFF_NEXT_SESSION_RU.md`.
