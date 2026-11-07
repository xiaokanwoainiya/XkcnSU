#!/usr/bin/env python3
"""Package selected binaries and public conclusions, never local device evidence."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile


MODULE_NAME = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+_(kernelsu|susfs_guard_lkm)\.ko")
SHA256 = re.compile(r"[0-9a-f]{64}")
PRIVATE_KEY = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|PuTTY-User-" rb"Key-File:", re.I)
PRIVATE_SUFFIXES = {".p12", ".pfx", ".jks", ".keystore", ".key", ".pem", ".p8",
                    ".pk8", ".pkcs12"}
EXCLUDED_DIRECTORIES = {
    "out", ".git", "target", "dist", "node_modules", "__pycache__", ".gradle",
    ".idea", ".vscode", ".venv", ".cxx", ".kotlin", "build", "private",
    "secrets", "credentials", "build-logs", "test-harness", "test-modules",
    "device-backup", "device-acceptance", "diagnostics", "offline-validation",
}
LOCAL_SOURCE_FILES = {"temp.gz", "tools/device-backup.py", "tools/extract-ota.py"}
SOURCE_ARCHIVES = {".img", ".kernel", ".dump", ".log", ".zip", ".7z", ".tar",
                   ".gz", ".xz", ".bz2", ".bundle", ".p12", ".pfx", ".part", ".tmp",
                   ".dmp", ".pcap", ".pcapng", ".hprof", ".o", ".obj", ".elf", ".ko"}
SOURCE_LINKS = {
    "kernel/include/uapi": "../../uapi",
    "manager/app/src/main/cpp/uapi": "../../../../../uapi",
}
REQUIRED_SOURCES = {
    "LICENSE", "README.md", "Cargo.toml", "Cargo.lock", "version.properties", "kernel/LICENSE",
    "kernel/XKCNSU_HOOK_PROVENANCE.json", "kernel/susfs/XKCNSU_PROVENANCE.json",
    "vendor/kpatch-next/LICENSE", "vendor/kpatch-next/XKCNSU_PROVENANCE.md",
    "vendor/kpatch-next/kernel/Makefile", "vendor/kpatch-next/tools/CMakeLists.txt",
    "tools/build-all.sh", "tools/build-common.sh", "tools/build-release.sh",
    "tools/package-release.py",
    "manager/build.gradle.kts", "manager/gradlew",
    "manager/gradle/wrapper/gradle-wrapper.jar", "uapi/ksu.h",
} | SOURCE_LINKS.keys()
SOURCE_GENERATED = {"BUILDING-SOURCE.md", "source-manifest.json", "SHA256SUMS"}
REBUILD_NOTES = b"""# Rebuilding the public source snapshot

The ZIP contains the current source tree, upstream licenses and provenance,
including the vendored KPatch sources. Extract it with a tool that preserves
Unix symlinks and executable modes, such as `unzip` on Linux or WSL.

The build scripts require a local Git repository for snapshots and version
queries. In the extracted XKCNSU directory, create a new public snapshot:

```sh
git init
git add .
git -c user.name=XKCNSU -c user.email=build@example.invalid commit -m "Public source snapshot"
export XKCNSU_ROOT="$PWD"
export XKCNSU_SOURCE_ROOT="$PWD"
export SOURCE_DATE_EPOCH=0
bash tools/bootstrap-xkcnsu.sh all
bash tools/build-all.sh --source-ready
```

Use Ubuntu/WSL with Git, network access, Docker and permissions to provision
the toolchains. Build caches default to /opt/xkcnsu. The tool scripts pin the
toolchain versions and fetch external build dependencies; this is not an
offline dependency mirror.

Original Git history, signing keys, signing passwords and device partitions
are excluded. The build generates a new local signing identity and reads the
release version from version.properties. It therefore rebuilds the source,
not the original signed APK bit for bit. Bit-for-bit rebuilding was not tested.
Keep the new APK and its matching root modules together.

Verify source files with `sha256sum -c SHA256SUMS` inside the extracted XKCNSU
directory. Checksums cover regular files; symlink targets and Unix modes are
recorded in source-manifest.json. The artifact ZIP has its own SHA256SUMS.
Test conclusions record only the supplied reports; host checks do not
establish device compatibility.
"""
ACCEPTANCE_NAMES = ("root", "susfs", "kpm", "final", "kpm-modules")
HOST_RESULT_FIELDS = {
    "baselib": ("groups_passed", "loader_helpers"),
    "loader": ("groups_passed", "text_boundary_cases"),
    "supercall": ("groups_passed", "string_groups", "load_buffer_groups"),
    "relocation": ("groups_passed",),
    "native": ("tests_passed", "tests_failed", "tests_ignored", "tests_filtered",
               "test_targets"),
}
SAFE_RESULTS = {"passed", "failed", "success", "error", "blocked", "incomplete",
                "not_run", "not-run", "skipped", "pending"}
BOOL_RESULTS = {
    "completed", "passed", "ok", "success", "verified", "device_writes",
    "baseline_app_open", "root_still_visible", "app_hidden_after_rule",
    "guard_unloaded_after_test", "autoload_enabled",
}
COUNT_RESULTS = {"passed_checks", "rule_errno", "groups_passed", "groups_failed",
                 "production_text_boundary_cases", "supercall_groups_passed",
                 "supercall_groups_failed", "loader_groups_passed",
                 "loader_text_boundary_assertions", "warnings_as_errors"}
DELIVERY_NAMES = {"XKCNSU-source.zip", "XKCNSU-build-and-images.zip", "delivery.json"}


class PackagingError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise PackagingError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value):
    value = str(value)
    require(not any(char in value for char in "\0\r\n"), "Unsafe package input path")
    path = PurePosixPath(value.replace("\\", "/"))
    require(not path.is_absolute() and path.parts and
            all(part not in {"", ".", ".."} and ":" not in part for part in path.parts),
            "Unsafe package input path")
    return path


def input_path(root, relative, source_link=False):
    relative = relative_path(relative)
    path = root
    for index, part in enumerate(relative.parts):
        path = path / part
        require((not path.is_symlink() or (source_link and index == len(relative.parts) - 1))
                and not (
            hasattr(path, "is_junction") and path.is_junction()),
            "Package input uses a link: " + relative.as_posix())
    require(path.resolve().is_relative_to(root), "Package input escapes its root")
    require(path.is_file() or (source_link and path.is_symlink()),
            "Required package input is missing: " + relative.as_posix())
    return path


def read_input(root, relative, limit=128 * 1024 * 1024):
    path = input_path(root, relative)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(before.st_size <= limit, "Package input exceeds its size limit")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and len(data) == before.st_size, "Package input changed during capture")
    return data


def read_json(root, relative):
    data = read_input(root, relative, 4 * 1024 * 1024)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise PackagingError("Invalid JSON input: " + str(relative)) from error


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode()


def private_path(relative):
    path = relative_path(relative)
    lowered = [part.lower() for part in path.parts]
    name = lowered[-1]
    return (any(part in EXCLUDED_DIRECTORIES for part in lowered)
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or name == ".env" or name.startswith(".env.")
            or name in {"keystore-password", "repack-config.json", "id_rsa", "id_ed25519",
                        "key.properties", "keystore.properties", "local.properties"}
            or name.startswith(("credentials.", "password.", "secret.")))


def source_allowed(relative):
    path = relative_path(relative)
    return (not private_path(path) and path.as_posix().lower() not in LOCAL_SOURCE_FILES
            and path.as_posix() not in SOURCE_GENERATED
            and path.suffix.lower() not in SOURCE_ARCHIVES)


def device_identifiers(value):
    """Only use private values as exclusion tokens; never publish them."""
    tokens = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if (key.lower() in {"serial", "device_serial", "fingerprint", "android_id",
                                "imei", "imsi", "password", "keystore_pass", "key_pass"}
                    and isinstance(child, str) and len(child) >= 6):
                tokens.add(child.encode())
            tokens.update(device_identifiers(child))
    elif isinstance(value, list):
        for child in value:
            tokens.update(device_identifiers(child))
    return tokens


def safe_content(data, identifiers):
    return not PRIVATE_KEY.search(data) and not any(token in data for token in identifiers)


def public_results(report):
    """Project known typed results; no descriptions, commands, errors or device fields."""
    require(isinstance(report, dict), "Test report must be an object")
    result = {}
    for key in BOOL_RESULTS:
        if key in report:
            require(type(report[key]) is bool, "Test result must be boolean: " + key)
            result[key] = report[key]
    for key in COUNT_RESULTS:
        if key in report:
            require(type(report[key]) in (int, bool), "Invalid test result count")
            result[key] = report[key]
    for key in ("result", "status"):
        if key in report:
            require(report[key] in SAFE_RESULTS, "Unsupported test result status")
            result[key] = report[key]
    if "error" in report:
        result["error_present"] = bool(report["error"])
    if "checks" in report:
        require(isinstance(report["checks"], list), "Test checks must be a list")
        checks = []
        for index, check in enumerate(report["checks"], 1):
            require(isinstance(check, dict) and type(check.get("passed")) is bool,
                    "A test check has no explicit boolean result")
            checks.append({"index": index, "passed": check["passed"]})
        result["checks"] = checks
    for key in ("test_result", "results"):
        if key in report:
            result[key] = public_results(report[key])
    return result


def host_report_summary(report, source_entries):
    require(isinstance(report, dict) and report.get("schema") == 1 and
            report.get("kind") == "kpatch-host-final", "Invalid final host test report")
    require(report.get("ok") is True and report.get("sources_unchanged") is True
            and not report.get("error"), "Final host tests have not passed")
    steps = report.get("steps")
    require(isinstance(steps, list) and steps and
            all(isinstance(step, dict) and type(step.get("exit_code")) is int
                and step["exit_code"] == 0 for step in steps),
            "Final host test steps have not passed")
    sources = report.get("sources")
    require(isinstance(sources, list) and sources, "Final host source evidence is missing")
    seen = set()
    for item in sources:
        require(isinstance(item, dict), "Invalid final host source evidence")
        relative = relative_path(item.get("path", ""))
        name = relative.as_posix()
        require(source_allowed(relative) and name not in seen,
                "Invalid or duplicate final host source path")
        seen.add(name)
        expected = sha256_field(item, "sha256")
        require(item.get("unchanged") is True and
                sha256_field(item, "sha256_after") == expected,
                "Final host source changed during testing")
        data = source_entries.get("XKCNSU/" + name)
        require(data is not None and type(item.get("bytes")) is int and
                len(data) == item["bytes"] and hashlib.sha256(data).hexdigest() == expected,
                "Final host source differs from the public source snapshot: " + name)
    results = report.get("results")
    require(isinstance(results, dict), "Final host test results are missing")
    summary = {"ok": True, "sources_verified": True, "source_files_verified": len(seen),
               "results": {}}
    for group, fields in HOST_RESULT_FIELDS.items():
        values = results.get(group)
        require(isinstance(values, dict), "Missing final host test group: " + group)
        counts = {}
        for field in fields:
            value = values.get(field)
            require(type(value) is int and value >= 0, "Invalid final host test count")
            counts[field] = value
        require(counts.get("groups_passed", counts.get("tests_passed", 0)) > 0 and
                counts.get("tests_failed", 0) == 0, "Final host test group has not passed")
        summary["results"][group] = counts
    return summary


def loader_relocation_summary(report):
    require(isinstance(report, dict), "Invalid loader relocation report")
    expected = sha256_field(report, "object_sha256")
    pointers = report.get("unadjusted_rodata_pointers")
    require(isinstance(pointers, list), "Invalid loader relocation evidence")
    require(report.get("passed") is True and not pointers and not report.get("error"),
            "Loader relocation validation has not passed")
    return {"passed": True, "object_sha256": expected,
            "unadjusted_rodata_pointer_count": len(pointers)}


def acceptance_passes(name, report):
    if name == "kpm-modules":
        require(report.get("result") == "passed",
                "KPM device module loading acceptance has not passed")
    else:
        require(report.get("completed") is True,
                "Device acceptance is incomplete: " + name + ".json")
    require(report.get("checks"), "Device acceptance has no checks: " + name)
    require(all(check["passed"] for check in report["checks"]),
            "Device acceptance contains a failed check: " + name)
    require(not report.get("error_present") and
            all(report.get(key, True) is not False
                for key in ("passed", "ok", "success", "verified")),
            "Device acceptance reports a failure: " + name)
    for key in ("result", "status"):
        if key in report:
            require(report[key] in {"passed", "success"},
                    "Device acceptance has not passed: " + name)


def source_candidates(root):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    paths = subprocess.check_output(
        ["git", "-c", "core.safecrlf=false", "-C", str(root),
         "--no-optional-locks", "-c", "core.fsmonitor=false", "-c",
         "core.untrackedCache=false", "ls-files", "--stage", "-c", "-o",
         "--exclude-standard", "-z"], env=env).split(b"\0")
    candidates = {}
    for raw in paths:
        if not raw:
            continue
        tracked = re.fullmatch(rb"([0-7]{6}) [0-9a-f]+ ([0-3])\t(.+)", raw, re.S)
        if tracked:
            mode, stage, raw = tracked.groups()
            require(stage == b"0", "Source index contains an unresolved merge")
            candidates[raw.decode("utf-8")] = int(mode, 8)
        else:
            candidates[raw.decode("utf-8")] = None
    return dict(sorted(candidates.items()))


def sha256_field(report, key):
    value = report.get(key)
    require(isinstance(value, str) and SHA256.fullmatch(value), "Invalid digest: " + key)
    return value


def descriptor(name, data):
    return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def add_binary(root, entries, source, name, identifiers, expected=None, elf=False,
               limit=128 * 1024 * 1024):
    data = read_input(root, source, limit)
    require(data and safe_content(data, identifiers), "Unsafe binary package input: " + name)
    if expected is not None:
        require(hashlib.sha256(data).hexdigest() == expected,
                "Binary differs from its verification manifest: " + name)
    if elf:
        require(data[:4] == b"\x7fELF" and len(data) >= 20 and
                int.from_bytes(data[18:20], "little") == 183,
                "Binary is not an AArch64 ELF: " + name)
    require(name not in entries, "Duplicate binary package entry")
    entries[name] = data
    return descriptor(name, data)


def verify_apk_daemon(apk, daemon):
    try:
        with zipfile.ZipFile(io.BytesIO(apk)) as archive:
            names = archive.namelist()
            name = "lib/arm64-v8a/libksud.so"
            require(names.count("AndroidManifest.xml") == names.count(name) == 1,
                    "APK has missing or duplicate manifest/daemon entries")
            require(archive.getinfo(name).file_size == len(daemon) and
                    archive.read(name) == daemon, "APK embedded daemon differs from ksud")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise PackagingError("Invalid APK archive") from error


def add_checksums(entries, name, modes=None):
    entries[name] = "".join(
        hashlib.sha256(data).hexdigest() + "  " + path.removeprefix("XKCNSU/") + "\n"
        for path, data in sorted(entries.items())
        if ((modes or {}).get(path, 0o100644) & 0o170000) != 0o120000).encode()


def image_command(report, prefix):
    commands = report.get("commands")
    require(isinstance(commands, list), "Image validation commands are missing")
    matches = [item for item in commands if isinstance(item, dict) and
               isinstance(item.get("arguments"), list) and
               item["arguments"][:len(prefix)] == list(prefix)]
    require(len(matches) == 1 and type(matches[0].get("exit_code")) is int and
            matches[0]["exit_code"] == 0, "Stock image generation has not passed")
    return matches[0]["arguments"]


def image_option(arguments, name):
    require(arguments.count(name) == 1, "Missing or duplicate image validation option")
    index = arguments.index(name) + 1
    require(index < len(arguments) and isinstance(arguments[index], str) and
            not arguments[index].startswith("--"), "Invalid image validation option")
    return arguments[index]


def offline_image_summary(report, entries):
    require(isinstance(report, dict) and report.get("completed") is True
            and report.get("device_writes") is False
            and not report.get("error"), "Offline image validation has not passed")
    root_report = report.get("root_image", {})
    roundtrip = report.get("kpm_roundtrip", {})
    require(isinstance(root_report, dict) and isinstance(roundtrip, dict) and
            root_report.get("preserved_original_init") is True and
            root_report.get("native_assets_match") is True and
            roundtrip.get("kernel_identical") is True and
            roundtrip.get("whole_image_identical") is True,
            "Stock image validation or restoration has not passed")
    root_command = image_command(report, ("boot-patch",))
    image_command(report, ("kpm-image", "patch"))
    image_command(report, ("kpm-image", "restore"))
    kmi = image_option(root_command, "--kmi")
    root_module = kmi + "_kernelsu.ko"
    require(MODULE_NAME.fullmatch(root_module), "Invalid validated root image KMI")
    ramdisk = root_report.get("analysis", {}).get("ramdisk", {}).get("entries")
    require(isinstance(ramdisk, list), "Root image native asset evidence is missing")
    for name, binary in (("init", "ksuinit/arm64-v8a/ksuinit"),
                         ("kernelsu.ko", "aarch64/" + root_module)):
        matches = [item for item in ramdisk if isinstance(item, dict) and
                   item.get("path") == name]
        require(len(matches) == 1 and binary in entries and
                sha256_field(matches[0], "sha256") ==
                hashlib.sha256(entries[binary]).hexdigest(),
                "Root image contains stale native assets")
    require(sha256_field(roundtrip, "original_kernel_sha256") ==
            sha256_field(roundtrip, "restored_kernel_sha256"),
            "Restored stock kernel differs from offline validation")
    summary = public_results(report)
    summary["root_image"] = {
        key: root_report[key] for key in ("preserved_original_init", "native_assets_match")}
    summary["kpm_roundtrip"] = {
        key: roundtrip[key] for key in ("kernel_identical", "whole_image_identical")}
    return summary


def build_plan(root, source_paths=None):
    root = Path(root).resolve(strict=True)
    reports, identifiers = {}, set()
    for name in ACCEPTANCE_NAMES:
        raw = read_json(root, "out/device-acceptance/" + name + ".json")
        identifiers.update(device_identifiers(raw))
        reports[name] = public_results(raw)
        acceptance_passes(name, reports[name])
    host_report = read_json(root, "out/manifests/host-kpatch-final.json")
    identifiers.update(device_identifiers(host_report))
    for password in ("out/private/keystore-password", "out/private/release-password"):
        if (root / password).exists():
            token = read_input(root, password, 4096).strip()
            if len(token) >= 6:
                identifiers.add(token)
    optional = {
        "susfs-runtime": "out/device-acceptance/susfs-runtime.json",
        "installed-artifact-identity": "out/device-acceptance/final-identity.json",
        "manager-navigation": "out/device-acceptance/ui-navigation/summary.json",
        "kpm-integration-recovery": "out/device-acceptance/original-paths/kpm-recovery.json",
        "original-paths": "out/device-acceptance/original-paths/summary.json",
        "root-policy-granted": "out/device-acceptance/root-policy/granted.json",
        "root-policy-persisted": "out/device-acceptance/root-policy/granted-after-reboot.json",
        "root-policy-revoked": "out/device-acceptance/root-policy/denied-after-revoke.json",
        "ordinary-module-install": "out/device-acceptance/base-module-v3/prepare.json",
        "ordinary-module-boot": "out/device-acceptance/base-module-v3/after-boot.json",
        "ordinary-module-cleanup": "out/device-acceptance/base-module-v3/cleanup-verified-retry.json",
        "kpm-susfs-autoload": "out/device-acceptance/coexistence/after-boot-retry.json",
        "kpm-susfs-autoload-disabled": "out/device-acceptance/coexistence/after-disabled.json",
        "kpatch-host-range-boundary": "out/manifests/kpatch-host-range-boundary-tests.json",
        "kpatch-supercall-nofault": "out/manifests/kpatch-supercall-nofault-host-tests.json",
        "kpatch-x18-audit": "out/manifests/kpatch-x18-audit.json",
        "kpatch-loader-relocations": "out/manifests/kpatch-loader-relocations.json",
    }
    for name, relative in optional.items():
        if (root / relative).exists():
            raw = read_json(root, relative)
            identifiers.update(device_identifiers(raw))
            reports[name] = (loader_relocation_summary(raw)
                             if name == "kpatch-loader-relocations" else public_results(raw))
    offline = "out/offline-validation/image-validation.json"
    offline_report = read_json(root, offline)
    identifiers.update(device_identifiers(offline_report))

    final_apk = read_json(root, "out/manifests/final-apk.json")
    apk_sha = sha256_field(final_apk, "sha256")
    daemon_sha = sha256_field(final_apk, "ksud_sha256")
    public_apk = {key: final_apk[key] for key in ("sha256", "size")}
    public_apk["ksud_sha256"] = daemon_sha
    require(type(public_apk["size"]) is int, "Invalid APK byte count")
    if "certificate_sha256" in final_apk:
        public_apk["certificate_sha256"] = sha256_field(final_apk, "certificate_sha256")
    if "package_name" in final_apk:
        package = final_apk["package_name"]
        require(isinstance(package, str) and
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package),
                "Invalid APK package name")
        public_apk["package_name"] = package

    if final_apk.get("signing_profile") == "release":
        require(final_apk.get("development_only") is False, "Invalid release signing metadata")
        apk_name = "XKCNSU.apk"
        public_apk.update(signing_profile="release", development_only=False)
    else:
        require(final_apk.get("signing_profile") in (None, "development"),
                "Unknown APK signing profile")
        apk_name = "XKCNSU-dev.apk"
    entries, binaries = {}, []
    binaries.append(add_binary(root, entries, "out/manager/" + apk_name,
                               apk_name, identifiers, apk_sha))
    require(binaries[-1]["bytes"] == public_apk["size"], "APK byte count differs from manifest")
    binaries.append(add_binary(root, entries, "out/ksud/arm64-v8a/ksud",
                               "ksud/arm64-v8a/ksud", identifiers, daemon_sha, elf=True))
    verify_apk_daemon(entries[apk_name], entries["ksud/arm64-v8a/ksud"])
    modules = read_json(root, "out/manifests/verified-modules.json")
    require(modules.get("errors") == [], "Module verification has not passed")
    embedded = read_json(root, "out/manifests/embedded-assets-aarch64.json")
    require(isinstance(embedded, list), "Embedded-assets manifest must be a list")
    assets = {item["name"]: item for item in embedded}
    require(len(assets) == len(embedded), "Duplicate embedded asset")
    module_names = set()
    for module in modules["artifacts"]:
        name = module["name"]
        require(MODULE_NAME.fullmatch(name) and name not in module_names,
                "Unexpected or duplicate module artifact")
        module_names.add(name)
        expected = sha256_field(module, "sha256")
        require(name in assets and sha256_field(assets[name], "sha256") == expected,
                "Module differs from the daemon embedded-assets manifest")
        item = add_binary(root, entries, "out/aarch64/" + name,
                          "aarch64/" + name, identifiers, expected, elf=True)
        require(item["bytes"] == module["size"] == assets[name]["size"],
                "Module size differs from verification manifest")
        binaries.append(item)
    require(module_names, "No verified modules are available")
    for name in module_names:
        prefix = name.rsplit("_", 1)[0] if name.endswith("_kernelsu.ko") else name[:-len("_susfs_guard_lkm.ko")]
        require(prefix + "_kernelsu.ko" in module_names and
                prefix + "_susfs_guard_lkm.ko" in module_names, "Incomplete KMI module pair")
    for name, source, destination, elf in (
        ("kpimg", "out/kpatch/aarch64/kpimg", "kpatch/aarch64/kpimg", False),
        ("kptools", "out/kpatch/aarch64/kptools", "kpatch/aarch64/kptools", True),
        ("ksuinit", "out/ksuinit/arm64-v8a/ksuinit", "ksuinit/arm64-v8a/ksuinit", True),
    ):
        require(name in assets, "Missing verified engine/init asset: " + name)
        item = add_binary(root, entries, source, destination, identifiers,
                          sha256_field(assets[name], "sha256"), elf=elf)
        require(item["bytes"] == assets[name]["size"], "Embedded asset size mismatch")
        binaries.append(item)
    reports["offline-images"] = offline_image_summary(offline_report, entries)
    images = {"schema": 2, "images_included": False, "development_acceptance": True,
              "original_backups_included": False, "images": []}

    source_entries, source_modes = {}, {}
    candidates = source_paths if source_paths is not None else source_candidates(root)
    for name in candidates:
        relative = relative_path(name)
        if not source_allowed(relative):
            continue
        path = root.joinpath(*relative.parts)
        if not path.is_file() and not path.is_symlink():
            continue
        mode = candidates.get(name) if isinstance(candidates, dict) else None
        if relative.as_posix() in SOURCE_LINKS:
            path = input_path(root, relative, source_link=True)
            data = (os.readlink(path).encode() if path.is_symlink()
                    else read_input(root, relative, 4096))
            require(data == SOURCE_LINKS[relative.as_posix()].encode(),
                    "Unexpected source link target: " + relative.as_posix())
            mode = 0o120777
        else:
            require(mode != 0o120000, "Unexpected source link: " + relative.as_posix())
            data = read_input(root, relative, 32 * 1024 * 1024)
            mode = 0o100755 if (mode == 0o100755 or data.startswith(b"#!")) else 0o100644
        if safe_content(data, identifiers):
            entry = "XKCNSU/" + relative.as_posix()
            source_entries[entry] = data
            source_modes[entry] = mode
    require(source_entries, "No public source files were selected")
    missing = REQUIRED_SOURCES - {name.removeprefix("XKCNSU/") for name in source_entries}
    require(not missing, "Required source/license/provenance is missing: " + ", ".join(sorted(missing)))
    reports["kpatch-host-final"] = host_report_summary(host_report, source_entries)
    source_entries["XKCNSU/BUILDING-SOURCE.md"] = REBUILD_NOTES
    source_manifest = {
        "schema": 2, "history_included": False, "rebuild_notes": "BUILDING-SOURCE.md",
        "files": [{**descriptor(name.removeprefix("XKCNSU/"), data),
                   "mode": format(source_modes.get(name, 0o100644), "06o")}
                  for name, data in sorted(source_entries.items())],
    }
    source_entries["XKCNSU/source-manifest.json"] = json_bytes(source_manifest)
    entries["manifests/release-artifacts.json"] = json_bytes(
        {"schema": 1, "apk": public_apk, "binaries": binaries})
    entries["manifests/test-conclusions.json"] = json_bytes(
        {"schema": 1, "reports": reports, "raw_evidence_included": False,
         "check_identifiers": "one-based source order; free-form descriptions omitted"})
    entries["manifests/images.json"] = json_bytes(images)
    add_checksums(source_entries, "XKCNSU/SHA256SUMS", source_modes)
    add_checksums(entries, "SHA256SUMS")
    require(all(safe_content(data, identifiers)
                for data in (*source_entries.values(), *entries.values())),
            "A public package entry contains private material")
    return {"source": source_entries, "source_modes": source_modes,
            "artifacts": entries, "apk": public_apk, "images": images}


def write_zip(path, entries, level, modes=None):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=level) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = (modes or {}).get(name, 0o100644)
            if modes is None and name.endswith(("/ksud", "/ksuinit", "/kptools")):
                mode = 0o100755
            info.external_attr = mode << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=level)


def package_release(root, source_paths=None):
    root = Path(root).resolve(strict=True)
    plan = build_plan(root, source_paths)
    output = root / "out/delivery"
    require(not output.is_symlink() and output.resolve().is_relative_to(root),
            "Unsafe delivery directory")
    if output.exists():
        require(all(path.name in DELIVERY_NAMES and not path.is_symlink()
                    for path in output.iterdir()),
                "Delivery directory contains legacy or unrelated files; review them locally")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".package-", dir=output) as staging:
        staging = Path(staging)
        archives = {}
        for kind, filename, level in (
            ("source", "XKCNSU-source.zip", 6),
            ("artifacts", "XKCNSU-build-and-images.zip", 3),
        ):
            path = staging / filename
            write_zip(path, plan[kind], level,
                      plan["source_modes"] if kind == "source" else None)
            archives[kind] = {"path": filename, "bytes": path.stat().st_size,
                              "sha256": digest(path)}
        report = {
            **archives, "apk": plan["apk"], "schema": 2,
            "private_keys_included": False, "history_bundle_included": False,
            "raw_device_evidence_included": False,
            "images_included": plan["images"]["images_included"],
            "development_acceptance": True,
            "exact_device_backups_included": False,
        }
        (staging / "delivery.json").write_bytes(json_bytes(report))
        for name in DELIVERY_NAMES:
            os.replace(staging / name, output / name)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check-only", action="store_true",
                        help="Validate and select inputs without creating delivery files")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            plan = build_plan(args.root)
            report = {"validated": True, "source_files": len(plan["source"]),
                      "artifact_entries": len(plan["artifacts"]), "delivery_written": False}
        else:
            report = package_release(args.root)
    except (PackagingError, OSError, KeyError, TypeError) as error:
        raise SystemExit("Packaging blocked: " + str(error)) from error
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
