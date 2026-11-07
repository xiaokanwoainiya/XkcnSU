#!/usr/bin/env bash
# Full local/CI build after every source owner has declared the inputs ready.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
[[ "${1:-}" == --source-ready ]] || xkcnsu_die "Usage: build-all.sh --source-ready"
xkcnsu_log all
xkcnsu_run bash "$XKCNSU_ROOT/tools/bootstrap-xkcnsu.sh" doctor
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-signing.sh"
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-freeze-kernel.sh" --with-susfs
xkcnsu_run python3 "$XKCNSU_ROOT/tools/build-matrix.py" --jobs 1 --stage-assets
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-kpatch.sh"
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-rust.sh" android-check
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-rust.sh" android-clippy
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-rust.sh" format-check
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-rust.sh" host-tests
xkcnsu_run bash "$XKCNSU_ROOT/tools/build-release.sh" snapshot --source-ready
for stage in ksuinit ksud manager repack; do
    xkcnsu_run bash "$XKCNSU_ROOT/tools/build-release.sh" "$stage"
done
xkcnsu_space
