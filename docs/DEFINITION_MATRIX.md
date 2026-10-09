# MS4X firmware / definition compatibility matrix

Definitions are not interchangeable between ECU families, software revisions, or
binary sizes. A plausible table display is not proof of compatibility.

| ECU | Firmware in this repository | Binary size | Preferred definition | Status |
|---|---|---:|---|---|
| MS41 | No verified stock binary yet | 24 KiB / 256 KiB | `MS41/definitions/Siemens_MS41_1429861_*` | Definitions only; no local BIN pairing |
| MS42 | 0110C6 full images | 512 KiB | `MS42/definitions/Siemens_MS42_0110C6_ENG_512K_v1.3.xdf` | Exact software and size match |
| MS42 | Calibration read | 32 KiB | `MS42/definitions/Siemens_MS42_0110C6_ENG_32K_v1.3.xdf` | Definition present; no local 32 KiB BIN |
| MS43 | MS430069 full images (18) | 512 KiB | `MS43/definitions/Siemens_MS43_430069_Extended_Load_512K.xdf` | Exact software and size match; definition is Extended Load edition. Writable: calibration + community patchlist |
| MS43 | MS430069 calibration read (1) | 64 KiB | `MS43/definitions/Siemens_MS43_430069_Extended_Load_64K.xdf` | Definition and one local 64 KiB read; 64 KiB reads are **not writable** (no boot/prog CRC) |
| MS43 | MS430037 stock (5 × 512 KiB + 6 reads) | 512 KiB / 64 KiB | `MS43/definitions/MS430037_Extended_Load_{512K,64K}.xdf` | Per-version policy built from this definition; calibration writes only, no patchlist (basedata is 430069-only) |
| MS43 | MS430055 stock (1 × 512 KiB + 2 reads) | 512 KiB / 64 KiB | `MS43/definitions/MS430055_Extended_Load_{512K,64K}.xdf` | Same; note 430055 and 430056 are not interchangeable |
| MS43 | MS430056 stock (2 × 512 KiB + 8 reads) | 512 KiB / 64 KiB | `MS43/definitions/MS430056_Extended_Load_{512K,64K}.xdf` | Same |
| MS43 | MS430066 stock (1 × 512 KiB + 4 reads) | 512 KiB / 64 KiB | `MS43/definitions/MS430066_Extended_Load_{512K,64K}.xdf` | Same |
| MS43 | MS430064 calibration reads (3) | 64 KiB | `MS430064_Extended_Load_64K.xdf` (not mirrored) | No full 512 KiB image → no policy, writes refused (`CAL_VERSION_UNSUPPORTED`); download with `MS43/tools/fetch_definitions.py --version 430064` |
| MS45.0 | 4560BO00S E83 partials | 116 KiB (118,784 bytes) | `MS45/definitions/Siemens_MS450_456BO02S_116K.xdf` or RomRaider XML | Upstream collection describes 4560BO00 as supported, but XDF identifies itself as 456BO02S; validate software ID and maps before writing |
| MS45.1 | 457LO02S/4570LO00 partials | 116 KiB (118,784 bytes) | `MS45/definitions/Siemens_MS451_457LO02S_116K.xdf` | Matching upstream set |

The 1024K MS45 XDF files are for full 1 MiB reads and must not be applied to the
116 KiB partial files. Conversely, the 116K XDF files must not be applied to a
full read.

## Included formats

- **XDF:** TunerPro table definitions.
- **RomRaider XML:** multi-revision MS45 community definitions.
- **A2L:** Siemens development descriptions used as reverse-engineering evidence.
  The included `456BG23P` and `457LG23P` A2Ls are not named for the final firmware
  revisions in the stock collection. They must not be treated as drop-in map
  definitions without address/conversion validation.

## Sources

- MS41/MS42/MS43 XDF: official `ms4x-net` GitHub repositories.
- MS45 XDF, RomRaider XML, and A2L: `pazi88/Siemens_ms45_RR-definitions`.
- Compatibility background: MS4X Wiki Definition Files page.

SHA-256 hashes are stored per ECU in
`MSxx/checksums/DEFINITIONS_SHA256SUMS.txt`; firmware hashes are stored beside
them in `MSxx/checksums/FIRMWARE_SHA256SUMS.txt`. The MS43 definition hashes were
verified against upstream on 2026-10-06 (10/10 files, see
`MS43/checksums/DEFINITIONS_SHA256SUMS.txt`), and per-version address proofs are
in `MS43/docs/VERSION_COVERAGE_RU.md`.
