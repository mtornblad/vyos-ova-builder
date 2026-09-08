#!/bin/vbash

export TERM=linux
export PAGER=cat
export VYATTA_PAGER=cat

readonly SCRIPT_PATH="/usr/local/sbin/vyos-vapp-init"
readonly SCRIPT_TEMPLATE="/opt/vyatta/etc/functions/script-template"
readonly CONFIG_PARSER="/usr/local/libexec/vyos-ova-parse-config"
readonly INTERFACE_RESOLVER="/usr/local/libexec/vyos-ova-resolve-interface"
readonly MARKER_DIR="/opt/vyos-ova-builder"
readonly MARKER_FILE="${MARKER_DIR}/vapp-configured"
readonly CONFIG_ARCHIVE_DIR="/opt/vyatta/etc/config/archive"
readonly COMMIT_LOG_FILE="${CONFIG_ARCHIVE_DIR}/commits"
readonly DEFAULT_MANAGEMENT_INTERFACE="eth0"
readonly MANAGEMENT_INTERFACE_TOKEN="__MANAGEMENT_INTERFACE__"
readonly TRUNK_INTERFACE_TOKEN="__TRUNK_INTERFACE__"
readonly REST_API_ID="automation"

PARSED_CONFIG_FILE=""
MANAGEMENT_INTERFACE="$DEFAULT_MANAGEMENT_INTERFACE"
TRUNK_INTERFACE=""

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

normalize_config_archive_permissions() {
    # A commit executed as root can leave the revision log owned by root:root.
    # Keep the stock VyOS group contract so later interactive commits made by
    # members of vyattacfg can update the log.
    mkdir -p -- "$CONFIG_ARCHIVE_DIR" \
        || fail "Could not create the VyOS configuration archive directory"
    chown root:vyattacfg "$CONFIG_ARCHIVE_DIR" \
        || fail "Could not set ownership on the VyOS configuration archive directory"
    chmod 2775 "$CONFIG_ARCHIVE_DIR" \
        || fail "Could not set permissions on the VyOS configuration archive directory"

    if [[ -e "$COMMIT_LOG_FILE" ]]; then
        chown root:vyattacfg "$COMMIT_LOG_FILE" \
            || fail "Could not set ownership on the VyOS commit log"
        chmod 0664 "$COMMIT_LOG_FILE" \
            || fail "Could not set permissions on the VyOS commit log"
    fi
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

    if [[ -n "$PARSED_CONFIG_FILE" ]]; then
        rm -f -- "$PARSED_CONFIG_FILE"
    fi

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

resolve_network_interface() {
    local property_name="$1"
    local network_name="$2"
    local fallback_interface="$3"
    local resolved_interface

    if [[ -z "$network_name" ]]; then
        printf '%s' "$fallback_interface"
        return 0
    fi
    if [[ ! -x "$INTERFACE_RESOLVER" ]]; then
        printf 'ERROR: VMware network interface resolver is missing: %s\n' \
            "$INTERFACE_RESOLVER" >&2
        return 1
    fi
    if ! resolved_interface="$(
        printf '%s' "$OVF_ENV" | "$INTERFACE_RESOLVER" "$network_name"
    )"; then
        return 1
    fi
    if [[ ! "$resolved_interface" =~ ^[A-Za-z0-9_.:-]+$ ]]; then
        printf 'ERROR: guestinfo.%s resolved to an unsafe interface name\n' \
            "$property_name" >&2
        return 1
    fi

    printf '%s' "$resolved_interface"
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

apply_extra_config() {
    local encoded_config="$1"
    local encoded_argument
    local decoded_argument
    local -a encoded_arguments
    local -a config_arguments
    local command_count=0

    if [[ -z "$encoded_config" ]]; then
        return 0
    fi
    if [[ ! -x "$CONFIG_PARSER" ]]; then
        fail "Supplemental configuration parser is missing: ${CONFIG_PARSER}"
    fi

    PARSED_CONFIG_FILE="$(mktemp /run/vyos-vapp-init.XXXXXX)" \
        || fail "Could not create supplemental configuration workspace"
    chmod 0600 "$PARSED_CONFIG_FILE" \
        || fail "Could not protect supplemental configuration workspace"

    if ! printf '%s' "$encoded_config" \
        | base64 --decode 2>/dev/null \
        | "$CONFIG_PARSER" >"$PARSED_CONFIG_FILE"; then
        fail "guestinfo.config_base64 is not valid supplemental configuration"
    fi

    while IFS=$'\t' read -r -a encoded_arguments; do
        if (( ${#encoded_arguments[@]} < 2 )); then
            fail "Supplemental configuration parser returned an invalid command"
        fi

        config_arguments=()
        for encoded_argument in "${encoded_arguments[@]}"; do
            if [[ "$encoded_argument" != x* ]] \
                || ! decoded_argument="$(
                    printf '%s' "${encoded_argument#x}" | base64 --decode 2>/dev/null
                )"; then
                fail "Supplemental configuration parser returned invalid data"
            fi

            case "$decoded_argument" in
                "$MANAGEMENT_INTERFACE_TOKEN")
                    decoded_argument="$MANAGEMENT_INTERFACE"
                    ;;
                "$TRUNK_INTERFACE_TOKEN")
                    if [[ -z "$TRUNK_INTERFACE" ]]; then
                        fail "Supplemental configuration uses ${TRUNK_INTERFACE_TOKEN}, but guestinfo.trunk_network is empty"
                    fi
                    decoded_argument="$TRUNK_INTERFACE"
                    ;;
                *"$MANAGEMENT_INTERFACE_TOKEN"*|*"$TRUNK_INTERFACE_TOKEN"*)
                    fail "Supplemental interface placeholders must be standalone arguments"
                    ;;
            esac
            config_arguments+=("$decoded_argument")
        done

        case "${config_arguments[0]}" in
            set)
                set "${config_arguments[@]:1}" \
                    || fail "VyOS rejected supplemental set command $((command_count + 1))"
                ;;
            delete)
                delete "${config_arguments[@]:1}" \
                    || fail "VyOS rejected supplemental delete command $((command_count + 1))"
                ;;
            *)
                fail "Supplemental configuration parser returned a forbidden command"
                ;;
        esac
        ((command_count += 1))
    done <"$PARSED_CONFIG_FILE"

    rm -f -- "$PARSED_CONFIG_FILE"
    PARSED_CONFIG_FILE=""
    log "Applied ${command_count} supplemental configuration command(s)."
}

apply_ssh_authorized_key() {
    local authorized_key="$1"
    local key_type
    local key_data
    local ignored_comment

    if [[ -z "$authorized_key" ]]; then
        return 0
    fi
    if [[ "$authorized_key" == *$'\n'* || "$authorized_key" == *$'\r'* ]]; then
        fail "guestinfo.ssh_authorized_key must contain one OpenSSH public key"
    fi

    read -r key_type key_data ignored_comment <<<"$authorized_key"
    case "$key_type" in
        ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-dss|ssh-ed25519|ssh-rsa)
            ;;
        *)
            fail "guestinfo.ssh_authorized_key uses an unsupported key type"
            ;;
    esac
    if [[ -z "$key_data" ]] \
        || ! printf '%s' "$key_data" | base64 --decode >/dev/null 2>&1; then
        fail "guestinfo.ssh_authorized_key contains invalid key data"
    fi

    log "Installing the bootstrap SSH public key for the vyos account."
    set system login user vyos authentication public-keys bootstrap type "$key_type" \
        || fail "VyOS rejected the guestinfo.ssh_authorized_key type"
    set system login user vyos authentication public-keys bootstrap key "$key_data" \
        || fail "VyOS rejected guestinfo.ssh_authorized_key"
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
ENABLE_SSH_VALUE="$(get_ovf_property enable_ssh)"
SSH_AUTHORIZED_KEY_VALUE="$(get_ovf_property ssh_authorized_key)"
ENABLE_REST_VALUE="$(get_ovf_property enable_rest)"
REST_API_KEY_VALUE="$(get_ovf_property rest_api_key)"
MANAGEMENT_NETWORK_VALUE="$(get_ovf_property management_network)"
TRUNK_NETWORK_VALUE="$(get_ovf_property trunk_network)"
CONFIG_BASE64_VALUE="$(get_ovf_property config_base64)"

HOSTNAME_VALUE="${HOSTNAME_VALUE:-vyos}"
NETMASK_VALUE="${NETMASK_VALUE:-24}"

if ! MANAGEMENT_INTERFACE="$(
    resolve_network_interface management_network \
        "$MANAGEMENT_NETWORK_VALUE" "$DEFAULT_MANAGEMENT_INTERFACE"
)"; then
    fail "guestinfo.management_network could not be mapped to a Linux interface"
fi
if ! TRUNK_INTERFACE="$(
    resolve_network_interface trunk_network "$TRUNK_NETWORK_VALUE" ""
)"; then
    fail "guestinfo.trunk_network could not be mapped to a Linux interface"
fi
if [[ -n "$TRUNK_INTERFACE" && "$TRUNK_INTERFACE" == "$MANAGEMENT_INTERFACE" ]]; then
    fail "Management and trunk networks resolve to the same Linux interface"
fi
if [[ -n "$MANAGEMENT_NETWORK_VALUE" ]]; then
    log "Mapped guestinfo.management_network to ${MANAGEMENT_INTERFACE}."
else
    log "Using default management interface ${MANAGEMENT_INTERFACE}."
fi
if [[ -n "$TRUNK_NETWORK_VALUE" ]]; then
    log "Mapped guestinfo.trunk_network to ${TRUNK_INTERFACE}."
fi

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

# Supplemental commands are applied first. Dedicated properties below are the
# stable appliance contract and therefore take precedence on conflicting paths.
apply_extra_config "$CONFIG_BASE64_VALUE"

log "Applying guestinfo.hostname as '${HOSTNAME_VALUE}'."
set system host-name "$HOSTNAME_VALUE" \
    || fail "VyOS rejected guestinfo.hostname '${HOSTNAME_VALUE}'"
log "Hostname configuration accepted."

INTERFACE_PATH=(interfaces ethernet "$MANAGEMENT_INTERFACE")
if [[ -n "$VLAN_VALUE" ]]; then
    log "Configuring management on ${MANAGEMENT_INTERFACE}.${VLAN_VALUE}."
    delete interfaces ethernet "$MANAGEMENT_INTERFACE" address || true
    INTERFACE_PATH+=(vif "$VLAN_VALUE")
else
    log "Configuring management on ${MANAGEMENT_INTERFACE}."
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

apply_ssh_authorized_key "$SSH_AUTHORIZED_KEY_VALUE"

if is_true "$ENABLE_SSH_VALUE"; then
    set service ssh port 22 || fail "VyOS rejected guestinfo.enable_ssh"
else
    delete service ssh || true
fi

# Like SSH, REST uses VyOS's default listen-address behavior. Deployments that
# need interface-specific exposure can constrain service ssh/service https in
# guestinfo.config_base64.
if is_true "$ENABLE_REST_VALUE"; then
    if [[ -z "$REST_API_KEY_VALUE" ]]; then
        fail "guestinfo.rest_api_key is required when guestinfo.enable_rest is enabled"
    fi
    log "Enabling the VyOS REST API."
    set service https api keys id "$REST_API_ID" key "$REST_API_KEY_VALUE" \
        || fail "VyOS rejected guestinfo.rest_api_key"
    set service https api rest \
        || fail "VyOS rejected guestinfo.enable_rest"
else
    delete service https api rest || true
    delete service https api keys id "$REST_API_ID" || true
fi

# Set the archive directory's set-group-ID bit before the root-owned bootstrap
# commit creates or rewrites its revision log.
normalize_config_archive_permissions

log "Committing first-boot configuration."
commit || fail "VyOS configuration could not be committed"
log "VyOS configuration committed."

log "Saving first-boot configuration."
save || fail "VyOS configuration could not be saved"
log "VyOS configuration saved."

# Normalize an existing log too, including images where an earlier boot or
# VyOS hook created it with root as both owner and group.
normalize_config_archive_permissions

install -d -m 0700 "$MARKER_DIR"
touch "$MARKER_FILE"
log "VMware vApp configuration completed successfully."
builtin exit 0
