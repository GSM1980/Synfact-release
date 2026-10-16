#!/usr/bin/env python3
"""Reproduce the pinned Synfact release from the user's official Elektron SYX."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zlib

ROOT = Path(__file__).resolve().parent
MAGIC = b"SYNFACT-XOR1\x00"


class BuildError(Exception):
    """An input or release artifact failed validation."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, spec: dict, label: str) -> None:
    if len(data) != spec["bytes"] or sha256(data) != spec["sha256"]:
        raise BuildError(f"{label}: size or SHA-256 mismatch.")


def apply_patch(original: bytes, patch: bytes, expected_size: int) -> bytes:
    """Decode a bounded XOR mask; requires the authenticated original."""
    header_size = len(MAGIC) + 4
    if patch[:len(MAGIC)] != MAGIC or len(patch) < header_size:
        raise BuildError("Invalid release patch header.")
    size = struct.unpack_from(">I", patch, len(MAGIC))[0]
    if size != expected_size or size > len(original):
        raise BuildError("Invalid release patch length.")
    decoder = zlib.decompressobj()
    try:
        mask = decoder.decompress(patch[header_size:], size + 1)
    except zlib.error as exc:
        raise BuildError("Corrupt release patch.") from exc
    if len(mask) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise BuildError("Truncated, oversized or trailing release patch data.")
    return bytes(a ^ b for a, b in zip(original, mask))


def download_machines(spec: dict) -> bytes:
    url = spec.get("url")
    if not url or urlparse(url).scheme != "https":
        raise BuildError("The release manifest must provide an HTTPS machines/mods URL.")
    request = Request(url, headers={"User-Agent": "Synfact-release-builder/1"})
    with urlopen(request, timeout=30) as response:
        if urlparse(response.geturl()).scheme != "https":
            raise BuildError("Machines download redirected to a non-HTTPS URL.")
        data = response.read(spec["bytes"] + 1)
    verify(data, spec, "Downloaded machines/mods file")
    return data


def build(original_path: Path, output_dir: Path) -> Path:
    release = json.loads((ROOT / "release/manifest.json").read_text(encoding="utf-8"))
    original = original_path.read_bytes()
    verify(original, release["original"], "Expected the unmodified official Syntakt OS 1.41 SYX")
    if output_dir.exists():
        raise BuildError(f"Output already exists: {output_dir}. Choose a new --output directory.")
    for spec in (release["firmware"], release["machines"], release["patch"]):
        name = spec["filename"]
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise BuildError("Release contains an invalid artifact filename.")
    patch = (ROOT / "release" / release["patch"]["filename"]).read_bytes()
    verify(patch, release["patch"], "Release patch")
    firmware = apply_patch(original, patch, release["firmware"]["bytes"])
    verify(firmware, release["firmware"], "Generated firmware")
    print("Official OS 1.41 verified; modified firmware matches the pinned release.", flush=True)
    print("Downloading machines/mods...", flush=True)
    machines = download_machines(release["machines"])
    # Publish only once BOTH artifacts have passed validation.
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".synfact-build-", dir=output_dir.parent) as staging:
        bundle = Path(staging) / "bundle"
        bundle.mkdir()
        (bundle / release["firmware"]["filename"]).write_bytes(firmware)
        (bundle / release["machines"]["filename"]).write_bytes(machines)
        (bundle / "build-info.json").write_text(
            json.dumps(release, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        bundle.rename(output_dir)
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path, help="Official, unmodified Syntakt OS 1.41 .syx")
    parser.add_argument("--output", type=Path, default=Path("dist"), help="New output directory (default: dist)")
    args = parser.parse_args()
    try:
        result = build(args.original, args.output.resolve())
    except (BuildError, OSError, URLError, ValueError, KeyError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Build complete: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
