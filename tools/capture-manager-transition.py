#!/usr/bin/env python3
"""Record a bounded Manager launch or tab transition for local visual acceptance."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--phase", choices=("startup", "kpm", "settings"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"completed": False, "phase": args.phase, "commands": []}

    def adb(*arguments):
        result = subprocess.run(["adb", "-s", args.serial, *arguments],
                                capture_output=True, text=True, timeout=20)
        report["commands"].append({"arguments": arguments, "exit_code": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    recorder = None
    try:
        size = adb("shell", "wm", "size")
        match = re.search(r"(?:Override|Physical) size: (\d+)x(\d+)", size)
        if not match:
            raise RuntimeError("Cannot determine display dimensions")
        width, height = map(int, match.groups())
        package = "io.xkcnsu.manager"
        if args.phase == "startup":
            adb("shell", "am", "force-stop", package)
        else:
            start = 0.1 if args.phase == "kpm" else 0.7
            adb("shell", "input", "tap", str(int(width * start)), str(int(height * 0.95)))
            time.sleep(0.8)
        remote = "/data/local/tmp/xkcnsu-transition-" + uuid.uuid4().hex + ".mp4"
        recorder = subprocess.Popen(
            ["adb", "-s", args.serial, "shell", shlex.join(["su", "-c", shlex.join([
                "screenrecord", "--size", "640x904", "--bit-rate", "1000000",
                "--time-limit", "4", remote,
            ])])],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.4)
        if args.phase == "startup":
            adb("shell", "am", "start", "-n", package + "/me.weishu.kernelsu.ui.MainActivity")
        else:
            target = 0.3 if args.phase == "kpm" else 0.9
            adb("shell", "input", "tap", str(int(width * target)), str(int(height * 0.95)))
        stdout, stderr = recorder.communicate(timeout=12)
        if recorder.returncode:
            raise RuntimeError((stderr or stdout).decode("utf-8", "replace"))
        adb("shell", shlex.join(["su", "-c", shlex.join(["chmod", "644", remote])]))
        adb("pull", remote, str(args.output / "transition.mp4"))
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    finally:
        if recorder is not None and recorder.poll() is None:
            recorder.terminate()
            recorder.wait(timeout=5)
        (args.output / "capture.json").write_text(json.dumps(report, indent=2) + "\n",
                                                 encoding="utf-8")
    print(json.dumps({"completed": report["completed"], "phase": args.phase,
                      "error": report.get("error")}))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
