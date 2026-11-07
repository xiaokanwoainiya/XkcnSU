#!/usr/bin/env python3
"""Run explicit XKCNSU acceptance phases and preserve the actual device output."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--ksud", default="/data/adb/ksud")
    parser.add_argument("--phase", choices=("wait-boot", "root", "susfs", "kpm", "final"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--kpm-expected", choices=("off", "on"), default="off")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    report = {"serial": args.serial, "phase": args.phase, "completed": False,
              "commands": [], "checks": [], "started_unix": time.time()}

    def command(arguments, root=True, timeout=45):
        text = shlex.join(arguments)
        if root:
            text = "su -c " + shlex.quote(text)
        result = subprocess.run(["adb", "-s", args.serial, "shell", text],
                                capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout)
        report["commands"].append({"arguments": arguments, "root": root,
                                   "exit_code": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(f"{arguments}: {result.stderr or result.stdout}")
        return result.stdout.strip()

    def check(condition, description):
        report["checks"].append({"description": description, "passed": bool(condition)})
        if not condition:
            raise RuntimeError(description)

    def daemon(*arguments):
        return command([args.ksud, *arguments], timeout=120)

    def status():
        result = json.loads(daemon("kpm", "status"))
        check(result.get("ok") is True, "KPM status has a successful envelope")
        return result["data"]

    try:
        if args.phase == "wait-boot":
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                try:
                    result = subprocess.run(
                        ["adb", "-s", args.serial, "shell", "getprop", "sys.boot_completed"],
                        capture_output=True, text=True, timeout=10)
                except subprocess.TimeoutExpired:
                    time.sleep(3)
                    continue
                if result.returncode == 0 and result.stdout.strip() == "1":
                    report["completed"] = True
                    report["checks"].append({"description": "Android boot completed", "passed": True})
                    return
                time.sleep(3)
            raise RuntimeError("Device did not complete Android boot within 240 seconds")
        check("uid=0(root)" in command(["id"]), "root shell works")
        check(command(["getprop", "sys.boot_completed"]) == "1", "Android boot completed")
        check(daemon("boot-info", "current-kmi") == "android14-6.1", "device KMI matches tested target")
        provider = dict(line.split(": ", 1) for line in daemon("debug", "info").splitlines()
                        if ": " in line)
        check(int(provider.get("version", "0")) > 0, "ready root provider responds to GET_INFO")
        check(int(provider.get("uapi_version", "0")) > 0, "root provider reports its UAPI version")
        check("io.xkcnsu.manager" in command(["pm", "path", "io.xkcnsu.manager"]),
              "XKCNSU manager is installed")
        module_text = daemon("module", "list")
        check(isinstance(json.loads(module_text), list), "ordinary module list remains readable")
        daemon("feature", "list")

        if args.phase in ("root", "final"):
            kpm = status()
            expected = args.kpm_expected == "on"
            check(kpm["configured_enabled"] is expected and kpm["active"] is expected,
                  "KPM configuration and running engine match the expected " + args.kpm_expected + " state")
            check(kpm["reboot_required"] is False, "no KPM transition is pending")
            susfs = json.loads(daemon("susfs", "status"))
            check(susfs["autoload"] is False, "SUSFS autoload default remains disabled")
        elif args.phase == "susfs":
            check(args.execute, "SUSFS phase requires --execute")
            daemon("susfs", "load")
            susfs = json.loads(daemon("susfs", "status"))
            check(susfs["supported"] and susfs["loaded"] and not susfs["builtin"],
                  "independent SUSFS guard loaded and responds")
            check(susfs["autoload"] is False, "manual loading does not enable autoload")
            command(["stat", "/system/bin/sh"])
            command(["uname", "-r"])
            daemon("susfs", "unload")
            susfs = json.loads(daemon("susfs", "status"))
            check(not susfs["loaded"] and not susfs["builtin"], "SUSFS guard unload was verified")
            check("uid=0(root)" in command(["id"]), "root remains usable after guard unload")
        elif args.phase == "kpm":
            kpm = status()
            check(kpm["supported"] and kpm["configured_enabled"] and kpm["active"],
                  "the managed KPM engine is active")
            check(not kpm["reboot_required"], "KPM activation reboot is complete")
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        report["finished_unix"] = time.time()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        print(json.dumps({"phase": args.phase, "completed": report["completed"],
                          "passed_checks": sum(row["passed"] for row in report["checks"]),
                          "error": report.get("error")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
