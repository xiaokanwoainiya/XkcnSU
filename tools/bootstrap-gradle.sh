#!/usr/bin/env bash
# Prefetch the exact Gradle wrapper distribution without configuring Manager.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log gradle
xkcnsu_space
read -r filename destination < <(python3 - "$XKCNSU_ROOT/manager/gradle/wrapper/gradle-wrapper.properties" "$GRADLE_USER_HOME" <<'PY'
import hashlib, pathlib, sys
properties = dict(line.strip().split("=", 1) for line in pathlib.Path(sys.argv[1]).read_text().splitlines() if "=" in line)
url = properties["distributionUrl"].replace("\\:", ":")
filename = url.rsplit("/", 1)[1]
number = int.from_bytes(hashlib.md5(url.encode()).digest(), "big")
digest = ""
while number:
    number, digit = divmod(number, 36)
    digest = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + digest
destination = pathlib.Path(sys.argv[2]) / "wrapper/dists" / filename.removesuffix(".zip") / digest
print(filename, destination)
PY
)
mkdir -p "$XKCNSU_CACHE/downloads" "$destination" "$XKCNSU_OUT/toolchain"
# Hashed artifacts are immutable; reuse their bytes without sharing mutable cache locks.
if [[ "$GRADLE_USER_HOME" != "$XKCNSU_CACHE/gradle" && \
      -d "$XKCNSU_CACHE/gradle/caches/modules-2/files-2.1" && \
      ! -d "$GRADLE_USER_HOME/caches/modules-2/files-2.1" ]]; then
    mkdir -p "$GRADLE_USER_HOME/caches/modules-2"
    xkcnsu_run cp -al "$XKCNSU_CACHE/gradle/caches/modules-2/files-2.1" \
        "$GRADLE_USER_HOME/caches/modules-2/"
fi
archive="$XKCNSU_CACHE/downloads/$filename"
checksum_file="$XKCNSU_CACHE/downloads/$filename.sha256"
xkcnsu_run curl --silent --show-error --fail --location --retry 3 \
    "https://downloads.gradle.org/distributions/$filename.sha256" -o "$checksum_file"
checksum="$(awk '{print $1}' "$checksum_file")"
[[ "$checksum" =~ ^[a-f0-9]{64}$ ]] || xkcnsu_die "Invalid Gradle checksum."
if ! printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status 2>/dev/null; then
    xkcnsu_run curl --silent --show-error --fail --location --retry 3 --continue-at - \
        "https://downloads.gradle.org/distributions/$filename" -o "$archive"
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check
if [[ ! -f "$destination/$filename.ok" ]]; then
    xkcnsu_run unzip -q -o "$archive" -d "$destination"
    touch "$destination/$filename.ok"
fi
cp "$checksum_file" "$XKCNSU_OUT/toolchain/$filename.sha256"
printf 'Pinned Gradle wrapper cached: %s\n' "$destination"
xkcnsu_space
