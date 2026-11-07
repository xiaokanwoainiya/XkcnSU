#!/usr/bin/env bash
# Shared paths and logging for the XKCNSU Linux build tools.
set -Eeuo pipefail

XKCNSU_ROOT="${XKCNSU_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
export XKCNSU_ROOT
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$XKCNSU_ROOT" show -s --format=%ct HEAD)}"
export TZ=UTC
export XKCNSU_CACHE="${XKCNSU_CACHE:-/opt/xkcnsu}"
export XKCNSU_OUT="${XKCNSU_OUT:-$XKCNSU_ROOT/out}"
export XKCNSU_JOBS="${XKCNSU_JOBS:-2}"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-$XKCNSU_JOBS}"
export CARGO_INCREMENTAL="${CARGO_INCREMENTAL:-0}"
export XKCNSU_DDK_RELEASE="${XKCNSU_DDK_RELEASE:-20260828}"
export XKCNSU_RUST_VERSION="${XKCNSU_RUST_VERSION:-1.93.1}"
export XKCNSU_NDK_VERSION="${XKCNSU_NDK_VERSION:-29.0.14206865}"
export XKCNSU_ANDROID_API="${XKCNSU_ANDROID_API:-26}"
export XKCNSU_PACKAGE_NAME="${XKCNSU_PACKAGE_NAME:-io.xkcnsu.manager}"
export CARGO_HOME="${CARGO_HOME:-$XKCNSU_CACHE/cargo}"
export RUSTUP_HOME="${RUSTUP_HOME:-$XKCNSU_CACHE/rustup}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$XKCNSU_CACHE/cargo-target}"
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-$XKCNSU_CACHE/gradle}"
export ANDROID_HOME="${ANDROID_HOME:-$XKCNSU_CACHE/android-sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export ANDROID_USER_HOME="${ANDROID_USER_HOME:-$XKCNSU_CACHE/android-user}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$XKCNSU_CACHE/xdg-cache}"
export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/$XKCNSU_NDK_VERSION"
export ANDROID_NDK_ROOT="$ANDROID_NDK_HOME"
export TMPDIR="${XKCNSU_TMPDIR:-$XKCNSU_CACHE/tmp}"
export DOCKER_HOST="${XKCNSU_DOCKER_HOST:-unix://$XKCNSU_CACHE/docker.sock}"
export DOCKER_CONFIG="${XKCNSU_DOCKER_CONFIG:-$XKCNSU_CACHE/docker-config}"
export PATH="$CARGO_HOME/bin:$ANDROID_HOME/cmdline-tools/23.0/bin:$ANDROID_HOME/build-tools/37.0.0:$PATH"
if [[ -f "$XKCNSU_CACHE/gitconfig" ]]; then
    export GIT_CONFIG_SYSTEM="$XKCNSU_CACHE/gitconfig"
fi

xkcnsu_die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

xkcnsu_freeze() {
    # Bash reads long scripts incrementally; isolate running jobs from concurrent edits.
    [[ "${XKCNSU_FROZEN_SCRIPT:-}" != "${BASH_SOURCE[1]}" ]] || return 0
    xkcnsu_init
    local snapshot original
    original="${BASH_SOURCE[1]}"
    snapshot="$(mktemp -d "$TMPDIR/tool-script.XXXXXX")"
    cp "$original" "$snapshot/$(basename "$original")"
    cp "$(dirname "$original")/build-common.sh" "$snapshot/build-common.sh"
    XKCNSU_FROZEN_SCRIPT="$snapshot/$(basename "$original")"
    export XKCNSU_FROZEN_SCRIPT
    exec bash "$XKCNSU_FROZEN_SCRIPT" "$@"
}

xkcnsu_init() {
    local path
    for path in "$XKCNSU_CACHE" "$CARGO_HOME" "$RUSTUP_HOME" "$CARGO_TARGET_DIR" "$GRADLE_USER_HOME"; do
        [[ "$path" != /mnt/d && "$path" != /mnt/d/* ]] ||
            xkcnsu_die "Build caches must not use D: ($path)."
    done
    if grep -qi microsoft /proc/sys/kernel/osrelease && [[ "$XKCNSU_CACHE" == /opt/xkcnsu ]]; then
        if ! mountpoint -q "$XKCNSU_CACHE"; then
            bash "$XKCNSU_ROOT/tools/bootstrap-xkcnsu.sh" cache
        fi
        mountpoint -q "$XKCNSU_CACHE" || xkcnsu_die "C: cache volume could not be mounted."
    fi
    mkdir -p "$XKCNSU_OUT/build-logs"
    if [[ ! -e "$XKCNSU_OUT/.gitignore" ]]; then
        printf '*\n' >"$XKCNSU_OUT/.gitignore"
    fi
    mkdir -p "$XKCNSU_CACHE" "$TMPDIR" "$DOCKER_CONFIG"
}

xkcnsu_log() {
    xkcnsu_init
    local label="${1:?log label required}"
    XKCNSU_LOG="$XKCNSU_OUT/build-logs/$(date -u +%Y%m%dT%H%M%S)-${label}-$$.log"
    export XKCNSU_LOG
    exec > >(tee -a "$XKCNSU_LOG") 2>&1
    printf 'UTC: %s\nLOG: %s\nROOT: %s\nCACHE: %s\n' \
        "$(date -u +%FT%TZ)" "$XKCNSU_LOG" "$XKCNSU_ROOT" "$XKCNSU_CACHE"
    trap 'xkcnsu_finish "$?"' EXIT
}

xkcnsu_finish() {
    local result="$1"
    trap - EXIT
    printf '\nEXIT: %s UTC: %s\n' "$result" "$(date -u +%FT%TZ)"
    exit "$result"
}

xkcnsu_run() {
    printf '\nCOMMAND:'
    printf ' %q' "$@"
    printf '\n'
    "$@"
}

xkcnsu_heavy_lock() {
    mkdir -p "$XKCNSU_CACHE/build"
    exec {XKCNSU_HEAVY_LOCK_FD}>"$XKCNSU_CACHE/build/heavy-build.lock"
    printf 'Waiting for the single heavy-build slot (memory-limited WSL).\n'
    flock "$XKCNSU_HEAVY_LOCK_FD"
    printf 'Heavy-build slot acquired; jobs=%s.\n' "$XKCNSU_JOBS"
}

xkcnsu_space() {
    df -h "$XKCNSU_CACHE" / /mnt/c /mnt/d 2>/dev/null || df -h "$XKCNSU_CACHE" /
    local available
    available="$(df -Pk "$XKCNSU_CACHE" | awk 'END {print $4}')"
    (( available >= 3 * 1024 * 1024 )) || xkcnsu_die "Less than 3 GiB remains in build cache."
    if [[ -d /mnt/c ]]; then
        available="$(df -Pk /mnt/c | awk 'END {print $4}')"
        (( available >= 12 * 1024 * 1024 )) || xkcnsu_die "C: has less than 12 GiB available."
    fi
}
