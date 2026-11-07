#!/usr/bin/env bash
# Create once, then reuse one signing identity for Manager and kernel builds.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log signing
umask 077
private="$XKCNSU_OUT/private"
mkdir -p "$private"
exec {signing_lock}>"$private/signing.lock"
flock "$signing_lock"
profile="${XKCNSU_SIGNING_PROFILE:-release}"
promote=false
compact=false
case "${1:-}" in
    "") ;;
    --promote-development) promote=true ;;
    --compact-certificate) compact=true ;;
    *) xkcnsu_die "Usage: build-signing.sh [--promote-development|--compact-certificate]" ;;
esac
case "$profile" in
    release) stem=xkcnsu-release; password_name=release-password ;;
    development) stem=xkcnsu-dev; password_name=keystore-password ;;
    *) xkcnsu_die "XKCNSU_SIGNING_PROFILE must be release or development." ;;
esac
[[ "$promote" == false || "$profile" == release ]] ||
    xkcnsu_die "Development-key promotion requires the release signing profile."
[[ "$compact" == false || "$profile" == development ]] ||
    xkcnsu_die "Release certificates must retain their exact signing identity."
store="$private/$stem.p12"
password_file="$private/$password_name"
alias_name="$stem"
staging="$(mktemp -d "$private/.signing.XXXXXXXX")"
cleanup() {
    rm -f -- "$staging/"*
    rmdir -- "$staging"
}
trap cleanup EXIT
if [[ -e "$store" || -e "$password_file" ]]; then
    [[ -s "$store" && -s "$password_file" ]] ||
        xkcnsu_die "Keystore or password is missing; existing signing material was retained."
else
    if [[ "$promote" == true ]]; then
        [[ -s "$private/xkcnsu-dev.p12" && -s "$private/keystore-password" ]] ||
            xkcnsu_die "The existing XKCNSU development key and its password are required."
    elif [[ "$profile" == release && -e "$private/xkcnsu-dev.p12" ]]; then
        xkcnsu_die "An existing Manager identity is present. Use --promote-development to retain upgrade and kernel authentication."
    fi
    openssl rand -hex 32 >"$staging/password"
    if [[ "$promote" == true ]]; then
        keytool -importkeystore -noprompt -srckeystore "$private/xkcnsu-dev.p12" \
            -srcstoretype PKCS12 -srcalias xkcnsu-dev \
            -srcstorepass:file "$private/keystore-password" \
            -destkeystore "$staging/store.p12" -deststoretype PKCS12 \
            -destalias "$alias_name" -deststorepass:file "$staging/password" \
            -destkeypass:file "$staging/password"
        keytool -exportcert -keystore "$private/xkcnsu-dev.p12" -alias xkcnsu-dev \
            -storepass:file "$private/keystore-password" -file "$staging/original.der"
        keytool -exportcert -keystore "$staging/store.p12" -alias "$alias_name" \
            -storepass:file "$staging/password" -file "$staging/promoted.der"
        cmp "$staging/original.der" "$staging/promoted.der" ||
            xkcnsu_die "Promotion changed the certificate; existing signing material was retained."
    else
        keytool -genkeypair -noprompt -storetype PKCS12 -keystore "$staging/store.p12" \
            -alias "$alias_name" -keyalg RSA -keysize 3072 -sigalg SHA256withRSA \
            -validity 10000 -dname 'CN=XKCNSU' \
            -storepass:file "$staging/password" -keypass:file "$staging/password"
    fi
    mv "$staging/store.p12" "$store"
    mv "$staging/password" "$password_file"
fi
keytool -exportcert -keystore "$store" -alias "$alias_name" \
    -storepass:file "$password_file" -file "$staging/certificate.der"
if [[ "$promote" == true ]]; then
    [[ -s "$private/xkcnsu-dev.p12" && -s "$private/keystore-password" ]] ||
        xkcnsu_die "The existing XKCNSU development identity is required to verify promotion."
    keytool -exportcert -keystore "$private/xkcnsu-dev.p12" -alias xkcnsu-dev \
        -storepass:file "$private/keystore-password" -file "$staging/expected-original.der"
    cmp "$staging/expected-original.der" "$staging/certificate.der" ||
        xkcnsu_die "The existing release certificate differs from the development identity; signing material was retained."
fi
if [[ "$compact" == true && $(stat -c%s "$staging/certificate.der") -gt 1024 ]]; then
    before="$(openssl x509 -inform DER -in "$staging/certificate.der" -pubkey -noout |
        openssl pkey -pubin -outform DER | sha256sum)"
    if [[ ! -e "$private/$stem-oversize-original.p12" ]]; then
        cp "$store" "$private/$stem-oversize-original.p12"
        cp "$staging/certificate.der" "$private/$stem-oversize-original.der"
    fi
    keytool -selfcert -keystore "$store" -alias "$alias_name" \
        -dname 'CN=XKCNSU' -validity 10000 -sigalg SHA256withRSA \
        -storepass:file "$password_file" -keypass:file "$password_file"
    keytool -exportcert -keystore "$store" -alias "$alias_name" \
        -storepass:file "$password_file" -file "$staging/certificate.der"
    after="$(openssl x509 -inform DER -in "$staging/certificate.der" -pubkey -noout |
        openssl pkey -pubin -outform DER | sha256sum)"
    [[ "$before" == "$after" ]] || xkcnsu_die "Public key changed during certificate compaction."
fi
openssl x509 -inform DER -in "$staging/certificate.der" -out "$staging/certificate.pem"
mv "$staging/certificate.der" "$private/$stem.der"
mv "$staging/certificate.pem" "$private/$stem.pem"
python3 - "$private" "$XKCNSU_PACKAGE_NAME" "$XKCNSU_ROOT/kernel/manager/apk_sign.c" \
    "$profile" "$stem" "$password_name" <<'PY'
import hashlib, json, os, pathlib, re, sys
private = pathlib.Path(sys.argv[1])
profile, stem, password_name = sys.argv[4:7]
cert = (private / (stem + ".der")).read_bytes()
metadata = {
    "package_name": sys.argv[2],
    "key_alias": stem,
    "certificate_der": str(private / (stem + ".der")),
    "keystore": str(private / (stem + ".p12")),
    "password_file": str(private / password_name),
    "signing_profile": profile,
    "certificate_size": len(cert),
    "KSU_PACKAGE_NAME": sys.argv[2],
    "KSU_EXPECTED_SIZE": f"0x{len(cert):04x}",
    "KSU_EXPECTED_HASH": hashlib.sha256(cert).hexdigest(),
    "development_only": profile != "release",
}
source = pathlib.Path(sys.argv[3]).read_text()
limit = re.search(r"#define\s+CERT_MAX_LENGTH\s+(\d+)", source)
if limit and len(cert) > int(limit.group(1)):
    raise SystemExit(
        f"Existing key retained, but certificate is {len(cert)} bytes and kernel "
        f"CERT_MAX_LENGTH is {limit.group(1)}."
    )
output = private / "certificate.json"
temporary = output.with_suffix(".json.tmp")
temporary.write_text(json.dumps(metadata, indent=2) + "\n")
os.replace(temporary, output)
print(json.dumps({k: metadata[k] for k in (
    "KSU_PACKAGE_NAME", "KSU_EXPECTED_SIZE", "KSU_EXPECTED_HASH"
)}, indent=2))
PY
chmod 600 "$store" "$password_file" "$private/$stem.der" "$private/$stem.pem" "$private/certificate.json"
printf 'Signing profile: %s\n' "$profile"
printf 'Certificate metadata: %s\n' "$private/certificate.json"
