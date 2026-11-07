#!/usr/bin/env python3
"""Validate root ramdisk generation and a KPM kernel patch/restore round trip."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def linux_path(path):
    path = Path(path).resolve()
    if os.name != "nt":
        return str(path)
    return "/mnt/" + path.drive[0].lower() + "/" + path.as_posix()[3:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--kmi", default="android14-6.1")
    args = parser.parse_args()
    root = args.root.resolve()
    stock = root / "out/stock/OPD2404_16.0.5.1201"
    images = root / "out/images"
    output = root / "out/offline-validation"
    images.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    report = {"completed": False, "device_writes": False, "commands": []}
    daemon = root / "out/tools/ksud"
    prefix = ["wsl.exe", "-d", "Ubuntu", "--exec"] if os.name == "nt" else []

    def run(*arguments):
        command = prefix + [linux_path(daemon)] + [
            linux_path(arg) if isinstance(arg, Path) else arg for arg in arguments
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=180)
        report["commands"].append({"arguments": [str(a) for a in arguments],
                                   "exit_code": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or f"exit {result.returncode}")

    try:
        run("boot-patch", "--boot", stock / "init_boot.img", "--kmi", args.kmi,
            "--allow-shell", "--out", images, "--out-name", "XKCNSU-init_boot.img")
        spec = importlib.util.spec_from_file_location("xkcnsu_extract_ota", root / "tools/extract-ota.py")
        ota = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ota)
        before = ota.analyze_image(stock / "init_boot.img")
        after = ota.analyze_image(images / "XKCNSU-init_boot.img")
        original_entries = {entry["path"]: entry for entry in before["ramdisk"]["entries"]}
        new_entries = {entry["path"]: entry for entry in after["ramdisk"]["entries"]}
        if new_entries["init.real"]["sha256"] != original_entries["init"]["sha256"]:
            raise RuntimeError("root image did not preserve the original Android init")
        expected = {
            "init": root / "out/ksuinit/arm64-v8a/ksuinit",
            "kernelsu.ko": root / f"out/aarch64/{args.kmi}_kernelsu.ko",
        }
        for name, path in expected.items():
            if new_entries[name]["sha256"] != sha(path.read_bytes()):
                raise RuntimeError(f"root image contains stale {name}")
        image = (images / "XKCNSU-init_boot.img").read_bytes()
        ramdisk_size = struct.unpack_from("<I", image, 12)[0]
        ramdisk, _ = ota.decompress(image[4096:4096 + ramdisk_size])
        entries = ota.cpio_entries(ramdisk)
        config_entry = next(entry for entry in entries if entry["path"] == "ksu_config")
        expected_config = b"allow_shell=1 bundled=1"
        if config_entry["sha256"] != sha(expected_config):
            raise RuntimeError("candidate boot parameters differ from allow_shell=1 bundled=1")
        report["root_image"] = {
            "sha256": sha(image), "bytes": len(image),
            "preserved_original_init": True,
            "native_assets_match": True,
            "ksu_config_entry": config_entry,
            "analysis": after,
        }
        patched = images / "XKCNSU-KPM-boot.img"
        original_kernel = output / "kernel.original"
        run("kpm-image", "patch", "--boot", stock / "boot.img", "--out", patched,
            "--kernel-backup", original_kernel, "--kptools", root / "out/tools/kptools",
            "--kpimg", root / "out/kpatch/aarch64/kpimg")
        restored = images / "XKCNSU-KPM-restored-boot.img"
        run("kpm-image", "restore", "--boot", patched, "--kernel", original_kernel,
            "--out", restored)
        raw = (stock / "boot.img").read_bytes()
        restored_bytes = restored.read_bytes()

        def kernel(data):
            size = struct.unpack_from("<I", data, 8)[0]
            return data[4096:4096 + size]

        if kernel(raw) != kernel(restored_bytes):
            raise RuntimeError("KPM removal changed the original kernel")
        report["kpm_roundtrip"] = {
            "kernel_identical": True,
            "whole_image_identical": raw == restored_bytes,
            "original_kernel_sha256": sha(kernel(raw)),
            "restored_kernel_sha256": sha(kernel(restored_bytes)),
            "patched_image_sha256": sha(patched.read_bytes()),
        }
        report["completed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        (output / "image-validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items()
                          if key not in {"commands", "root_image"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
