#!/usr/bin/env bash
# Prefetch the Gradle wrapper and, for Manager builds, the pinned Kotlin compiler.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log gradle
xkcnsu_space
read -r filename checksum destination < <(python3 - "$XKCNSU_ROOT/manager/gradle/wrapper/gradle-wrapper.properties" "$GRADLE_USER_HOME" <<'PY'
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
print(filename, properties["distributionSha256Sum"], destination)
PY
)
[[ "$checksum" =~ ^[a-f0-9]{64}$ ]] || xkcnsu_die "Invalid Gradle wrapper checksum."
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

archive_matches() {
    printf '%s  %s\n' "$checksum" "$1" | sha256sum --check --status 2>/dev/null
}

download_checked() {
    xkcnsu_run curl --silent --show-error --fail --location --proto '=https' \
        --proto-redir '=https' --connect-timeout 15 --max-time 120 --retry 1 \
        "$@" -o "$partial" && archive_matches "$partial"
}

download_release() {
    local version="${filename#gradle-}" asset_id
    version="${version%-*.zip}"
    asset_id="$(curl --silent --show-error --fail --connect-timeout 15 --max-time 30 \
        "https://api.github.com/repos/gradle/gradle-distributions/releases/tags/v$version" |
        python3 -c 'import json, sys
for asset in json.load(sys.stdin)["assets"]:
    if asset["name"] == sys.argv[1] and asset.get("digest") == "sha256:" + sys.argv[2]:
        print(asset["id"])
        break' "$filename" "$checksum")" || return 1
    [[ "$asset_id" =~ ^[0-9]+$ ]] || return 1
    download_checked -H 'Accept: application/octet-stream' \
        "https://api.github.com/repos/gradle/gradle-distributions/releases/assets/$asset_id?download=$(date +%s)"
}

if ! archive_matches "$archive"; then
    (
        partial="$(mktemp "$archive.XXXXXX")"
        trap 'rm -f -- "$partial"' EXIT
        if ! download_release &&
            ! download_checked "https://downloads.gradle.org/distributions/$filename" &&
            ! download_checked "https://mirrors.cloud.tencent.com/gradle/$filename"; then
            xkcnsu_die "Could not download a Gradle distribution matching the pinned checksum."
        fi
        mv -- "$partial" "$archive"
    )
fi
printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check
if [[ ! -f "$destination/$filename.ok" ]]; then
    xkcnsu_run unzip -q -o "$archive" -d "$destination"
    touch "$destination/$filename.ok"
fi
printf '%s\n' "$checksum" > "$XKCNSU_OUT/toolchain/$filename.sha256"
printf 'Pinned Gradle wrapper cached: %s\n' "$destination"

if [[ -n "${XKCNSU_KOTLIN_MAVEN:-}" ]]; then
    (
        coordinate="org/jetbrains/kotlin/kotlin-compiler-embeddable/2.4.20"
        maven_dir="$XKCNSU_KOTLIN_MAVEN/$coordinate"
        mkdir -p "$maven_dir"
        # Keep the original POM so Gradle still resolves the same transitive dependencies.
        for extension in pom jar; do
            artifact="kotlin-compiler-embeddable-2.4.20.$extension"
            case "$extension" in
                pom) checksum=269d98cc4c68670c11e4aeb404e1c7c6f31b782321908d7268ba25cd236d59b2 ;;
                jar) checksum=cf97161430683fb9af96dc6a7017ffae1fe8737b80d12eee11f2fe3e76f8ded8 ;;
            esac
            archive="$maven_dir/$artifact"
            if ! archive_matches "$archive"; then
                partial="$(mktemp "$archive.XXXXXX")"
                trap 'rm -f -- "$partial"' EXIT
                if ! download_checked "https://maven-central.storage-download.googleapis.com/maven2/$coordinate/$artifact" &&
                    ! download_checked "https://repo1.maven.org/maven2/$coordinate/$artifact" &&
                    ! download_checked "https://repo.maven.apache.org/maven2/$coordinate/$artifact"; then
                    xkcnsu_die "Could not download $artifact with the pinned checksum."
                fi
                mv -- "$partial" "$archive"
            fi
            printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check
        done
    )
fi
xkcnsu_space
