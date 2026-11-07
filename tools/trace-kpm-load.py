#!/usr/bin/env python3
"""Capture a bounded, explicit KPM load together with live kernel diagnostics."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--id")
    parser.add_argument("--native-probe", choices=(
        "hello", "null-load", "missing-load", "fixture-load", "fixture-buffer-load",
        "fixture-read", "fixture-buffer-nullargs"))
    parser.add_argument("--args", default="XKCNSU-acceptance")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.native_probe and not args.id:
        parser.error("--id is required unless --native-probe is selected")
    args.output.mkdir(parents=True, exist_ok=True)
    base = ["adb", "-s", args.serial, "shell"]

    def command(arguments):
        return base + ["su -c " + shlex.quote(shlex.join(arguments))]

    before = subprocess.run(command(["cat", "/proc/sys/kernel/random/boot_id"]),
                            capture_output=True, text=True, timeout=10)
    report = {"serial": args.serial, "id": args.id, "native_probe": args.native_probe,
              "boot_id_before": before.stdout.strip(), "completed": False}
    with (args.output / "kernel-live.log").open("wb") as log:
        # A PTY makes dmesg flush each line before a possible kernel crash.
        stream_command = command(["dmesg", "-w"])
        stream_command.insert(len(base), "-tt")
        stream = subprocess.Popen(stream_command, stdout=log, stderr=subprocess.STDOUT)
        try:
            time.sleep(1)
            operation = (
                ["/data/local/tmp/xkcnsu-kpm-transport-check", args.native_probe]
                if args.native_probe else
                ["/data/adb/ksud", "kpm", "load", args.id, "--args", args.args]
            )
            operation_command = command(operation)
            operation_command.insert(len(base), "-tt")
            result = subprocess.run(operation_command,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
            report.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
            after = subprocess.run(command(["cat", "/proc/sys/kernel/random/boot_id"]),
                                   capture_output=True, text=True, timeout=10)
            report["boot_id_after"] = after.stdout.strip()
            if args.native_probe:
                values = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                report["completed"] = (
                    result.returncode == 0 and
                    any(value.get("stage") == "returned" for value in values) and
                    report["boot_id_before"] == report["boot_id_after"]
                )
            else:
                value = json.loads(result.stdout)
                report["completed"] = (
                    result.returncode == 0 and value.get("ok") is True and
                    report["boot_id_before"] == report["boot_id_after"])
        except Exception as error:
            report["error"] = str(error)
        finally:
            stream.terminate()
            try:
                stream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stream.kill()
                stream.wait(timeout=5)
    lines = (args.output / "kernel-live.log").read_text(encoding="utf-8", errors="replace").splitlines()
    report["load_checkpoints"] = [line for line in lines if "checkpoint=" in line][-40:]
    report["failure_lines"] = [line for line in lines if any(
        text in line for text in ("Kernel panic", "BUG:", "KASAN:", "CFI failure", "Internal error"))][-20:]
    (args.output / "load.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["completed"] else 1)


if __name__ == "__main__":
    main()
