#!/bin/vbash

set -o errexit
set -o nounset
set -o pipefail

export TERM=linux
export PAGER=cat
export VYATTA_PAGER=cat

readonly MARKER_DIR="/opt/vyos-ova-builder"
readonly MARKER_FILE="${MARKER_DIR}/vapp-configured"
COMMAND_FILE=""
ARGUMENT_FILE=""

exec > >(logger -t vyos-vapp-init) 2>&1

log() {
    printf '%s\n' "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

source /opt/vyatta/etc/functions/script-template

cleanup() {
    [[ -z "$COMMAND_FILE" ]] || rm -f -- "$COMMAND_FILE"
    [[ -z "$ARGUMENT_FILE" ]] || rm -f -- "$ARGUMENT_FILE"
    if cli-shell-api inSession; then
        discard >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

xml_decode() {
    local value="$1"
    value="${value//&quot;/\"}"
    value="${value//&apos;/\'}"
    value="${value//&lt;/<}"
    value="${value//&gt;/>}"
    value="${value//&amp;/&}"
    printf '%s' "$value"
}

get_ovf_property() {
    local key="$1"
    local element
    local value
    element="$(printf '%s' "$OVF_ENV" | sed 's/></>\n</g' | grep -F "oe:key=\"vapp.${key}\"" | head -n 1 || true)"
    value="$(printf '%s' "$element" | sed -n 's/.*oe:value="\([^"]*\)".*/\1/p')"
    xml_decode "$value"
}

get_property_with_legacy_name() {
    local current_key="$1"
    local legacy_key="$2"
    local value
    value="$(get_ovf_property "$current_key")"
    if [[ -z "$value" && -n "$legacy_key" ]]; then
        value="$(get_ovf_property "$legacy_key")"
    fi
    printf '%s' "$value"
}

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

apply_additional_commands() {
    local encoded="$1"
    local command
    local -a arguments
    local applied=0

    [[ -z "$encoded" ]] && return 0
    COMMAND_FILE="$(mktemp)"
    ARGUMENT_FILE="$(mktemp)"
    if ! printf '%s' "$encoded" | base64 --decode >"$COMMAND_FILE" 2>/dev/null; then
        fail "config_commands_base64 is not valid Base64"
    fi

    while IFS= read -r command || [[ -n "$command" ]]; do
        command="${command#"${command%%[![:space:]]*}"}"
        command="${command%"${command##*[![:space:]]}"}"
        [[ -z "$command" || "$command" == \#* ]] && continue

        if ! printf '%s\n' "$command" | python3 -c '
import shlex
import sys

try:
    arguments = shlex.split(sys.stdin.read(), comments=False, posix=True)
except ValueError as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)

for argument in arguments:
    sys.stdout.buffer.write(argument.encode("utf-8") + b"\0")
' >"$ARGUMENT_FILE"; then
            fail "additional configuration contains invalid quoting"
        fi

        arguments=()
        mapfile -d '' -t arguments <"$ARGUMENT_FILE"
        if [[ ${#arguments[@]} -eq 0 ]]; then
            continue
        fi
        case "${arguments[0]}" in
            set|delete|comment) ;;
            *)
                fail "additional configuration contains a command other than set, delete, or comment"
                ;;
        esac

        "${arguments[@]}"
        applied=$((applied + 1))
    done <"$COMMAND_FILE"

    rm -f -- "$COMMAND_FILE" "$ARGUMENT_FILE"
    COMMAND_FILE=""
    ARGUMENT_FILE=""
    log "Applied ${applied} additional VyOS configuration command(s)."
}

log "Waiting for VMware vApp properties."
OVF_ENV="$(vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>/dev/null || true)"
if [[ -z "$OVF_ENV" || "$OVF_ENV" == *"No value found"* ]]; then
    fail "VMware Tools did not return a vApp environment"
fi

HOSTNAME_VALUE="$(get_ovf_property hostname)"
MANAGEMENT_INTERFACE="$(get_ovf_property management_interface)"
MANAGEMENT_ADDRESS="$(get_property_with_legacy_name management_ipv4_address mgmt_ip)"
MANAGEMENT_PREFIX="$(get_property_with_legacy_name management_ipv4_prefix_length mgmt_mask)"
MANAGEMENT_GATEWAY="$(get_property_with_legacy_name management_ipv4_gateway mgmt_gw)"
VYOS_PASSWORD="$(get_ovf_property vyos_password)"
ENABLE_SSH="$(get_ovf_property enable_ssh)"
ENABLE_API="$(get_property_with_legacy_name enable_api enable_rest)"
API_KEY="$(get_property_with_legacy_name api_key rest_api_key)"
API_ALLOWED_NETWORK="$(get_ovf_property api_allowed_network)"
CONFIG_COMMANDS="$(get_property_with_legacy_name config_commands_base64 config_blob)"

HOSTNAME_VALUE="${HOSTNAME_VALUE:-vyos}"
MANAGEMENT_INTERFACE="${MANAGEMENT_INTERFACE:-eth0}"
MANAGEMENT_PREFIX="${MANAGEMENT_PREFIX:-24}"

if [[ -n "$MANAGEMENT_ADDRESS" && ! "$MANAGEMENT_PREFIX" =~ ^([0-9]|[12][0-9]|3[0-2])$ ]]; then
    fail "management_ipv4_prefix_length must be between 0 and 32"
fi
if [[ -z "$MANAGEMENT_ADDRESS" && -n "$MANAGEMENT_GATEWAY" ]]; then
    fail "management_ipv4_gateway requires a static management address"
fi
if is_true "$ENABLE_API" && [[ -z "$API_KEY" ]]; then
    fail "api_key is required when enable_api is true"
fi

while ! systemctl is-active --quiet vyos-router.service; do
    sleep 2
done
while cli-shell-api inSession; do
    log "Another VyOS configuration session is active; waiting."
    sleep 2
done

configure

set system host-name "$HOSTNAME_VALUE"

if [[ -n "$MANAGEMENT_ADDRESS" ]]; then
    log "Applying static management addressing to ${MANAGEMENT_INTERFACE}."
    delete interfaces ethernet "$MANAGEMENT_INTERFACE" address dhcp || true
    set interfaces ethernet "$MANAGEMENT_INTERFACE" address "${MANAGEMENT_ADDRESS}/${MANAGEMENT_PREFIX}"
    if [[ -n "$MANAGEMENT_GATEWAY" ]]; then
        set protocols static route 0.0.0.0/0 next-hop "$MANAGEMENT_GATEWAY"
    fi
else
    log "Using DHCP on ${MANAGEMENT_INTERFACE}."
    set interfaces ethernet "$MANAGEMENT_INTERFACE" address dhcp
fi

if is_true "$ENABLE_SSH"; then
    set service ssh port 22
else
    delete service ssh || true
fi

if is_true "$ENABLE_API"; then
    log "Enabling the VyOS HTTPS API."
    set service https api rest
    set service https api keys id vis key "$API_KEY"
    set service https port 443
    if [[ -n "$API_ALLOWED_NETWORK" ]]; then
        set service https allow-client address "$API_ALLOWED_NETWORK"
    fi
else
    delete service https api rest || true
    delete service https api keys id vis || true
fi

if [[ -n "$VYOS_PASSWORD" ]]; then
    log "Setting the vyos account password."
    set system login user vyos authentication plaintext-password "$VYOS_PASSWORD"
fi

apply_additional_commands "$CONFIG_COMMANDS"

log "Committing first-boot configuration."
commit || fail "VyOS rejected the generated configuration"
save || fail "VyOS configuration could not be saved"

install -d -m 0700 "$MARKER_DIR"
touch "$MARKER_FILE"
trap - EXIT
log "VMware vApp configuration completed successfully."
exit 0
