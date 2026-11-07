#!/usr/bin/env python3
"""Build the eight ARM64 KMI pairs, with a priority 6.1 gate and bounded concurrency."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import time

KMIS = (
    "android14-6.1", "android12-5.10", "android13-5.10", "android13-5.15",
    "android14-5.15", "android15-6.6", "android16-6.12", "android17-6.18",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=1)
    parser.add_argument("--kmi", action="append", choices=KMIS)
    parser.add_argument("--priority-already-passed", action="store_true")
    parser.add_argument("--stage-assets", action="store_true")
    parser.add_argument("--mode", choices=("root", "both"), default="both")
    args = parser.parse_args()
    root = Path(os.environ.get("XKCNSU_ROOT", Path(__file__).resolve().parents[1]))
    out = Path(os.environ.get("XKCNSU_OUT", root / "out"))
    logs = out / "build-logs"
    logs.mkdir(parents=True, exist_ok=True)
    results = {}

    def build(kmi: str):
        started = time.time()
        log = logs / f"matrix-{kmi}-{int(started)}.log"
        with log.open("w") as stream:
            status = subprocess.call(
                ["bash", str(root / "tools/build-lkm.sh"), kmi, args.mode],
                stdout=stream, stderr=subprocess.STDOUT,
            )
        if status == 0 and args.stage_assets:
            assets = root / "userspace/ksud/bin/aarch64"
            assets.mkdir(parents=True, exist_ok=True)
            suffixes = ("kernelsu",) if args.mode == "root" else ("kernelsu", "susfs_guard_lkm")
            for suffix in suffixes:
                source = out / f"aarch64/{kmi}_{suffix}.ko"
                if not source.is_file():
                    status = 1
                    break
                temporary = assets / f".{source.name}.tmp"
                temporary.write_bytes(source.read_bytes())
                temporary.replace(assets / source.name)
        return kmi, {
            "exit_code": status, "log": str(log),
            "elapsed_seconds": round(time.time() - started, 2),
        }

    selected = args.kmi or list(KMIS)
    if not args.priority_already_passed:
        kmi, result = build("android14-6.1")
        results[kmi] = result
        print(json.dumps({kmi: result}), flush=True)
        if result["exit_code"]:
            return result["exit_code"]
        selected = [item for item in selected if item != "android14-6.1"]
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [executor.submit(build, kmi) for kmi in selected]
        for future in as_completed(futures):
            kmi, result = future.result()
            results[kmi] = result
            print(json.dumps({kmi: result}), flush=True)
    manifests = out / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests / f"matrix-{int(time.time())}.json"
    manifest.write_text(json.dumps(results, indent=2) + "\n")
    return int(any(item["exit_code"] for item in results.values()))


if __name__ == "__main__":
    sys.exit(main())
