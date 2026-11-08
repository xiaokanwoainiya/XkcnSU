#!/usr/bin/env bash
# Run the production C harnesses and standalone native KPM backend tests.
# build-rust.sh already holds the heavy-build lock; call this with --lock-held.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"

lock_held=false
case "${1:-}" in
    "") [[ "$#" == 0 ]] || xkcnsu_die "Usage: $0 [--lock-held]" ;;
    --lock-held) [[ "$#" == 1 ]] || xkcnsu_die "Usage: $0 [--lock-held]"; lock_held=true ;;
    --help|-h) printf 'Usage: %s [--lock-held]\n' "$0"; exit 0 ;;
    *) xkcnsu_die "Usage: $0 [--lock-held]" ;;
esac

export XKCNSU_JOBS=1
export CARGO_BUILD_JOBS=1
export CARGO_INCREMENTAL=0
xkcnsu_freeze "$@"
xkcnsu_log "kpatch-host-tests"
if [[ "$lock_held" == false ]]; then
    xkcnsu_heavy_lock
else
    printf 'Using the caller-held heavy-build lock; jobs=1.\n'
fi

test_work="$XKCNSU_CACHE/build/kpatch-host-tests"
mkdir -p "$test_work"
test_run="$(mktemp -d "$test_work/run.XXXXXX")"
host_cc="${HOST_CC:-cc}"
test_sources="$XKCNSU_ROOT/vendor/kpatch-next/tests"
native_manifest="$test_sources/native/Cargo.toml"
step_count=0
printf 'HARNESS CACHE: %s\n' "$test_run"

python3 - "$XKCNSU_ROOT" "$test_run/sources-before.json" <<'PY'
import datetime
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
paths = [
    "tools/test-kpatch-host.sh",
    "tools/build-rust.sh",
    "tools/build-common.sh",
    "vendor/kpatch-next/kernel/patch/common/supercall.c",
    "vendor/kpatch-next/kernel/patch/module/module.c",
    "vendor/kpatch-next/kernel/patch/module/relo.c",
    "vendor/kpatch-next/kernel/base/baselib.c",
    "vendor/kpatch-next/kernel/include/baselib.h",
    "vendor/kpatch-next/kernel/include/kpmodule.h",
    "vendor/kpatch-next/kernel/patch/include/module.h",
    "vendor/kpatch-next/tests/loader_host.c",
    "vendor/kpatch-next/tests/loader_host_stubs.h",
    "vendor/kpatch-next/tests/supercall_string_host.c",
    "vendor/kpatch-next/tests/supercall_string_host_stubs.h",
    "vendor/kpatch-next/tests/relocation_host.c",
    "vendor/kpatch-next/tests/relocation_host_stubs.h",
    "vendor/kpatch-next/tests/baselib_host.c",
    "vendor/kpatch-next/tests/native/Cargo.toml",
    "vendor/kpatch-next/tests/native/Cargo.lock",
    "userspace/ksud/src/kpm.rs",
    "userspace/ksud/src/kpm_elf.rs",
    "userspace/ksud/src/kpm_runtime.rs",
    "userspace/ksud/src/kpm_store.rs",
    "userspace/ksud/src/kpm_tests.rs",
]
paths.extend(str(path.relative_to(root)) for path in
             (root / "vendor/kpatch-next/tests/native/src").rglob("*.rs"))
sources = []
for relative in sorted(paths):
    data = (root / relative).read_bytes()
    sources.append({"path": relative, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()})
snapshot = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sources": sources}
pathlib.Path(sys.argv[2]).write_text(json.dumps(snapshot, indent=2) + "\n")
PY

write_manifest() {
    python3 - "$XKCNSU_ROOT" "$XKCNSU_OUT/manifests/host-kpatch-final.json" \
        "$test_run" "$XKCNSU_LOG" "$1" "$lock_held" <<'PY'
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

root, destination, run = map(pathlib.Path, sys.argv[1:4])
snapshot = json.loads((run / "sources-before.json").read_text())
labels = ["compile-baselib-object", "compile-baselib", "baselib",
          "compile-loader", "loader", "compile-supercall", "supercall",
          "compile-relocation", "relocation", "native-cargo"]
steps = []
outputs = {}
for index, label in enumerate(labels, 1):
    log = run / f"{index}-{label}.log"
    status = log.with_suffix(".log.exit")
    text = log.read_text(errors="replace") if log.exists() else ""
    outputs[label] = text
    steps.append({"name": label, "log": str(log),
                  "command": text.partition("\n")[0].removeprefix("COMMAND: "),
                  "exit_code": int(status.read_text()) if status.exists() else None})
for source in snapshot["sources"]:
    path = root / source["path"]
    source["sha256_after"] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    source["unchanged"] = source["sha256"] == source["sha256_after"]
sources_unchanged = all(source["unchanged"] for source in snapshot["sources"])
loader = re.search(r"PASS \((\d+) groups; (\d+) text-boundary cases\)", outputs["loader"])
supercall = re.search(r"PASS \((\d+) groups; (\d+) string, (\d+) load-buffer\)", outputs["supercall"])
relocation = re.search(r"PASS \((\d+) groups\)", outputs["relocation"])
baselib = re.search(r"PASS \((\d+) groups; (\d+) loader helpers\)", outputs["baselib"])
helpers = ["memchr", "memcmp", "memcpy", "memset", "strcpy", "strcmp",
           "strncmp", "strlen", "strchr"]
symbol_check = {"ok": False, "required_helpers": ["lib_" + name for name in helpers]}
object_file = run / "baselib_host.o"
if object_file.exists():
    symbols = subprocess.run(["nm", "-g", "--defined-only", str(object_file)],
                             capture_output=True, text=True, check=False)
    defined = {line.split()[-1] for line in symbols.stdout.splitlines() if line.split()}
    overridden = sorted(set(helpers) & defined)
    symbol_check.update({
        "renamed_global_memset": "xkcnsu_host_global_memset" in defined,
        "libc_symbols_overridden": overridden,
        "ok": symbols.returncode == 0 and not overridden
              and set(symbol_check["required_helpers"]) <= defined
              and "xkcnsu_host_global_memset" in defined,
    })
alias_pattern = r"^#define\s+(" + "|".join(helpers) + r")\s+(lib_\w+)\s*$"
production_aliases = dict(re.findall(alias_pattern,
    (root / "vendor/kpatch-next/kernel/patch/module/module.c").read_text(), re.MULTILINE))
host_aliases = dict(re.findall(alias_pattern,
    (root / "vendor/kpatch-next/tests/loader_host_stubs.h").read_text(), re.MULTILINE))
aliases_match = all(production_aliases.get(name) == host_aliases.get(name) == "lib_" + name
                    for name in helpers)
native = re.findall(
    r"test result: (\w+)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
    r"(\d+) measured; (\d+) filtered out", outputs["native-cargo"])
native_passed = sum(int(result[1]) for result in native)
native_failed = sum(int(result[2]) for result in native)
all_steps_passed = all(step["exit_code"] == 0 for step in steps)
ok = (int(sys.argv[5]) == 0 and all_steps_passed and sources_unchanged
      and loader is not None and supercall is not None and relocation is not None
      and baselib is not None and symbol_check["ok"] and aliases_match
      and native_passed > 0 and native_failed == 0
      and all(result[0] == "ok" for result in native))
results = {
    "baselib": {"groups_passed": int(baselib[1]), "loader_helpers": int(baselib[2])} if baselib else None,
    "loader": {"groups_passed": int(loader[1]), "text_boundary_cases": int(loader[2])} if loader else None,
    "supercall": {"groups_passed": int(supercall[1]), "string_groups": int(supercall[2]),
                  "load_buffer_groups": int(supercall[3])} if supercall else None,
    "relocation": {"groups_passed": int(relocation[1])} if relocation else None,
    "native": {"tests_passed": native_passed, "tests_failed": native_failed,
               "tests_ignored": sum(int(result[3]) for result in native),
               "tests_filtered": sum(int(result[5]) for result in native),
               "test_targets": len(native)},
}
binaries = []
for name in ("baselib_host.o", "baselib_host", "loader_host", "supercall_host", "relocation_host"):
    path = run / name
    if path.exists():
        data = path.read_bytes()
        binaries.append({"path": str(path), "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
report = {
    "schema": 1, "kind": "kpatch-host-final", "ok": ok,
    "started_utc": snapshot["started_utc"],
    "completed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "log": sys.argv[4], "run_cache": str(run),
    "build_jobs": 1, "rust_test_threads": 1,
    "heavy_lock": "caller-held" if sys.argv[6] == "true" else "acquired",
    "production_sources_edited": False, "device_commands": False, "engine_rebuilt": False,
    "sanitizers": ["address", "undefined"], "c_warnings_as_errors": True,
    "sources_unchanged": sources_unchanged,
    "baselib_symbol_check": symbol_check, "loader_helper_aliases_match": aliases_match,
    "results": results, "steps": steps, "sources": snapshot["sources"],
    "test_binaries": binaries,
    "limitations": [
        "Host tests do not execute Android kernel VFS, HW KASAN, or ARM64 cache instructions.",
        "Native suite uses test-only image hooks and a mock Engine; Android transport is not executed.",
    ],
}
destination.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile("w", dir=destination.parent, prefix=".host-kpatch-",
                                 suffix=".json", delete=False) as output:
    json.dump(report, output, indent=2)
    output.write("\n")
    temporary = output.name
os.replace(temporary, destination)
print(f"MANIFEST: {destination}")
if not ok:
    print("Host suite incomplete, failed, or source hashes changed.", file=sys.stderr)
    sys.exit(1)
PY
}

run_step() {
    local label="$1" duration="$2" step_log result
    shift 2
    step_count=$((step_count + 1))
    step_log="$test_run/$step_count-$label.log"
    printf '[%s/10] %s\n' "$step_count" "$label"
    if {
        printf 'COMMAND:'
        printf ' %q' "$@"
        printf '\n'
        timeout --kill-after=5s "$duration" "$@"
    } >"$step_log" 2>&1; then
        printf '0\n' >"$step_log.exit"
        cat "$step_log" >>"$XKCNSU_LOG"
        awk '/PASS|test result:/ && shown++ < 6 { print }' "$step_log"
    else
        result=$?
        printf '%s\n' "$result" >"$step_log.exit"
        cat "$step_log" >>"$XKCNSU_LOG"
        printf 'FAILED: %s (exit %s); last 40 lines follow. Full log: %s\n' \
            "$label" "$result" "$XKCNSU_LOG" >&2
        tail -n 40 "$step_log" >&2
        write_manifest "$result" || true
        exit "$result"
    fi
}

common_flags=(
    -std=gnu11 -g -fsanitize=address,undefined -fno-sanitize-recover=undefined
    -Wall -Wextra -Werror -Wno-unused-parameter
)
run_step compile-baselib-object 90s "$host_cc" "${common_flags[@]}" -fno-builtin \
    -Dmemset=xkcnsu_host_global_memset -include stddef.h \
    -idirafter "$XKCNSU_ROOT/vendor/kpatch-next/kernel/include" \
    -c "$XKCNSU_ROOT/vendor/kpatch-next/kernel/base/baselib.c" -o "$test_run/baselib_host.o"
run_step compile-baselib 90s "$host_cc" "${common_flags[@]}" -fno-builtin \
    "$test_sources/baselib_host.c" "$test_run/baselib_host.o" -o "$test_run/baselib_host"
run_step baselib 20s "$test_run/baselib_host"
run_step compile-loader 90s "$host_cc" "${common_flags[@]}" -fno-builtin -pthread -Wno-sign-compare \
    "$test_sources/loader_host.c" "$test_run/baselib_host.o" -o "$test_run/loader_host"
run_step loader 20s "$test_run/loader_host"
run_step compile-supercall 90s "$host_cc" "${common_flags[@]}" -O1 \
    "$test_sources/supercall_string_host.c" -o "$test_run/supercall_host"
run_step supercall 20s "$test_run/supercall_host"
# The production relocator intentionally falls through from NC variants.
run_step compile-relocation 90s "$host_cc" "${common_flags[@]}" -Wno-implicit-fallthrough \
    "$test_sources/relocation_host.c" -o "$test_run/relocation_host"
run_step relocation 20s "$test_run/relocation_host"
run_step native-cargo 900s cargo test --locked --jobs 1 \
    --manifest-path "$native_manifest" --target x86_64-unknown-linux-gnu \
    --all-targets -- --test-threads=1
write_manifest 0
printf 'KPatch host tests: PASS (4 C harnesses and standalone native Cargo suite).\n'
