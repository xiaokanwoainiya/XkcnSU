#!/usr/bin/env python3
"""Verify final APK identity, signer, and embedded native daemon inputs."""
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile


def main() -> None:
    apk = Path(sys.argv[1]).resolve()
    out = Path(os.environ.get("XKCNSU_OUT", Path(__file__).resolve().parents[1] / "out"))
    certificate = json.loads((out / "private/certificate.json").read_text())
    if os.environ.get("XKCNSU_REQUIRE_RELEASE_SIGNING") == "1" and (
            certificate.get("signing_profile") != "release" or
            certificate.get("development_only") is not False):
        raise SystemExit("Final APK blocked: verified release signing is required")
    root = Path(os.environ.get("XKCNSU_ROOT", Path(__file__).resolve().parents[1]))
    release = configparser.ConfigParser(interpolation=None)
    release.read_string("[release]\n" + (root / "version.properties").read_text())
    version_name = release["release"]["XKCNSU_VERSION_NAME"]
    version_code = release["release"].getint(
        "XKCNSU_MANAGER_VERSION_CODE", fallback=release["release"].getint("XKCNSU_VERSION_CODE"))
    verifier = (root / "kernel/manager/apk_sign.c").read_text()
    limit = re.search(r"#define\s+CERT_MAX_LENGTH\s+(\d+)", verifier)
    if limit and certificate["certificate_size"] > int(limit.group(1)):
        raise SystemExit("Signing certificate exceeds the kernel APK verifier limit")
    module_command = [sys.executable, str(root / "tools/build-verify-modules.py")]
    if os.environ.get("XKCNSU_REQUIRE_FULL_MATRIX", "1") != "1":
        module_command += ["--kmi", "android14-6.1", "--kmi", "android12-5.10"]
    modules = subprocess.run(module_command, check=False)
    if modules.returncode:
        raise SystemExit("Final APK blocked: the KMI module set is incomplete or uses stale certificates")
    embedded_assets = json.loads((out / "manifests/embedded-assets-aarch64.json").read_text())
    by_name = {item["name"]: item for item in embedded_assets}
    for name, path in (
        ("ksuinit", out / "ksuinit/arm64-v8a/ksuinit"),
        ("kpimg", out / "kpatch/aarch64/kpimg"),
        ("kptools", out / "kpatch/aarch64/kptools"),
    ):
        expected = by_name.get(name)
        if not path.is_file() or expected is None or expected["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise SystemExit(f"Final APK blocked: daemon embedded asset is stale: {name}")
    for module in (out / "aarch64").glob("*.ko"):
        expected = by_name.get(module.name)
        if expected is None or expected["sha256"] != hashlib.sha256(module.read_bytes()).hexdigest():
            raise SystemExit(f"Final APK blocked: daemon embedded asset is stale: {module.name}")
    signing = subprocess.check_output(
        ["apksigner", "verify", "--print-certs", str(apk)], text=True
    )
    match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)", signing)
    if not match or match.group(1).lower() != certificate["KSU_EXPECTED_HASH"]:
        raise SystemExit("APK signing certificate does not match the kernel certificate")
    badging = subprocess.check_output(["aapt2", "dump", "badging", str(apk)], text=True)
    if f"name='{certificate['KSU_PACKAGE_NAME']}'" not in badging.splitlines()[0]:
        raise SystemExit("Unexpected APK applicationId")
    package_line = badging.splitlines()[0]
    if (f"versionCode='{version_code}'" not in package_line or
            f"versionName='{version_name}'" not in package_line):
        raise SystemExit("APK release version does not match version.properties")
    manifest_xml = subprocess.check_output(
        ["aapt2", "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)], text=True
    )
    true_attribute = r"[^\n]*=(?:true|\(type 0x12\)0xffffffff)(?:\s|$)"
    if re.search(r"android:testOnly" + true_attribute, manifest_xml):
        raise SystemExit("Final APK must install normally; testOnly is still enabled")
    if re.search(r"android:debuggable" + true_attribute, manifest_xml):
        raise SystemExit("Final APK must not be debuggable")
    abi_machines = {"arm64-v8a": 183, "x86_64": 62}
    requested_abis = os.environ.get("XKCNSU_ABIS")
    native_daemons = {}
    with zipfile.ZipFile(apk) as archive:
        packaged_abis = {
            name.split("/")[1] for name in archive.namelist()
            if name.startswith("lib/") and name.endswith("/libksud.so")
        }
        if requested_abis:
            requested = set(re.split(r"[,\s]+", requested_abis.strip()))
            if packaged_abis != requested:
                raise SystemExit("APK daemon ABIs differ from the requested build ABIs")
        for abi in sorted(packaged_abis):
            if abi not in abi_machines:
                raise SystemExit(f"Unsupported packaged daemon ABI: {abi}")
            packaged = archive.read(f"lib/{abi}/libksud.so")
            expected = out / f"ksud/{abi}/ksud"
            if not expected.is_file() or packaged != expected.read_bytes():
                raise SystemExit(f"APK libksud.so differs from the source-built release daemon: {abi}")
            if packaged[:4] != b"\x7fELF" or int.from_bytes(packaged[18:20], "little") != abi_machines[abi]:
                raise SystemExit(f"Embedded ksud has the wrong ELF machine for {abi}")
            for library in ("libkernelsu.so", "libadbroot.so"):
                name = f"lib/{abi}/{library}"
                if name not in archive.namelist():
                    raise SystemExit(f"APK is missing a native library: {name}")
                data = archive.read(name)
                if data[:4] != b"\x7fELF" or int.from_bytes(data[18:20], "little") != abi_machines[abi]:
                    raise SystemExit(f"APK native library has the wrong ELF machine: {name}")
            native_daemons[abi] = hashlib.sha256(packaged).hexdigest()
        daemon = archive.read("lib/arm64-v8a/libksud.so")
        if daemon[:4] != b"\x7fELF" or int.from_bytes(daemon[18:20], "little") != 183:
            raise SystemExit("Embedded ksud is not an AArch64 ELF")
    manifest = {
        "apk": str(apk),
        "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        "size": apk.stat().st_size,
        "package_name": certificate["KSU_PACKAGE_NAME"],
        "version_name": version_name,
        "version_code": version_code,
        "certificate_sha256": match.group(1).lower(),
        "ksud_sha256": hashlib.sha256(daemon).hexdigest(),
        "native_daemons": native_daemons,
    }
    if certificate.get("signing_profile") == "release":
        manifest.update(signing_profile="release", development_only=False)
    output = out / "manifests/final-apk.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
