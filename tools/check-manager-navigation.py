#!/usr/bin/env python3
"""Check the visible Manager navigation, settings order and empty-state placement."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--mode", choices=("off", "on", "empty", "settings"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = {"mode": args.mode, "completed": False, "checks": []}

    def adb(*arguments):
        result = subprocess.run(
            ["adb", "-s", args.serial, *arguments],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout

    def check(passed, description):
        report["checks"].append({"description": description, "passed": bool(passed)})
        if not passed:
            raise RuntimeError(description)

    def bounds(node):
        return tuple(map(int, re.findall(r"\d+", node.get("bounds", ""))))

    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        remote = "/data/local/tmp/xkcnsu-navigation-" + uuid.uuid4().hex + ".xml"
        adb("shell", "uiautomator", "dump", remote)
        xml = args.report.with_suffix(".xml")
        adb("pull", remote, str(xml))
        nodes = list(ET.parse(xml).iter("node"))
        app = [node for node in nodes if node.get("package") == "io.xkcnsu.manager"]
        check(bool(app), "XKCNSU Manager is visible")
        width = max(bounds(node)[2] for node in app)
        height = max(bounds(node)[3] for node in app)
        labels = {"主页", "KPM", "超级用户", "模块", "设置",
                  "Home", "Superuser", "Modules", "Module", "Settings"}
        nav = sorted(
            [node for node in app if node.get("text") in labels
             and bounds(node)[1] >= height * 0.85],
            key=lambda node: bounds(node)[0],
        )
        nav_text = [node.get("text") for node in nav]
        report["navigation"] = nav_text
        check(len(nav) in (4, 5), "The bottom bar has four or five destinations")
        if args.mode in ("off", "on", "empty"):
            enabled = args.mode != "off"
            check(len(nav) == (5 if enabled else 4),
                  "Destination count follows the KPM integration setting")
            check(("KPM" in nav_text) == enabled, "KPM visibility follows integration")
            if enabled:
                check(nav_text.index("KPM") == 1, "KPM is between Home and Superuser")
        texts = [node for node in app if node.get("text")]
        if args.mode == "empty":
            matches = [node for node in texts if node.get("text") in
                       ("未发现模块", "No modules found")]
            check(len(matches) == 1, "The empty page has the requested single message")
            left, top, right, bottom = bounds(matches[0])
            report["empty_center"] = [(left + right) / (2 * width),
                                      (top + bottom) / (2 * height)]
            check(abs(report["empty_center"][0] - 0.5) < 0.04
                  and abs(report["empty_center"][1] - 0.5) < 0.08,
                  "The empty message is centered in the screen")
            above = [node.get("text") for node in texts if bounds(node)[1] < height * 0.3]
            check(not any(text in ("KPM", "关闭", "Off", "尚未导入 KPM") for text in above),
                  "The page has no integration status or redundant empty-state label at the top")
            check(any(text in ("KPM 模块", "KPM Modules") for text in above),
                  "The KPM page has a page title matching the other main pages")
            check(any(node.get("content-desc") in ("导入 .kpm", "Import .kpm") for node in app),
                  "The import action is available")
        elif args.mode == "settings":
            def row(*names):
                found = [node for node in texts if node.get("text") in names]
                check(bool(found), "The settings row is visible: " + names[0])
                return bounds(found[0])[1]
            kpm = row("KPM 集成", "KPM integration")
            susfs = row("SUSFS 集成", "SUSFS integration")
            selinux = row("隐藏 SELinux 修改", "隐藏SELinux修改", "Hide SELinux modification")
            sulog = row("SU 日志", "SU日志", "SU Log", "SU log")
            check(kpm < selinux and kpm < sulog and susfs < selinux and susfs < sulog,
                  "KPM and SUSFS integration are above SELinux Hide and SU Log")
            check(not any(node.get("text") in ("SUSFS", "开机加载 SUSFS", "Load SUSFS at boot") for node in texts),
                  "SUSFS uses one integration switch without a separate autoload row")
            check(not any(node.get("text") in ("完整重启设备", "Reboot device") for node in texts),
                  "There is no dedicated full-device reboot row")
            check(not any("KPM 集成与 SUSFS" in node.get("text", "")
                          or "默认关闭。更改时会更新" in node.get("text", "")
                          for node in texts),
                  "No separate system entry or explanatory paragraph is present")
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
