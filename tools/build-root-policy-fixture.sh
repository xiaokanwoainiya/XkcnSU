#!/usr/bin/env bash
# Standalone acceptance APK: no Gradle, product keys, release staging or device calls.
set -Eeuo pipefail
umask 077

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="$root/tools/fixtures/root-policy"
output="$root/out/test-harness/root-policy"
sdk=/opt/xkcnsu/android-sdk
build_tools="$sdk/build-tools/37.0.0"
android_jar="$sdk/platforms/android-37.0/android.jar"
if [[ ! -f "$android_jar" ]]; then
    android_jar="$sdk/platforms/android-37/android.jar"
fi

for tool in javac jar java keytool zip sha256sum; do
    command -v "$tool" >/dev/null || { printf 'Missing tool: %s\n' "$tool" >&2; exit 1; }
done
for tool in aapt2 d8 zipalign apksigner; do
    [[ -x "$build_tools/$tool" ]] || { printf 'Missing SDK tool: %s\n' "$tool" >&2; exit 1; }
done
[[ -f "$android_jar" ]] || { printf 'Missing Android platform 37 android.jar\n' >&2; exit 1; }

mkdir -p "$output"
work="$(mktemp -d "$output/build.XXXXXXXX")"
key_directory="$(mktemp -d)"
cleanup() {
    rm -f -- "$key_directory/testkey.p12" "$key_directory/password"
    rmdir -- "$key_directory"
}
trap cleanup EXIT
mkdir "$work/classes" "$work/dex"

javac -J-Xmx256m --release 11 -encoding UTF-8 -Xlint:all -Werror \
    -classpath "$android_jar" -d "$work/classes" \
    "$fixture/src/io/xkcnsu/acceptance/rootpolicy/ProbeActivity.java" \
    "$fixture/src/io/xkcnsu/acceptance/rootpolicy/ProbeReceiver.java"
jar --create --file "$work/classes.jar" -C "$work/classes" .
"$build_tools/d8" -JXmx256M --release --min-api 31 \
    --lib "$android_jar" --output "$work/dex" "$work/classes.jar"
"$build_tools/aapt2" link -I "$android_jar" \
    --manifest "$fixture/AndroidManifest.xml" -o "$work/unsigned.apk"
zip -q -j "$work/unsigned.apk" "$work/dex/classes.dex"
"$build_tools/zipalign" -P 16 -f 4 "$work/unsigned.apk" "$work/aligned.apk"

od -An -N32 -tx1 /dev/urandom | tr -d ' \n' > "$key_directory/password"
keytool -J-Xmx128m -genkeypair -noprompt -storetype PKCS12 \
    -keystore "$key_directory/testkey.p12" -alias root-policy-test \
    -keyalg RSA -keysize 2048 -sigalg SHA256withRSA -validity 30 \
    -dname 'CN=XKCNSU Root Test,OU=Acceptance Only' \
    -storepass:file "$key_directory/password" -keypass:file "$key_directory/password"
"$build_tools/apksigner" -JXmx256M sign \
    --ks "$key_directory/testkey.p12" --ks-key-alias root-policy-test \
    --ks-pass "file:$key_directory/password" \
    --v1-signing-enabled false --v2-signing-enabled true \
    --v3-signing-enabled true --v4-signing-enabled false \
    --out "$work/XKCNSU-Root-Test.apk" "$work/aligned.apk"
"$build_tools/apksigner" -JXmx256M verify --verbose --print-certs \
    "$work/XKCNSU-Root-Test.apk" > "$work/signature.txt"
"$build_tools/zipalign" -c -P 16 4 "$work/XKCNSU-Root-Test.apk"
"$build_tools/aapt2" dump badging "$work/XKCNSU-Root-Test.apk" > "$work/apk-info.txt"
"$build_tools/aapt2" dump permissions "$work/XKCNSU-Root-Test.apk" > "$work/permissions.txt"
"$build_tools/aapt2" dump xmltree "$work/XKCNSU-Root-Test.apk" \
    --file AndroidManifest.xml > "$work/manifest.txt"
if grep -q 'uses-permission' "$work/permissions.txt"; then
    printf 'Unexpected permission in the test fixture\n' >&2
    exit 1
fi
for artifact in XKCNSU-Root-Test.apk signature.txt apk-info.txt permissions.txt manifest.txt; do
    cp -- "$work/$artifact" "$output/$artifact"
done
(cd "$output" && sha256sum XKCNSU-Root-Test.apk > XKCNSU-Root-Test.apk.sha256)
printf 'APK: %s/XKCNSU-Root-Test.apk\n' "$output"
printf 'Signature and alignment verified. The one-use test private key is discarded.\n'
