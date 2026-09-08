#!/bin/vbash

export TERM=linux
export PAGER=cat
export VYATTA_PAGER=cat

readonly SCRIPT_PATH="/usr/local/sbin/vyos-vapp-init"
readonly SCRIPT_TEMPLATE="/opt/vyatta/etc/functions/script-template"
readonly MARKER_DIR="/opt/vyos-ova-builder"
readonly MARKER_FILE="${MARKER_DIR}/vapp-configured"
readonly BASE_INTERFACE="eth0"

# VyOS configuration scripts must run as root with vyattacfg as their primary
# group. This also makes direct execution from the postconfig hook safe.
if (( EUID != 0 )); then
    printf 'ERROR: %s must be run as root\n' "$SCRIPT_PATH" >&2
    builtin exit 1
fi
if [[ "$(id -g -n)" != "vyattacfg" ]]; then
    if [[ ! -x /usr/bin/sg ]]; then
        printf 'ERROR: /usr/bin/sg is not available\n' >&2
        builtin exit 1
    fi
    exec /usr/bin/sg vyattacfg -c "/bin/vbash ${SCRIPT_PATH}"
fi

exec > >(logger -t vyos-vapp-init) 2>&1

log() {
    printf '%s\n' "$*"
}

fail() {
    log "ERROR: $*"
    builtin exit 1
}

log "Starting VyOS vApp initialization."

if [[ -e "$MARKER_FILE" ]]; then
    log "VMware vApp configuration has already been applied; nothing to do."
    builtin exit 0
fi

if [[ ! -r "$SCRIPT_TEMPLATE" ]]; then
    fail "VyOS script template is missing: ${SCRIPT_TEMPLATE}"
fi

# Some VyOS releases return a non-zero status after sourcing this helper even
# though the required aliases have been installed. Do not use errexit here.
log "Loading VyOS configuration functions."
source "$SCRIPT_TEMPLATE"

for required_command in configure set delete commit save discard; do
    if ! type "$required_command" >/dev/null 2>&1; then
        fail "VyOS script template did not provide: ${required_command}"
    fi
done
SET_COMMAND_TYPE="$(type -t set || true)"
if [[ -z "$SET_COMMAND_TYPE" || "$SET_COMMAND_TYPE" == "builtin" ]]; then
    fail "VyOS script template did not replace the Bash set builtin"
fi
log "VyOS configuration functions loaded."

# script-template exposes a VyOS command named "set". Always use the Bash
# builtin explicitly for shell options. We deliberately avoid errexit here and
# check every configuration command so failures produce a useful log message.
builtin set -o nounset
builtin set -o pipefail

cleanup() {
    local exit_status=$?

    # Avoid recursively invoking this trap when cleanup has completed.
    trap - EXIT

    if cli-shell-api inSession; then
        if (( exit_status != 0 )); then
            discard >/dev/null 2>&1 || true
        fi
        cli-shell-api teardownSession >/dev/null 2>&1 || true
    fi

    builtin exit "$exit_status"
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

    element="$(
        printf '%s' "$OVF_ENV" \
            | sed 's/></>\n</g' \
            | grep -F "oe:key=\"guestinfo.${key}\"" \
            | head -n 1 \
            || true
    )"
    value="$(printf '%s' "$element" | sed -n 's/.*oe:value="\([^"]*\)".*/\1/p')"
    xml_decode "$value"
}

is_true() {
    case "${1,,}" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

prefix_from_netmask() {
    local netmask="$1"
    local prefix

    if [[ "$netmask" =~ ^([0-9]|[12][0-9]|3[0-2])$ ]]; then
        printf '%s' "$netmask"
        return 0
    fi

    if ! prefix="$(python3 -c '
import ipaddress
import sys

try:
    print(ipaddress.IPv4Network(f"0.0.0.0/{sys.argv[1]}").prefixlen)
except (ipaddress.NetmaskValueError, ipaddress.AddressValueError):
    raise SystemExit(1)
' "$netmask")"; then
        fail "guestinfo.netmask must be a prefix length or dotted IPv4 netmask"
    fi
    printf '%s' "$prefix"
}

log "Reading VMware guestinfo vApp environment."
OVF_ENV="$(vmtoolsd --cmd 'info-get guestinfo.ovfEnv' 2>/dev/null || true)"
if [[ -z "$OVF_ENV" || "$OVF_ENV" == *"No value found"* || "$OVF_ENV" != *"<Environment"* ]]; then
    fail "VMware Tools did not return a valid vApp environment"
fi

HOSTNAME_VALUE="$(get_ovf_property hostname)"
PASSWORD_VALUE="$(get_ovf_property password)"
IP_ADDRESS_VALUE="$(get_ovf_property ipaddress)"
NETMASK_VALUE="$(get_ovf_property netmask)"
GATEWAY_VALUE="$(get_ovf_property gateway)"
DNS_VALUE="$(get_ovf_property dns)"
DOMAIN_VALUE="$(get_ovf_property domain)"
NTP_VALUE="$(get_ovf_property ntp)"
VLAN_VALUE="$(get_ovf_property vlan)"
SSH_VALUE="$(get_ovf_property ssh)"

HOSTNAME_VALUE="${HOSTNAME_VALUE:-vyos}"
NETMASK_VALUE="${NETMASK_VALUE:-24}"

if [[ -n "$VLAN_VALUE" ]]; then
    if [[ ! "$VLAN_VALUE" =~ ^[0-9]+$ ]] || (( 10#$VLAN_VALUE < 1 || 10#$VLAN_VALUE > 4094 )); then
        fail "guestinfo.vlan must be empty or between 1 and 4094"
    fi
fi
if [[ -n "$GATEWAY_VALUE" && ( -z "$IP_ADDRESS_VALUE" || "${IP_ADDRESS_VALUE,,}" == "dhcp" ) ]]; then
    fail "guestinfo.gateway requires a static guestinfo.ipaddress"
fi

log "Opening VyOS configuration session."
configure || fail "VyOS configuration session could not be opened"
log "VyOS configuration session opened."

log "Applying guestinfo.hostname as '${HOSTNAME_VALUE}'."
set system host-name "$HOSTNAME_VALUE" \
    || fail "VyOS rejected guestinfo.hostname '${HOSTNAME_VALUE}'"
log "Hostname configuration accepted."

INTERFACE_PATH=(interfaces ethernet "$BASE_INTERFACE")
if [[ -n "$VLAN_VALUE" ]]; then
    log "Configuring management on ${BASE_INTERFACE}.${VLAN_VALUE}."
    delete interfaces ethernet "$BASE_INTERFACE" address || true
    INTERFACE_PATH+=(vif "$VLAN_VALUE")
else
    log "Configuring management on ${BASE_INTERFACE}."
fi

delete "${INTERFACE_PATH[@]}" address || true
if [[ -z "$IP_ADDRESS_VALUE" || "${IP_ADDRESS_VALUE,,}" == "dhcp" ]]; then
    set "${INTERFACE_PATH[@]}" address dhcp \
        || fail "VyOS rejected DHCP on the management interface"
else
    PREFIX_VALUE="$(prefix_from_netmask "$NETMASK_VALUE")"
    set "${INTERFACE_PATH[@]}" address "${IP_ADDRESS_VALUE}/${PREFIX_VALUE}" \
        || fail "VyOS rejected the management IP address"
    if [[ -n "$GATEWAY_VALUE" ]]; then
        set protocols static route 0.0.0.0/0 next-hop "$GATEWAY_VALUE" \
            || fail "VyOS rejected guestinfo.gateway"
    fi
fi

if [[ -n "$DOMAIN_VALUE" ]]; then
    set system domain-name "$DOMAIN_VALUE" \
        || fail "VyOS rejected guestinfo.domain"
fi

if [[ -n "$DNS_VALUE" ]]; then
    DNS_SERVERS=()
    read -r -a DNS_SERVERS <<<"${DNS_VALUE//,/ }"
    for DNS_SERVER in "${DNS_SERVERS[@]}"; do
        set system name-server "$DNS_SERVER" \
            || fail "VyOS rejected a guestinfo.dns server"
    done
fi

if [[ -n "$NTP_VALUE" ]]; then
    NTP_SERVERS=()
    read -r -a NTP_SERVERS <<<"${NTP_VALUE//,/ }"
    for NTP_SERVER in "${NTP_SERVERS[@]}"; do
        set service ntp server "$NTP_SERVER" \
            || fail "VyOS rejected a guestinfo.ntp server"
    done
fi

if [[ -n "$PASSWORD_VALUE" ]]; then
    log "Setting the vyos account password."
    set system login user vyos authentication plaintext-password "$PASSWORD_VALUE" \
        || fail "VyOS rejected guestinfo.password"
fi

if is_true "$SSH_VALUE"; then
    set service ssh port 22 || fail "VyOS rejected guestinfo.ssh"
else
    delete service ssh || true
fi

log "Committing first-boot configuration."
commit || fail "VyOS configuration could not be committed"
log "VyOS configuration committed."

log "Saving first-boot configuration."
save || fail "VyOS configuration could not be saved"
log "VyOS configuration saved."

install -d -m 0700 "$MARKER_DIR"
touch "$MARKER_FILE"
log "VMware vApp configuration completed successfully."
builtin exit 0
