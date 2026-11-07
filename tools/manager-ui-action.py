#!/usr/bin/env python3
"""Perform one visible, exact-text or accessibility-label tap for local acceptance."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--text")
    selector.add_argument("--description")
    selector.add_argument("--class-name")
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()

    def adb(*arguments):
        result = subprocess.run(["adb", "-s", args.serial, *arguments],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    key, value = ("text", args.text) if args.text is not None else (
        ("content-desc", args.description) if args.description is not None
        else ("class", args.class_name)
    )
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)
    matches = []
    for attempt in range(3):
        remote = "/data/local/tmp/xkcnsu-ui-action-" + uuid.uuid4().hex + ".xml"
        adb("shell", "uiautomator", "dump", remote)
        adb("pull", remote, str(args.snapshot))
        matches = [node for node in ET.parse(args.snapshot).iter("node")
                   if node.get(key) == value and node.get("enabled") == "true"]
        if matches:
            break
        time.sleep(0.5)
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one enabled {key}={value}; found {len(matches)}")
    rectangle = list(map(int, re.findall(r"\d+", matches[0].get("bounds", ""))))
    if len(rectangle) != 4:
        raise RuntimeError("Invalid bounds")
    left, top, right, bottom = rectangle
    adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    print(json.dumps({"tapped": True, "selector": key, "value": value}, ensure_ascii=True))


if __name__ == "__main__":
    main()
