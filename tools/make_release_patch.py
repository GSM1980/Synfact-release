#!/usr/bin/env python3
"""Maintainer utility: pin an existing release without copying either SYX into Git."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from urllib.parse import urlparse
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import MAGIC, apply_patch


def artifact(data: bytes, filename: str) -> dict:
    return {"filename": filename, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--modified", type=Path, required=True)
    parser.add_argument("--machines", type=Path, required=True)
    parser.add_argument("--machines-url", help="HTTPS URL; omit only while preparing a pending release")
    parser.add_argument("--output", type=Path, required=True, help="New release data directory")
    args = parser.parse_args()
    if args.machines_url and urlparse(args.machines_url).scheme != "https":
        parser.error("--machines-url must use HTTPS")
    original = args.original.read_bytes()
    expected = "8e2488f462c4a5656396a895f113bcd415e9900fa8709340dccf45d4cb9ed19e"
    if hashlib.sha256(original).hexdigest() != expected:
        parser.error("--original must be the official unmodified Syntakt OS 1.41")
    modified = args.modified.read_bytes()
    if not modified or len(modified) > len(original):
        parser.error("This fixed patch format requires a nonempty output no larger than the original")
    machines = args.machines.read_bytes()
    mask = bytes(a ^ b for a, b in zip(original, modified))
    patch = MAGIC + struct.pack(">I", len(modified)) + zlib.compress(mask, 9)
    if apply_patch(original, patch, len(modified)) != modified:
        parser.error("Patch round-trip failed")
    manifest = {
        "format": 1,
        "version": "0.0.3",
        "original": artifact(original, "Syntakt_OS1.41.syx"),
        "firmware": artifact(modified, "synfact_syx_v0.0.1.syx"),
        "machines": {**artifact(machines, "synfact_mods_v0.0.3.smz4"), "url": args.machines_url},
        "patch": artifact(patch, "firmware.xor.zlib"),
        "validation": {
            "protected_sections": [1, 2, 4, 5, 6, 8],
            "destinations": "unchanged in the pinned reference release",
            "full_app_boot_acceptance": False,
            "hardware_validation": False,
            "note": "Exact reproduction of the fixed experimental release; see README limitations.",
        },
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "firmware.xor.zlib").write_bytes(patch)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Created {args.output}: {len(patch)} patch bytes; exact round-trip verified.")


if __name__ == "__main__":
    main()
