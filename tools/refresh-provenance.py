#!/usr/bin/env python3
"""Refresh current-tree hashes without changing pinned upstream attribution."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    path = root / "kernel/XKCNSU_HOOK_PROVENANCE.json"
    report = json.loads(path.read_text())
    upstream = root.parent / "KernelSU-Next"
    avc = {
        "source_path": "kernel/extras.c",
        "destination_path": "kernel/feature/avc_spoof.c",
        "compiled_object": "kernel/feature/avc_spoof.o",
        "adapted": True,
    }
    if not any(item["destination_path"] == avc["destination_path"] for item in report["files"]):
        avc["source_sha256"] = hashlib.sha256((upstream / avc["source_path"]).read_bytes()).hexdigest()
        report["files"].append(avc)
    for item in report["files"]:
        source = root / item["destination_path"]
        item["destination_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    for item in report["integration_files"]:
        item["sha256"] = hashlib.sha256((root / item["path"]).read_bytes()).hexdigest()
    report["adaptations"] = [
        item for item in report["adaptations"] if not item.startswith("Remove Next AVC extras")
    ]
    detail = "Port Next AVC feature 10003 with XKCNSU domain, live BTF validation, failure readback and shared SUSFS service"
    if detail not in report["adaptations"]:
        report["adaptations"].append(detail)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print("Refreshed kernel provenance hashes")


if __name__ == "__main__":
    main()
