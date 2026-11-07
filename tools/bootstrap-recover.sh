#!/usr/bin/env bash
# Recover the generated C: build cache after a WSL interruption.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
profile="${XKCNSU_WINDOWS_PROFILE:-$(find /mnt/c/Users -mindepth 2 -maxdepth 2 -type d -name .codex -printf '%h\n' -quit)}"
image="$profile/.cache/xkcnsu/build-cache.ext4"
[[ -f "$image" ]] || xkcnsu_die "Build cache image is missing."
mkdir -p "$XKCNSU_OUT/build-logs"
recovery_log="$XKCNSU_OUT/build-logs/cache-recovery-$(date -u +%Y%m%dT%H%M%S).log"
if ! mountpoint -q "$XKCNSU_CACHE"; then
    if losetup -j "$image" | grep -q .; then
        xkcnsu_die "Cache image still has an active loop device; inspect before filesystem repair."
    fi
    set +e
    e2fsck -f -p "$image" >"$recovery_log" 2>&1
    result=$?
    set -e
    tail -n 15 "$recovery_log"
    (( result == 0 || result == 1 )) ||
        xkcnsu_die "Filesystem check needs manual attention (exit $result); cache preserved."
fi
bash "$XKCNSU_ROOT/tools/bootstrap-xkcnsu.sh" cache
bash "$XKCNSU_ROOT/tools/bootstrap-xkcnsu.sh" docker
printf 'Build cache and isolated Docker restored. Resume with one build at a time.\n'
