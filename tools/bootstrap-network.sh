#!/usr/bin/env bash
# Optional workaround for a broken regional GitHub DNS route; TLS remains verified.
set -Eeuo pipefail
# shellcheck source=build-common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/build-common.sh"
xkcnsu_freeze "$@"
xkcnsu_log network
resolve="${1:?Usage: bootstrap-network.sh github.com:443:VERIFIED_IP}"
[[ "$resolve" =~ ^github\.com:443:[0-9.]+$ ]] || xkcnsu_die "Only a GitHub IPv4 route is accepted."
xkcnsu_run curl --silent --show-error --fail --head --connect-timeout 10 --max-time 20 \
    --resolve "$resolve" https://github.com/rust-lang/libc
git config --file "$XKCNSU_CACHE/gitconfig" http.curloptResolve "$resolve"
git config --file "$XKCNSU_CACHE/gitconfig" http.lowSpeedLimit 1024
git config --file "$XKCNSU_CACHE/gitconfig" http.lowSpeedTime 30
printf 'Optional TLS-verified GitHub route saved to %s\n' "$XKCNSU_CACHE/gitconfig"
