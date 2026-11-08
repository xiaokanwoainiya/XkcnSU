#!/usr/bin/env python3
"""Check Git publication candidates for local artifacts and private material."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    listed = subprocess.check_output([
        "git", "-C", str(root), "ls-files", "-c", "-o", "--exclude-standard", "-z"
    ]).split(b"\0")
    private_parts = {
        ".git", "out", "target", "node_modules", ".gradle", ".kotlin", ".cxx",
        "build", "private", "credentials", "secrets", "device-backup",
        "__pycache__",
    }
    private_suffixes = {".apk", ".ko", ".img", ".p12", ".pfx", ".jks",
                        ".keystore", ".pem", ".key", ".pcap", ".pcapng", ".hprof"}
    public_modules = {
        "manager/app/src/main/assets/ghostlock/vrpatch.ko":
            "9264a0abe2ab1189c6f86c730a53fec52f6058ceb14befc9625052997963f577",
    }
    secret = re.compile(
        rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|"
        rb"gh[pousr]_[A-Za-z0-9_]{30,}|"
        rb"github_pat_[A-Za-z0-9_]{40,}|"
        rb"AKIA[0-9A-Z]{16}"
    )
    local = re.compile(rb"(?:[CD]:[/\\]Users[/\\]|/mnt/[cd]/Users/)", re.I)
    issues, files = [], []
    for name in sorted({entry.decode("utf-8") for entry in listed if entry}):
        relative = PurePosixPath(name)
        path = root / name
        if not path.is_file() and not path.is_symlink():
            continue
        if (set(part.lower() for part in relative.parts) & private_parts or
                (relative.suffix.lower() in private_suffixes and name not in public_modules) or
                relative.name.lower() in {"temp.gz", "local.properties", "keystore-password"} or
                relative.name.lower().startswith(".env")):
            issues.append({"path": name, "reason": "local-or-private-artifact"})
            continue
        if path.is_symlink():
            if name not in {"kernel/include/uapi", "manager/app/src/main/cpp/uapi"}:
                issues.append({"path": name, "reason": "unexpected-symlink"})
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if name in public_modules and digest != public_modules[name]:
            issues.append({"path": name, "reason": "public-module-digest-mismatch"})
        if secret.search(data):
            issues.append({"path": name, "reason": "private-key-or-access-token"})
        if local.search(data):
            issues.append({"path": name, "reason": "local-user-path"})
        files.append({"path": name, "sha256": digest})
    report = {"ok": not issues, "files": files, "issues": issues}
    output = root / "out/manifests/public-source-audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"ok": report["ok"], "files_checked": len(files), "issues": issues}, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    sys.exit(main())
