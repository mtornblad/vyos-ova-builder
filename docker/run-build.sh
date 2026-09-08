#!/usr/bin/env bash

set -Eeuo pipefail

readonly WORKSPACE="/vyos"
readonly OWNERSHIP_MARKER="${WORKSPACE}/.vyos-ova-builder-owner"

if (( $# < 3 )); then
    printf 'Usage: %s HOST_UID HOST_GID COMMAND [ARG ...]\n' "$0" >&2
    exit 2
fi

readonly HOST_UID="$1"
readonly HOST_GID="$2"
shift 2

if [[ ! "$HOST_UID" =~ ^[0-9]+$ || ! "$HOST_GID" =~ ^[0-9]+$ ]]; then
    printf 'ERROR: HOST_UID and HOST_GID must be numeric\n' >&2
    exit 2
fi

cleanup() {
    local command_status=$?
    local cleanup_status=0
    trap - EXIT

    # Do not cross any filesystem that a failed live-build may have left
    # mounted below /vyos. Symlinks are changed rather than followed.
    sudo find "$WORKSPACE" -xdev \
        \( ! -uid "$HOST_UID" -o ! -gid "$HOST_GID" \) \
        -exec chown -h -- "${HOST_UID}:${HOST_GID}" {} + \
        || cleanup_status=$?

    if (( cleanup_status == 0 )); then
        printf '%s:%s\n' "$HOST_UID" "$HOST_GID" >"$OWNERSHIP_MARKER" \
            || cleanup_status=$?
    fi

    if (( cleanup_status != 0 )); then
        printf 'ERROR: Could not restore ownership below %s\n' "$WORKSPACE" >&2
        if (( command_status == 0 )); then
            command_status=$cleanup_status
        fi
    fi

    exit "$command_status"
}
trap cleanup EXIT

# Removing the marker before sudo starts makes an interrupted build detectable
# and lets the next invocation repair the workspace before trying to delete it.
rm -f -- "$OWNERSHIP_MARKER"
sudo -- "$@"
