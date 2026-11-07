#!/usr/bin/env bash
# Build isolated ARM64 root/SUSFS modules with the repository's pinned DDK.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
kmi="${1:-android14-6.1}"
mode="${2:-both}"
case "$kmi" in
    android12-5.10|android13-5.10|android13-5.15|android14-5.15|android14-6.1|android15-6.6|android16-6.12|android17-6.18) ;;
    *) xkcnsu_die "Unknown KMI: $kmi" ;;
esac
case "$mode" in root|susfs|both|baseline) ;; *) xkcnsu_die "Mode must be root, susfs, both, or baseline." ;; esac
xkcnsu_log "lkm-$kmi-$mode"
xkcnsu_space
xkcnsu_heavy_lock
metadata="$XKCNSU_OUT/private/certificate.json"
[[ -f "$metadata" ]] || bash "$XKCNSU_ROOT/tools/build-signing.sh"
identity="$(python3 "$XKCNSU_ROOT/tools/verify-signing-identity.py" \
    --metadata "$metadata" --package "$XKCNSU_PACKAGE_NAME" --kernel-args)"
read -r package cert_size cert_hash <<<"$identity"
image="ghcr.io/ylarod/ddk-min:$kmi-$XKCNSU_DDK_RELEASE"
if ! docker image inspect "$image" >/dev/null 2>&1; then
    bash "$XKCNSU_ROOT/tools/bootstrap-xkcnsu.sh" ddk "$kmi"
fi
source_root="${XKCNSU_SOURCE_ROOT:-$XKCNSU_ROOT}"
if [[ -z "${XKCNSU_SOURCE_ROOT:-}" && -f "$XKCNSU_CACHE/sources/kernel-source-ready/.xkcnsu-kernel-ready" ]]; then
    source_root="$XKCNSU_CACHE/sources/kernel-source-ready"
fi
destination="$XKCNSU_OUT/aarch64"
if [[ "$mode" == baseline ]]; then
    source_root="$XKCNSU_CACHE/sources/baseline-$(git -C "$XKCNSU_ROOT" rev-parse --short=12 HEAD)"
    [[ -d "$source_root/.git" ]] || xkcnsu_die "Run build-rust.sh fetch to create the baseline snapshot first."
    destination="$XKCNSU_OUT/baseline/aarch64"
fi
work="$XKCNSU_CACHE/build/lkm/$mode/$kmi"
mkdir -p "$work/kernel" "$work/uapi" "$destination"
exec {build_lock}>"$XKCNSU_CACHE/build/lkm/$mode-$kmi.lock"
flock "$build_lock"
xkcnsu_run rsync -a --delete --exclude='*.o' --exclude='*.ko' --exclude='*.cmd' \
    --exclude='*.mod' --exclude='*.mod.c' --exclude='*.symversions' --exclude=Module.symvers \
    --exclude=modules.order --exclude=check_symbol --exclude=out \
    "$source_root/kernel/" "$work/kernel/"
xkcnsu_run rsync -a --delete "$source_root/uapi/" "$work/uapi/"
if [[ "$mode" != baseline ]]; then
    [[ -f "$source_root/version.properties" ]] ||
        xkcnsu_die "Kernel source snapshot has no shared release version; freeze it again."
    cp "$source_root/version.properties" "$work/version.properties"
fi
(
    cd "$source_root"
    find kernel uapi -type f \( -name '*.c' -o -name '*.h' -o -name '*.S' -o -name Kbuild -o -name Makefile \) \
        ! -name '*.mod.c' -print0 | sort -z | xargs -0 sha256sum
    if [[ -f version.properties ]]; then sha256sum version.properties; fi
) >"$work/source.sha256"
gitdir="$(git -C "$XKCNSU_ROOT" rev-parse --absolute-git-dir)"
# shellcheck disable=SC2016
container_script='
set -Eeuo pipefail
git config --global --add safe.directory /source
cd /source/kernel
printf "DDK KDIR=%s ARCH=%s\n" "$KDIR" "$ARCH"
if [[ "$XKCNSU_MODE" != susfs ]]; then
    make -C "$KDIR" M=/source/kernel ARCH=arm64 CONFIG_ARM64=y CONFIG_KSU=m clean
    CONFIG_KSU=m CC=clang make -j"$XKCNSU_JOBS" \
        KSU_PACKAGE_NAME="$KSU_PACKAGE_NAME" KSU_EXPECTED_SIZE="$KSU_EXPECTED_SIZE" \
        KSU_EXPECTED_HASH="$KSU_EXPECTED_HASH"
    cp kernelsu.ko "/artifacts/${XKCNSU_KMI}_kernelsu.ko"
    llvm-strip -d "/artifacts/${XKCNSU_KMI}_kernelsu.ko"
    readelf -h "/artifacts/${XKCNSU_KMI}_kernelsu.ko"
fi
if [[ "$XKCNSU_MODE" == both || "$XKCNSU_MODE" == susfs ]]; then
    test -f susfs/Makefile
    extra_symbols=()
    if [[ -f /source/kernel/Module.symvers ]]; then
        extra_symbols=(KBUILD_EXTRA_SYMBOLS=/source/kernel/Module.symvers)
    fi
    if [[ "$XKCNSU_KMI" == android17-6.18 ]]; then
        make -C "$KDIR" M=/source/kernel/susfs MO=/source/kernel/susfs \
            modules -j"$XKCNSU_JOBS" CC=clang "${extra_symbols[@]}"
    else
        make -C "$KDIR" M=/source/kernel/susfs src=/source/kernel/susfs \
            modules -j"$XKCNSU_JOBS" CC=clang "${extra_symbols[@]}"
    fi
    cp susfs/susfs_guard_lkm.ko "/artifacts/${XKCNSU_KMI}_susfs_guard_lkm.ko"
    llvm-strip -d "/artifacts/${XKCNSU_KMI}_susfs_guard_lkm.ko"
    readelf -h "/artifacts/${XKCNSU_KMI}_susfs_guard_lkm.ko"
fi
'
xkcnsu_run docker run --rm --network host \
    --cpuset-cpus="0-$((XKCNSU_JOBS - 1))" \
    --memory=1536m --memory-swap=2g --pids-limit=512 \
    --mount "type=bind,src=$work,dst=/source" \
    --mount "type=bind,src=$gitdir,dst=/source/.git,readonly" \
    --mount "type=bind,src=$destination,dst=/artifacts" \
    -e "XKCNSU_MODE=$mode" -e "XKCNSU_KMI=$kmi" -e "XKCNSU_JOBS=$XKCNSU_JOBS" \
    -e "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" -e KBUILD_BUILD_USER=xkcnsu -e KBUILD_BUILD_HOST=xkcnsu \
    -e "KSU_PACKAGE_NAME=$package" -e "KSU_EXPECTED_SIZE=$cert_size" \
    -e "KSU_EXPECTED_HASH=$cert_hash" "$image" bash -c "$container_script"
mkdir -p "$XKCNSU_OUT/manifests"
cp "$work/source.sha256" "$XKCNSU_OUT/manifests/lkm-$kmi-$mode-source.sha256"
python3 - "$destination" "$kmi" "$mode" "$metadata" "$image" "$XKCNSU_OUT/manifests" "$work/version.properties" <<'PY'
import configparser, hashlib, json, pathlib, subprocess, sys
directory, kmi, mode, cert, image, manifests, version_file = sys.argv[1:]
artifacts = []
for path in sorted(pathlib.Path(directory).glob(f"{kmi}_*.ko")):
    data = path.read_bytes()
    artifacts.append({"file": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
report = {
    "kmi": kmi, "mode": mode, "artifacts": artifacts,
    "signing": json.load(open(cert)),
    "ddk": json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]["RepoDigests"],
    "root_rebuilt": mode in ("root", "both", "baseline"),
    "susfs_rebuilt": mode in ("susfs", "both"),
    "susfs_reused": mode == "root",
}
if mode != "baseline":
    version = configparser.ConfigParser(interpolation=None)
    version.read_string("[release]\n" + pathlib.Path(version_file).read_text())
    report["release"] = {
        "version_name": version["release"]["XKCNSU_VERSION_NAME"],
        "version_code": version["release"].getint("XKCNSU_VERSION_CODE"),
        "manifest_sha256": hashlib.sha256(pathlib.Path(version_file).read_bytes()).hexdigest(),
    }
path = pathlib.Path(manifests) / f"lkm-{kmi}-{mode}.json"
path.write_text(json.dumps(report, indent=2) + "\n")
if mode != "baseline":
    (pathlib.Path(manifests) / f"lkm-{kmi}.json").write_text(json.dumps(report, indent=2) + "\n")
    # The pair manifest always describes the actual current pair, including reused components.
    (pathlib.Path(manifests) / f"lkm-{kmi}-both.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"manifest": str(path), "artifacts": artifacts}, indent=2))
PY
xkcnsu_space
