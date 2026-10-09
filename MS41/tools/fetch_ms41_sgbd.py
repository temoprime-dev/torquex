#!/usr/bin/env python3
"""Download decoded MS41 SGBD (*.prg) documentation into a local cache.

The six MS41 diagnostic description files (MS410DS0 … MS411DS2) define every
EDIABAS job and result available on the ECU. BMW distributes them as encrypted
BEST object files; the cached copies here are the openly published *decoded*
form, which is what `parse_sgbd.py` reads. No BMW binary is redistributed by
this repository, and the cache directory is git-ignored.

Usage:

    python3 MS41/tools/fetch_ms41_sgbd.py                 # into MS41/.cache/sgbd
    python3 MS41/tools/fetch_ms41_sgbd.py --outdir /tmp/x
    python3 MS41/tools/fetch_ms41_sgbd.py --from-org-dir C:/EDIABAS/Ecu
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

VARIANTS = ("MS410DS0", "MS410DS1", "MS410DS2", "MS410DS3", "MS411DS1", "MS411DS2")
DEFAULT_OUTDIR = Path(__file__).parents[1] / ".cache" / "sgbd"
REPO = "emdzej/ediabasx-docs-sgbd"
DOC_DIR = "docs/sgbd"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/HEAD/{DOC_DIR}"
API_BASE = f"https://api.github.com/repos/{REPO}/contents/{DOC_DIR}"


def _download_raw(variant: str) -> str | None:
    url = f"{RAW_BASE}/{variant}.md"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None


def _download_gh(variant: str) -> str | None:
    """Fallback for environments where raw.githubusercontent.com is blocked."""
    if shutil.which("gh") is None:
        return None
    try:
        payload = subprocess.run(
            ["gh", "api", f"repos/{REPO}/contents/{DOC_DIR}/{variant}.md", "--jq", ".content"],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    import base64

    try:
        return base64.b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def fetch(outdir: Path, force: bool = False) -> dict[str, str]:
    outdir.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for variant in VARIANTS:
        target = outdir / f"{variant}.md"
        if target.exists() and not force:
            status[variant] = "cached"
            continue
        content = _download_raw(variant) or _download_gh(variant)
        if content is None:
            status[variant] = "failed"
            continue
        target.write_text(content, encoding="utf-8")
        status[variant] = "downloaded"
    return status


def from_directory(source: Path, outdir: Path) -> dict[str, str]:
    outdir.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for variant in VARIANTS:
        candidates = list(source.glob(f"{variant}.md")) + list(source.glob(f"{variant}.b1v"))
        if not candidates:
            status[variant] = "missing"
            continue
        target = outdir / f"{variant}.md"
        if candidates[0].suffix.lower() == ".md":
            shutil.copyfile(candidates[0], target)
        else:
            shutil.copyfile(candidates[0], target.with_suffix(".b1v"))
            status[variant] = "copied (b1v; decode with BimmerDis first)"
            continue
        status[variant] = "copied"
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--from-org-dir", type=Path,
                        help="directory with decoded .md/.b1v files instead of downloading")
    parser.add_argument("--force", action="store_true", help="re-download cached files")
    args = parser.parse_args()

    status = (
        from_directory(args.from_org_dir, args.outdir)
        if args.from_org_dir
        else fetch(args.outdir, args.force)
    )
    print(json.dumps(status, indent=2))
    if any(value in {"failed", "missing"} for value in status.values()):
        print(
            "Some variants could not be obtained. Provide decoded files with "
            "--from-org-dir, or decode your own EDIABAS .prg with BimmerDis/ediabasx.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
