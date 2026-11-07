#!/usr/bin/env python3
"""Verify missing KPM record recovery without writing a boot partition."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--ksud", default="/data/adb/ksud")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"completed": False, "checks": [], "commands": []}
    saved = None
    record = None

    def run(*arguments):
        result = subprocess.run(
            ["adb", "-s", args.serial, "shell",
             "su -c " + shlex.quote(shlex.join(arguments))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        report["commands"].append({
            "arguments": list(arguments), "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        })
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    def check(passed, description):
        report["checks"].append({"description": description, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(description)

    def status(*arguments):
        value = json.loads(run(args.ksud, "kpm", *arguments))
        check(value.get("ok") is True, "KPM returns a successful JSON envelope")
        return value["data"]

    try:
        slot = run("getprop", "ro.boot.slot_suffix")
        check(slot in ("", "_a", "_b"), "The boot slot suffix is valid")
        boot_id = run("cat", "/proc/sys/kernel/random/boot_id")
        before = status("status")
        check(before["active"] and before["configured_enabled"]
              and before["provider"] == "xkcnsu" and not before["reboot_required"],
              "The existing XKCNSU KPM engine is configured and active")
        partitions = ["/dev/block/by-name/" + name + slot
                      for name in ("boot", "init_boot", "vendor_boot")]
        before_hashes = run("sha256sum", *partitions)
        record = "/data/adb/ksu/XKCNSU/kpm/integration/state" + (slot or "_single") + ".json"
        saved = record + ".acceptance-" + uuid.uuid4().hex
        run("test", "!", "-L", record)
        run("test", "!", "-e", saved)
        original = json.loads(run("cat", record))
        run("mv", record, saved)
        recovered = status("status")
        check(recovered["active"] and recovered["configured_enabled"]
              and recovered["provider"] == "xkcnsu"
              and not recovered["transition_failed"] and not recovered["reboot_required"],
              "A missing record recovers the running engine without misidentifying it")
        rebuilt = json.loads(run("cat", record))
        check(rebuilt["patched_sha256"] == original["patched_sha256"]
              and rebuilt["original_sha256"] == original["original_sha256"],
              "Recovered kernel checksums agree with the previous verified record")
        check(re.fullmatch(r"[0-9a-f]{64}", rebuilt["original_sha256"]) is not None
              and rebuilt["original_kernel"].startswith("/data/adb/ksu/XKCNSU/kpm/integration/"),
              "The recovery record uses the requested KSU working directory")
        enabled = status("integration", "enable")
        check(enabled["configured_enabled"] and not enabled["reboot_required"],
              "Enabling an already running XKCNSU engine succeeds without reinstalling it")
        check(run("sha256sum", *partitions) == before_hashes,
              "Recovery and idempotent enabling did not write any boot partition")
        check(run("cat", "/proc/sys/kernel/random/boot_id") == boot_id,
              "Recovery completed in the same kernel boot")
        check("uid=0(root)" in run("id"), "Root remains available")
        run("rm", saved)
        saved = None
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if saved is not None and record is not None:
            try:
                run("mv", saved, record)
                report["previous_record_restored"] = True
            except Exception as error:
                report["restore_error"] = str(error)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n",
                               encoding="utf-8")
    print(json.dumps({"completed": report["completed"], "checks": report["checks"],
                      "error": report.get("error"), "restore_error": report.get("restore_error")}))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
