#!/usr/bin/env bash
# Build the Linux offline image tool from current sources and real Android assets.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log host-ksud
xkcnsu_space
xkcnsu_heavy_lock
work="$XKCNSU_CACHE/build/host/source"
mkdir -p "$(dirname "$work")" "$XKCNSU_OUT/tools"
exec {host_lock}>"$XKCNSU_CACHE/build/host.lock"
flock "$host_lock"
if [[ ! -d "$work/.git" ]]; then
    xkcnsu_run git clone --shared --no-hardlinks "$XKCNSU_ROOT" "$work"
fi
xkcnsu_run rsync -a --delete --exclude=.git --exclude=out --exclude=target \
    --exclude=build --exclude=.gradle --exclude=.kotlin --exclude=.cxx \
    --exclude=node_modules --exclude=local.properties --exclude=dist \
    "$XKCNSU_ROOT/" "$work/"
cd "$work"
export KSU_PACKAGE_NAME="$XKCNSU_PACKAGE_NAME"
# shellcheck source=/dev/null
source <(sed 's/\r$//' "$work/.github/scripts/setup-rust-build.sh") aarch64-linux-android "$XKCNSU_ANDROID_API"
builtins="$("$CLANG_PATH" --print-resource-dir)/lib/linux/libclang_rt.builtins-aarch64-android.a"
export RUSTFLAGS="-C target-feature=+crt-static -C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wno-unused-command-line-argument -C link-arg=$builtins"
xkcnsu_run cargo build --locked --package ksuinit --target aarch64-linux-android --release
unset RUSTFLAGS
assets="$work/userspace/ksud/bin/aarch64"
mkdir -p "$assets"
cp "$CARGO_TARGET_DIR/aarch64-linux-android/release/ksuinit" "$assets/ksuinit"
for module in "$XKCNSU_OUT/aarch64/"*.ko; do
    [[ -f "$module" ]] || continue
    cp "$module" "$assets/"
done
for name in kpimg kptools; do
    [[ -s "$XKCNSU_OUT/kpatch/aarch64/$name" ]] || xkcnsu_die "Missing source-built $name."
    cp "$XKCNSU_OUT/kpatch/aarch64/$name" "$assets/$name"
done
for kmi in android14-6.1 android12-5.10; do
    for suffix in kernelsu susfs_guard_lkm; do
        [[ -s "$assets/${kmi}_${suffix}.ko" ]] || xkcnsu_die "Missing real asset: ${kmi}_${suffix}.ko."
    done
done
xkcnsu_run cargo clean --package ksud --target x86_64-unknown-linux-gnu
xkcnsu_run cargo build --locked --package ksud --target x86_64-unknown-linux-gnu
cp "$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/debug/ksud" "$XKCNSU_OUT/tools/ksud"
xkcnsu_run "$XKCNSU_OUT/tools/ksud" --help
xkcnsu_run file "$XKCNSU_OUT/tools/ksud"
sha256sum "$XKCNSU_OUT/tools/ksud" >"$XKCNSU_OUT/tools/ksud.sha256"
xkcnsu_space
