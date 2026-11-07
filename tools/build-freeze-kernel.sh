#!/usr/bin/env bash
# Preserve the source-ready kernel input independently of remaining UI/userspace work.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log kernel-freeze
destination="$XKCNSU_CACHE/sources/kernel-source-ready"
mkdir -p "$destination/kernel" "$destination/uapi" "$XKCNSU_OUT/manifests"
exclusions=(--exclude=susfs)
include_susfs=false
if [[ "${1:-}" == --with-susfs ]]; then
    exclusions=()
    include_susfs=true
fi
xkcnsu_run rsync -a --delete "${exclusions[@]}" --exclude='*.o' --exclude='*.ko' \
    --exclude='*.cmd' --exclude='*.mod' --exclude='*.mod.c' --exclude=Module.symvers \
    --exclude=modules.order --exclude=check_symbol --exclude=out \
    "$XKCNSU_ROOT/kernel/" "$destination/kernel/"
xkcnsu_run rsync -a --delete "$XKCNSU_ROOT/uapi/" "$destination/uapi/"
cp "$XKCNSU_ROOT/version.properties" "$destination/version.properties"
python3 - "$destination" "$XKCNSU_OUT/manifests/kernel-source-ready.json" "$include_susfs" <<'PY'
import configparser, hashlib, json, pathlib, sys
root, output = map(pathlib.Path, sys.argv[1:3])
files = []
version = root / "version.properties"
files.append({"path": version.name, "sha256": hashlib.sha256(version.read_bytes()).hexdigest()})
for directory in ("kernel", "uapi"):
    for path in sorted((root / directory).rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
manifest = {"source_ready": True, "kernel_snapshot": str(root), "susfs_excluded": sys.argv[3] != "true", "files": files}
release = configparser.ConfigParser(interpolation=None)
release.read_string("[release]\n" + version.read_text())
manifest["release"] = {
    "version_name": release["release"]["XKCNSU_VERSION_NAME"],
    "version_code": release["release"].getint("XKCNSU_VERSION_CODE"),
}
output.write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Frozen kernel inputs: {len(files)} files; manifest: {output}")
PY
if [[ "$include_susfs" == true ]]; then
    touch "$destination/.xkcnsu-kernel-ready"
fi
