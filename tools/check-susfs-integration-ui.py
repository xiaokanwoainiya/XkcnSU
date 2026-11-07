#!/usr/bin/env python3
"""Toggle the visible SUSFS integration row and verify immediate kernel state."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--expect", choices=("on", "off"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = {"completed": False, "checks": [], "commands": [], "expected": args.expect}

    def adb(*arguments):
        result = subprocess.run(["adb", "-s", args.serial, *arguments],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=30)
        report["commands"].append({"arguments": arguments, "exit_code": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    def root(*arguments):
        return adb("shell", shlex.join(["su", "-c", shlex.join(arguments)]))

    def check(value, description):
        report["checks"].append({"description": description, "passed": bool(value)})
        if not value:
            raise RuntimeError(description)

    try:
        check(args.execute, "Explicit UI execution is required")
        args.report.parent.mkdir(parents=True, exist_ok=True)
        remote = "/data/local/tmp/xkcnsu-susfs-ui-" + uuid.uuid4().hex + ".xml"
        adb("shell", "uiautomator", "dump", remote)
        xml = args.report.with_suffix(".xml")
        adb("pull", remote, str(xml))
        rows = [node for node in ET.parse(xml).iter("node")
                if node.get("package") == "io.xkcnsu.manager"
                and node.get("text") in ("SUSFS 集成", "SUSFS integration")]
        check(len(rows) == 1, "Exactly one SUSFS integration row is visible")
        before = json.loads(root("/data/adb/ksud", "susfs", "status"))
        boot = root("cat", "/proc/sys/kernel/random/boot_id")
        desired = args.expect == "on"
        report["before"] = before
        check(before["autoload"] != desired, "The switch is tested through an actual state change")
        left, top, right, bottom = map(int, re.findall(r"\d+", rows[0].get("bounds")))
        adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        deadline = time.monotonic() + 20
        while True:
            status = json.loads(root("/data/adb/ksud", "susfs", "status"))
            if (status["autoload"] == desired and status["loaded"] == desired) or time.monotonic() >= deadline:
                break
            time.sleep(1)
        report["after"] = status
        check(status["autoload"] == desired, "The autoload configuration matches the integration switch")
        check(status["loaded"] == desired, "The current runtime changes immediately with the same switch")
        if desired:
            check(status["supported"] is True, "The immediately loaded SUSFS guard is operational")
        check(root("cat", "/proc/sys/kernel/random/boot_id") == boot,
              "The integration change took effect without rebooting")
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"completed": report["completed"], "expected": args.expect,
                      "checks": len(report["checks"]), "error": report.get("error")}))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
