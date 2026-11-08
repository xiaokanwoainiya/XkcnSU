#!/usr/bin/env bash
# Snapshot completed sources, then build ksuinit -> ksud -> Manager -> signed repack.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
stage="${1:-help}"
xkcnsu_log "release-$stage"
xkcnsu_space
case "$stage" in
    ksuinit|ksud|manager|manager-tests|manager-lint|manager-assemble|repack) xkcnsu_heavy_lock ;;
esac
work="${XKCNSU_RELEASE_SOURCE:-$XKCNSU_CACHE/build/release/source}"
abis="${XKCNSU_ABIS:-arm64-v8a x86_64}"
mkdir -p "$XKCNSU_CACHE/build"
lock_name="$(printf '%s' "$work" | sha256sum | cut -c1-16)"
exec {release_lock}>"$XKCNSU_CACHE/build/release-$lock_name.lock"
flock "$release_lock"
case "$stage" in
    snapshot)
        case "${2:-}" in
            --source-ready) input_state=source-ready ;;
            --preview) input_state=preview ;;
            *) xkcnsu_die "Use snapshot --source-ready, or snapshot --preview for provisional compiler feedback." ;;
        esac
        mkdir -p "$(dirname "$work")"
        if [[ ! -d "$work/.git" ]]; then
            xkcnsu_run git clone --shared --no-hardlinks "$XKCNSU_ROOT" "$work"
        fi
        xkcnsu_run rsync -a --delete --exclude=.git --exclude=out --exclude=target \
            --exclude=build --exclude=.gradle --exclude=.kotlin --exclude=.cxx \
            --exclude=node_modules --exclude=local.properties --exclude=dist \
            "$XKCNSU_ROOT/" "$work/"
        mkdir -p "$XKCNSU_OUT/manifests"
        git -C "$XKCNSU_ROOT" rev-parse HEAD >"$XKCNSU_OUT/manifests/source-head.txt"
        git -C "$XKCNSU_ROOT" diff --binary HEAD >"$XKCNSU_OUT/manifests/source-tracked.patch"
        git -C "$XKCNSU_ROOT" status --short >"$XKCNSU_OUT/manifests/source-status.txt"
        printf '%s\n' "$input_state" >"$work/.xkcnsu-input-state"
        printf '%s\n' "$input_state" >"$XKCNSU_OUT/manifests/source-state.txt"
        printf 'Release snapshot: %s\n' "$work"
        ;;
    manager-sync)
        [[ -d "$work/.git" ]] || xkcnsu_die "Release snapshot is missing."
        xkcnsu_run rsync -a --delete --exclude=build --exclude=.gradle --exclude=.kotlin \
            --exclude=.cxx --exclude=local.properties --exclude=libksud.so \
            "$XKCNSU_ROOT/manager/" "$work/manager/"
        printf 'Manager preview sources refreshed; real daemon retained.\n'
        ;;
    ksuinit|ksud)
        [[ -d "$work/.git" ]] || xkcnsu_die "Release snapshot is missing."
        cd "$work"
        export KSU_PACKAGE_NAME="$XKCNSU_PACKAGE_NAME"
        export LIBCLANG_PATH="${LIBCLANG_PATH:-$(llvm-config --libdir)}"
        for abi in $abis; do
            case "$abi" in
                arm64-v8a) triple=aarch64-linux-android; arch=aarch64 ;;
                x86_64) triple=x86_64-linux-android; arch=x86_64 ;;
                *) xkcnsu_die "ABI $abi needs its own pinned build-std toolchain; use arm64-v8a or x86_64." ;;
            esac
            # shellcheck source=/dev/null
            source <(sed 's/\r$//' "$work/.github/scripts/setup-rust-build.sh") "$triple" "$XKCNSU_ANDROID_API"
            if [[ "$stage" == ksuinit ]]; then
                builtins="$("$CLANG_PATH" --print-resource-dir)/lib/linux/libclang_rt.builtins-$arch-android.a"
                [[ -f "$builtins" ]] || xkcnsu_die "Missing compiler runtime: $builtins"
                export RUSTFLAGS="-C target-feature=+crt-static -C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wno-unused-command-line-argument -C link-arg=$builtins"
                xkcnsu_run cargo build --locked --package ksuinit --target "$triple" --release
                unset RUSTFLAGS
                mkdir -p "$work/userspace/ksud/bin/$arch" "$XKCNSU_OUT/ksuinit/$abi"
                cp "$CARGO_TARGET_DIR/$triple/release/ksuinit" "$work/userspace/ksud/bin/$arch/ksuinit"
                cp "$CARGO_TARGET_DIR/$triple/release/ksuinit" "$XKCNSU_OUT/ksuinit/$abi/ksuinit"
            else
                [[ -s "$XKCNSU_OUT/ksuinit/$abi/ksuinit" ]] ||
                    xkcnsu_die "Build ksuinit before ksud."
                cp "$XKCNSU_OUT/ksuinit/$abi/ksuinit" "$work/userspace/ksud/bin/$arch/ksuinit"
                if [[ "$abi" == arm64-v8a ]]; then
                    for asset in kpimg kptools; do
                        [[ -s "$XKCNSU_OUT/kpatch/aarch64/$asset" ]] || xkcnsu_die "Missing source-built $asset."
                        cp "$XKCNSU_OUT/kpatch/aarch64/$asset" "$work/userspace/ksud/bin/aarch64/$asset"
                    done
                fi
                module_dir="$XKCNSU_OUT/$arch"
                for module in "$module_dir/"*_kernelsu.ko "$module_dir/"*_susfs_guard_lkm.ko; do
                    [[ -f "$module" ]] || continue
                    cp "$module" "$work/userspace/ksud/bin/$arch/"
                done
                if [[ "$abi" == arm64-v8a ]]; then
                    [[ -s "$work/userspace/ksud/bin/$arch/android14-6.1_kernelsu.ko" ]] ||
                        xkcnsu_die "The priority android14-6.1 root LKM is missing."
                    [[ -s "$work/userspace/ksud/bin/$arch/android14-6.1_susfs_guard_lkm.ko" ]] ||
                        xkcnsu_die "The priority android14-6.1 SUSFS LKM is missing."
                    if [[ "${XKCNSU_REQUIRE_FULL_MATRIX:-1}" == 1 ]]; then
                        xkcnsu_run python3 "$XKCNSU_ROOT/tools/build-verify-modules.py"
                    else
                        xkcnsu_run python3 "$XKCNSU_ROOT/tools/build-verify-modules.py" \
                            --kmi android14-6.1 --kmi android12-5.10
                    fi
                fi
                python3 - "$work/userspace/ksud/bin/$arch" "$XKCNSU_OUT/manifests/embedded-assets-$arch.json" <<'PY'
import hashlib, json, pathlib, sys
directory, output = map(pathlib.Path, sys.argv[1:])
assets = []
for path in sorted(directory.iterdir()):
    if path.is_file() and (path.suffix == ".ko" or path.name in ("ksuinit", "kpimg", "kptools")):
        data = path.read_bytes()
        assets.append({"name": path.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(assets, indent=2) + "\n")
PY
                # rust-embed compression caches directory assets; force a new derive expansion.
                xkcnsu_run cargo clean --package ksud --target "$triple" --release
                xkcnsu_run cargo build --locked --package ksud --target "$triple" --release
                mkdir -p "$work/manager/app/src/main/jniLibs/$abi" "$XKCNSU_OUT/ksud/$abi"
                cp "$CARGO_TARGET_DIR/$triple/release/ksud" "$work/manager/app/src/main/jniLibs/$abi/libksud.so"
                cp "$CARGO_TARGET_DIR/$triple/release/ksud" "$XKCNSU_OUT/ksud/$abi/ksud"
            fi
        done
        ;;
    manager|manager-tests|manager-lint|manager-assemble)
        export GRADLE_USER_HOME="${XKCNSU_MANAGER_GRADLE_HOME:-$XKCNSU_CACHE/gradle-build}"
        export XKCNSU_KOTLIN_MAVEN="$XKCNSU_CACHE/kotlin-maven"
        [[ -d "$work/.git" ]] || xkcnsu_die "Release snapshot is missing."
        for abi in $abis; do
            [[ -s "$work/manager/app/src/main/jniLibs/$abi/libksud.so" ]] ||
                xkcnsu_die "Build ksud for $abi before Manager."
        done
        [[ -f "$XKCNSU_OUT/private/certificate.json" ]] || bash "$XKCNSU_ROOT/tools/build-signing.sh"
        python3 "$XKCNSU_ROOT/tools/verify-signing-identity.py" \
            --metadata "$XKCNSU_OUT/private/certificate.json" --package "$XKCNSU_PACKAGE_NAME"
        signing_init="$XKCNSU_OUT/private/gradle-signing.init.gradle"
        python3 "$XKCNSU_ROOT/tools/signing_config.py" \
            --metadata "$XKCNSU_OUT/private/certificate.json" gradle-init --output "$signing_init"
        cd "$work/manager"
        sed -i 's/\r$//' gradlew
        xkcnsu_run bash "$XKCNSU_ROOT/tools/bootstrap-gradle.sh"
        mkdir -p "$GRADLE_USER_HOME"
        gradle_arguments=(
            --no-daemon --console=plain --max-workers=1
            -Pandroid.r8.maxWorkers=1 -Dorg.gradle.parallel=false
            '-Dorg.gradle.jvmargs=-Xmx3072m -XX:MaxMetaspaceSize=512m'
            -Pkotlin.compiler.execution.strategy=in-process
            -Pandroid.injected.testOnly=false
            --init-script "$signing_init"
            "-PKSU_PACKAGE_NAME=$XKCNSU_PACKAGE_NAME" -PKSU_NAME=XkcnSU
            "-PXKCNSU_ABIS=$abis"
        )
        case "$stage" in
            manager-tests) manager_tasks=(:app:testReleaseUnitTest) ;;
            manager-lint) manager_tasks=(:app:lintRelease) ;;
            manager-assemble) manager_tasks=(:app:assembleRelease) ;;
            manager) manager_tasks=(:app:testReleaseUnitTest :app:lintRelease :app:assembleRelease) ;;
        esac
        for manager_task in "${manager_tasks[@]}"; do
            xkcnsu_run bash ./gradlew "${gradle_arguments[@]}" "$manager_task"
        done
        if [[ "$stage" == manager || "$stage" == manager-assemble ]]; then
            mkdir -p "$XKCNSU_OUT/manager"
            # AGP 9.4 keeps the packaged APK in its artifact directory.
            if [[ -d app/build/intermediates/apk/release ]]; then
                mkdir -p app/build/outputs/apk/release
                find app/build/intermediates/apk/release -maxdepth 1 -name '*.apk' -type f \
                    -exec cp '{}' app/build/outputs/apk/release/ \;
            fi
            find app/build/outputs/apk/release -name '*.apk' -type f -exec cp '{}' "$XKCNSU_OUT/manager/" \;
        fi
        ;;
    repack)
        [[ -d "$work/.git" ]] || xkcnsu_die "Release snapshot is missing."
        python3 "$XKCNSU_ROOT/tools/verify-signing-identity.py" \
            --metadata "$XKCNSU_OUT/private/certificate.json" --package "$XKCNSU_PACKAGE_NAME"
        [[ -f "$work/.xkcnsu-input-state" && "$(cat "$work/.xkcnsu-input-state")" == source-ready ]] ||
            xkcnsu_die "Final repack requires a source-ready snapshot."
        # Repack in a temporary view with the existing helper's expected target layout.
        ln -sfn "$CARGO_TARGET_DIR" "$work/target"
        config="$XKCNSU_OUT/private/repack-config.json"
        python3 "$XKCNSU_ROOT/tools/signing_config.py" \
            --metadata "$XKCNSU_OUT/private/certificate.json" repack-config --output "$config" --abis "$abis"
        apk_name="$(python3 "$XKCNSU_ROOT/tools/signing_config.py" \
            --metadata "$XKCNSU_OUT/private/certificate.json" apk-name)"
        xkcnsu_run python3 "$XKCNSU_ROOT/tools/build-repack.py" "$work" repack --config "$config" --out-dir "$XKCNSU_OUT/manager"
        xkcnsu_run apksigner verify --verbose --print-certs "$XKCNSU_OUT/manager/$apk_name"
        xkcnsu_run zipalign -c -P 16 4 "$XKCNSU_OUT/manager/$apk_name"
        python3 "$XKCNSU_ROOT/tools/build-verify.py" "$XKCNSU_OUT/manager/$apk_name"
        python3 - "$work/version.properties" "$XKCNSU_OUT/manager" "$apk_name" <<'PY'
import pathlib, shutil, sys
version_file, directory = map(pathlib.Path, sys.argv[1:3])
version = dict(line.split("=", 1) for line in version_file.read_text().splitlines() if "=" in line)
name = version["XKCNSU_VERSION_NAME"]
code = version.get("XKCNSU_MANAGER_VERSION_CODE", version["XKCNSU_VERSION_CODE"])
target = directory / f"XKCNSU_{name}_{code}.apk"
shutil.copy2(directory / sys.argv[3], target)
print(f"Stable release APK: {target}")
PY
        ;;
    *) xkcnsu_die "Usage: $0 {snapshot --source-ready|snapshot --preview|manager-sync|ksuinit|ksud|manager|manager-tests|manager-lint|manager-assemble|repack}. Default ABIs: arm64-v8a x86_64." ;;
esac
xkcnsu_space
