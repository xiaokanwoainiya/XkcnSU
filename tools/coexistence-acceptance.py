#!/usr/bin/env python3
"""Explicit reboot-separated acceptance for the inert echo KPM and SUSFS."""

import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time

ECHO = "940d22509b5cb6acadc5f4fb7a208a152d14cdc5eacd0eb5b597ae6a6b481638"
MARKER = "XKCNSU-autoload-acceptance"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--ksud", default="/data/adb/ksud")
    parser.add_argument("--phase", choices=("prepare", "after-boot", "after-disabled"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = {"phase": args.phase, "serial": args.serial, "completed": False,
              "commands": [], "checks": [], "started_unix": time.time()}

    def check(value, description):
        report["checks"].append({"description": description, "passed": bool(value)})
        if not value:
            raise RuntimeError(description)

    def root(*arguments, expected=0):
        read_only = arguments[0] in ("id", "getprop", "cat") or (
            arguments[0] == args.ksud and arguments[1:3] in (
                ("kpm", "status"), ("kpm", "info"), ("susfs", "status"),
            )
        )
        for attempt in range(3):
            result = subprocess.run(
                ["adb", "-s", args.serial, "shell",
                 shlex.join(["su", "-c", shlex.join(arguments)])],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            report["commands"].append({"arguments": arguments, "exit_code": result.returncode,
                                       "stdout": result.stdout, "stderr": result.stderr})
            if not (read_only and attempt < 2 and "adb" in result.stderr and
                    any(text in result.stderr for text in ("not found", "offline", "closed"))):
                break
            time.sleep(1)
        if result.returncode != expected:
            raise RuntimeError("Command failed: " + shlex.join(arguments))
        return result.stdout.strip()

    def kpm(*arguments, error=None):
        value = json.loads(root(args.ksud, "kpm", *arguments, expected=1 if error else 0))
        check(value["ok"] is (error is None), "KPM envelope matches process status")
        if error:
            check(value["error"]["code"] == error, "KPM reports expected " + error)
        return value["data"]

    def echo():
        value = kpm("info", ECHO)
        check(value["name"] == "xkcnsu-test-echo", "Only the inert acceptance KPM is selected")
        return value

    def susfs():
        return json.loads(root(args.ksud, "susfs", "status"))

    def save_state(state):
        args.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.state.with_suffix(".part")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(args.state)

    try:
        check(args.execute, "Explicit execution is required")
        check("uid=0(root)" in root("id"), "Root is available")
        check(root("getprop", "sys.boot_completed") == "1", "Android boot completed")
        boot = root("cat", "/proc/sys/kernel/random/boot_id")
        status = kpm("status")
        check(status["active"] and status["configured_enabled"] and not status["reboot_required"],
              "Managed KPM integration is active without a pending transition")
        if args.phase == "prepare":
            check(not args.state.exists(), "Prepare uses a fresh acceptance state")
            module = echo()
            check(not module["loaded"] and not module["pending_unload"],
                  "Earlier resident fixtures have cleared after reboot")
            kpm("load", ECHO, "--args", MARKER)
            check(kpm("control", ECHO, "--args", MARKER)["output"] == "echo: " + MARKER,
                  "KPM control succeeds with SUSFS loaded")
            kpm("autoload", ECHO, "enable")
            root(args.ksud, "susfs", "autoload", "enable")
            guard = susfs()
            check(guard["loaded"] and guard["supported"] and guard["autoload"],
                  "SUSFS is active and configured for autoload")
            save_state({"serial": args.serial, "phase": "prepared", "boot": boot})
        else:
            state = json.loads(args.state.read_text(encoding="utf-8"))
            check(state["serial"] == args.serial and boot != state["boot"],
                  "Acceptance continued on the same device after a kernel reboot")
            module, guard = echo(), susfs()
            if args.phase == "after-boot":
                check(state["phase"] == "prepared", "Autoload preparation was verified")
                check(module["loaded"] and module["autoload"] and module["args"] == MARKER,
                      "Echo KPM autoloaded with its saved literal arguments")
                check(guard["loaded"] and guard["supported"] and guard["autoload"],
                      "SUSFS autoloaded beside the managed KPM engine")
                check(root("cat", "/sys/module/susfs_guard_lkm/parameters/kpm_conflict") == "1",
                      "SUSFS reports the KPM conflict ownership mode")
                probe = json.loads(root("/data/local/tmp/xkcnsu-susfs-runtime-check"))
                check(probe["completed"] and probe["root_still_visible"] and probe["app_hidden_after_rule"],
                      "SUSFS hides its temporary probe while root still sees it")
                check(kpm("control", ECHO, "--args", "coexist")["output"] == "echo: coexist",
                      "KPM remains callable after the SUSFS runtime check")
                kpm("autoload", ECHO, "disable")
                root(args.ksud, "susfs", "autoload", "disable")
                kpm("unload", ECHO, error="reboot_required")
                check(echo()["pending_unload"], "Unloaded echo code stays resident until reboot")
                save_state({"serial": args.serial, "phase": "disabled", "boot": boot})
            else:
                check(state["phase"] == "disabled", "Both autoload settings were explicitly disabled")
                check(not module["loaded"] and not module["autoload"] and not module["pending_unload"],
                      "Echo KPM is absent after reboot and autoload remains off")
                check(not guard["loaded"] and not guard["autoload"],
                      "SUSFS is absent after reboot and autoload remains off")
                save_state({"serial": args.serial, "phase": "completed", "boot": boot})
        check(root("cat", "/proc/sys/kernel/random/boot_id") == boot,
              "Acceptance commands did not reboot or crash the kernel")
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        report["finished_unix"] = time.time()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": args.phase, "completed": report["completed"],
                      "checks": len(report["checks"]), "error": report.get("error")}))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
