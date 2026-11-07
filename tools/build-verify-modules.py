#!/usr/bin/env python3
"""Reject missing KMI pairs, wrong architectures, or stale signing identities."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import sys

KMIS = (
    "android12-5.10", "android13-5.10", "android13-5.15", "android14-5.15",
    "android14-6.1", "android15-6.6", "android16-6.12", "android17-6.18",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kmi", action="append", choices=KMIS)
    args = parser.parse_args()
    root = Path(os.environ.get("XKCNSU_ROOT", Path(__file__).resolve().parents[1]))
    out = Path(os.environ.get("XKCNSU_OUT", root / "out"))
    signing = json.loads((out / "private/certificate.json").read_text())
    release = configparser.ConfigParser(interpolation=None)
    release.read_string("[release]\n" + (root / "version.properties").read_text())
    version_code = release["release"].getint("XKCNSU_VERSION_CODE")
    errors = []
    artifacts = []
    for kmi in args.kmi or KMIS:
        for suffix in ("kernelsu", "susfs_guard_lkm"):
            path = out / f"aarch64/{kmi}_{suffix}.ko"
            if not path.is_file():
                errors.append(f"Missing {path.name}")
                continue
            data = path.read_bytes()
            if data[:4] != b"\x7fELF" or int.from_bytes(data[18:20], "little") != 183:
                errors.append(f"{path.name} is not an AArch64 ELF")
            if suffix == "kernelsu":
                if f"welcome to XKCNSU version {version_code}, package name ".encode() not in data:
                    errors.append(f"{path.name} has a stale root version")
                if signing["KSU_EXPECTED_HASH"].encode() not in data:
                    errors.append(f"{path.name} has a stale Manager signing certificate")
                if signing["KSU_PACKAGE_NAME"].encode() not in data:
                    errors.append(f"{path.name} has the wrong Manager package")
            artifacts.append({
                "name": path.name, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    report = {"version_code": version_code, "signing": signing, "artifacts": artifacts, "errors": errors}
    (out / "manifests").mkdir(parents=True, exist_ok=True)
    (out / "manifests/verified-modules.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"artifacts": len(artifacts), "errors": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())
