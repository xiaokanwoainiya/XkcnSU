#!/usr/bin/env bash
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
stage="${1:-fetch}"
xkcnsu_log "rust-$stage"
xkcnsu_space
xkcnsu_heavy_lock
baseline="$XKCNSU_CACHE/sources/baseline-$(git -C "$XKCNSU_ROOT" rev-parse --short=12 HEAD)"
if [[ ! -d "$baseline/.git" ]]; then
    mkdir -p "$(dirname "$baseline")"
    xkcnsu_run git clone --shared --no-hardlinks "$XKCNSU_ROOT" "$baseline"
fi
cd "$baseline"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
case "$stage" in
    fetch)
        xkcnsu_run cargo fetch --locked --manifest-path "$baseline/Cargo.toml"
        find "$CARGO_HOME/git/checkouts" -maxdepth 3 -name Cargo.toml -path '*android_bootimg*' -print
        ;;
    baseline-check|baseline-clippy)
        test -x "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
        export LIBCLANG_PATH="${LIBCLANG_PATH:-$(llvm-config --libdir)}"
        # shellcheck source=/dev/null
        source <(sed 's/\r$//' "$baseline/.github/scripts/setup-rust-build.sh") aarch64-linux-android "$XKCNSU_ANDROID_API"
        operation=check
        [[ "$stage" != baseline-clippy ]] || operation=clippy
        xkcnsu_run cargo ndk -t arm64-v8a -P "$XKCNSU_ANDROID_API" "$operation" \
            --locked --workspace --all-targets
        ;;
    baseline-btf-test)
        xkcnsu_run cargo test --locked --package ksud --target x86_64-unknown-linux-gnu \
            rejects_conflicting_loading_module_values
        ;;
    host-tests)
        cd "$XKCNSU_ROOT"
        xkcnsu_run cargo test --locked --package ksud --target x86_64-unknown-linux-gnu image_transaction
        xkcnsu_run cargo test --locked --package ksud --target x86_64-unknown-linux-gnu kpm_image
        xkcnsu_run cargo test --locked --package ksud --target x86_64-unknown-linux-gnu
        xkcnsu_run bash "$XKCNSU_ROOT/tools/test-kpatch-host.sh" --lock-held
        ;;
    format-check)
        cd "$XKCNSU_ROOT"
        xkcnsu_run cargo fmt --all -- --check
        ;;
    android-check|android-clippy)
        cd "$XKCNSU_ROOT"
        test -x "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/clang"
        export LIBCLANG_PATH="${LIBCLANG_PATH:-$(llvm-config --libdir)}"
        export KSU_PACKAGE_NAME="$XKCNSU_PACKAGE_NAME"
        # shellcheck source=/dev/null
        source <(sed 's/\r$//' "$XKCNSU_ROOT/.github/scripts/setup-rust-build.sh") aarch64-linux-android "$XKCNSU_ANDROID_API"
        operation=check
        lock_arguments=()
        if [[ "$stage" == android-clippy ]]; then
            operation=clippy
            lock_arguments=(--locked)
        fi
        if [[ "$operation" == clippy ]]; then
            xkcnsu_run cargo ndk -t arm64-v8a -P "$XKCNSU_ANDROID_API" "$operation" \
                "${lock_arguments[@]}" --workspace --all-targets -- -D warnings
        else
            xkcnsu_run cargo ndk -t arm64-v8a -P "$XKCNSU_ANDROID_API" "$operation" \
                "${lock_arguments[@]}" --workspace --all-targets
        fi
        ;;
    *) xkcnsu_die "Usage: $0 {fetch|baseline-check|baseline-clippy|baseline-btf-test|host-tests|format-check|android-check|android-clippy}" ;;
esac
