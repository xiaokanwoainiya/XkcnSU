#!/usr/bin/env bash
# Match the Arm GNU bare-metal release pinned in vendor/kpatch-next CI.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log kpatch-toolchain
xkcnsu_space
name=arm-gnu-toolchain-12.2.rel1-x86_64-aarch64-none-elf
archive="$XKCNSU_CACHE/downloads/$name.tar.xz"
directory="$XKCNSU_CACHE/toolchains/$name"
url="https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu/12.2.rel1/binrel/$name.tar.xz"
mkdir -p "$XKCNSU_CACHE/downloads" "$XKCNSU_CACHE/toolchains" "$XKCNSU_OUT/toolchain"
if [[ ! -x "$directory/bin/aarch64-none-elf-gcc" ]]; then
    xkcnsu_run curl --silent --show-error --fail --location --retry 3 --continue-at - "$url" -o "$archive"
    xkcnsu_run tar -xJf "$archive" -C "$XKCNSU_CACHE/toolchains"
fi
sha256sum "$archive" >"$XKCNSU_OUT/toolchain/$name.sha256"
xkcnsu_run "$directory/bin/aarch64-none-elf-gcc" --version
printf 'TARGET_COMPILE=%s/bin/aarch64-none-elf-\n' "$directory"
xkcnsu_space
