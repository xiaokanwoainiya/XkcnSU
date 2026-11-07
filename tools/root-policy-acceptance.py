#!/usr/bin/env python3
"""Probe the temporary test app after an actual Manager authorization change."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid

PACKAGE = "io.xkcnsu.acceptance.rootpolicy"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--expect", choices=("granted", "denied"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--foreground", action="store_true",
                        help="Launch ProbeActivity to keep the probe app in the foreground")
    args = parser.parse_args()
    nonce = uuid.uuid4().hex
    report = {"serial": args.serial, "expected": args.expect, "nonce": nonce,
              "mode": "foreground" if args.foreground else "broadcast",
              "read_log_as_root": True,
              "completed": False, "checks": [], "started_unix": time.time()}

    def adb(*arguments):
        result = subprocess.run(["adb", "-s", args.serial, *arguments],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20)
        if result.returncode:
            raise RuntimeError("ADB command failed: " + result.stderr.strip())
        return result.stdout

    def check(value, description):
        report["checks"].append({"description": description, "passed": bool(value)})
        if not value:
            raise RuntimeError(description)

    try:
        listing = adb("shell", shlex.join(["pm", "list", "packages", "-U", PACKAGE]))
        match = re.fullmatch(r"package:" + re.escape(PACKAGE) + r" uid:(\d+)\s*", listing)
        check(match is not None, "Exactly the temporary acceptance package is selected")
        uid = int(match[1])
        check(uid >= 10000, "Probe runs as a real application UID")
        if args.foreground:
            adb("shell", shlex.join([
                "am", "start", "-S", "-n", PACKAGE + "/.ProbeActivity",
                "--es", "nonce", nonce,
            ]))
        else:
            adb("shell", shlex.join([
                "am", "broadcast", "--include-stopped-packages", "--receiver-foreground",
                "-a", PACKAGE + ".PROBE", "-n", PACKAGE + "/.ProbeReceiver",
                "--es", "nonce", nonce,
            ]))
        deadline = time.monotonic() + 15
        event = None
        while time.monotonic() < deadline:
            lines = adb("shell", shlex.join([
                "su", "-c", shlex.join([
                    "logcat", "-d", "-b", "all", "-v", "raw",
                    "-s", "XKCNSUAcceptance:I", "*:S",
                ]),
            ]))
            for line in reversed(lines.splitlines()):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("nonce") == nonce:
                    event = value
                    break
            if event is not None:
                break
            time.sleep(1)
        report["probe"] = event
        check(event is not None, "The app completed this exact nonce probe")
        check(event["uid"] == uid, "The probe belongs to the selected installed app")
        hidden_su = args.expect == "denied" and (
            'Cannot run program "/system/bin/su": error=2' in (event["error"] or "")
        )
        check(not event["timed_out"] and (event["error"] is None or hidden_su),
              "The probe completed without a timeout or an unexpected execution error")
        check(hidden_su or (event["stdout_complete"] and event["stderr_complete"]),
              "Both streams completed, or su was absent for the unauthorized app")
        if args.expect == "granted":
            check(event["su_exit"] == 0 and "uid=0(root)" in event["stdout"],
                  "The authorized app received root")
            check("context=u:r:ksu:s0" in event["stdout"],
                  "The root child entered the expected SELinux domain")
        else:
            check((hidden_su or event["su_exit"] not in (0, None)) and
                  "uid=0(root)" not in event["stdout"],
                  "The unauthorized app was denied root")
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        report["finished_unix"] = time.time()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"expected": args.expect, "completed": report["completed"],
                      "checks": len(report["checks"]), "error": report.get("error")}))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
