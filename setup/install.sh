#!/usr/bin/env bash
# ============================================================
# Azure Diagnostic Logs Collection — Bootstrap Script
#
# One-command entry point for deploying the diagnostic logs
# collection pipeline. Handles everything:
#   1. Downloads or locates the deployment package
#   2. Extracts and enters the project directory
#   3. Lists Azure subscriptions and lets you pick
#   4. Generates config.env automatically
#   5. Runs setup.sh
#
# Usage:
#   bash install.sh [URL]
#
#   URL  (optional) — Direct download link for the zip package.
#        If omitted, looks for diagnostic-logs-collection.zip
#        in the current directory.
# ============================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_section() { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

ZIP_NAME="diagnostic-logs-collection.zip"
PROJECT_DIR="diagnostic-logs-collection"

# ============================================================
# 1. GET THE PACKAGE
# ============================================================
get_package() {
    log_section "Package Setup"

    local url="${1:-}"

    if [[ -n "$url" ]]; then
        log_info "Downloading package from URL ..."
        if command -v curl &>/dev/null; then
            curl -fSL -o "$ZIP_NAME" "$url"
        elif command -v wget &>/dev/null; then
            wget -q -O "$ZIP_NAME" "$url"
        else
            log_error "Neither curl nor wget found. Cannot download."
            exit 1
        fi
        log_ok "Downloaded ${ZIP_NAME}"
    elif [[ -f "$ZIP_NAME" ]]; then
        log_ok "Found ${ZIP_NAME} in current directory"
    else
        log_error "No URL provided and ${ZIP_NAME} not found in current directory."
        echo ""
        echo "Usage:"
        echo "  bash install.sh <download-url>"
        echo "  bash install.sh                  # if zip is already present"
        exit 1
    fi

    # Extract
    if [[ -d "$PROJECT_DIR" ]]; then
        log_info "Removing existing ${PROJECT_DIR}/ directory ..."
        rm -rf "$PROJECT_DIR"
    fi
    unzip -qo "$ZIP_NAME"
    log_ok "Extracted to ${PROJECT_DIR}/"
}

# ============================================================
# 2. CHECK PREREQUISITES
# ============================================================
check_prereqs() {
    log_section "Prerequisites"

    for cmd in az jq zip unzip; do
        if ! command -v "$cmd" &>/dev/null; then
            log_error "${cmd} is required but not installed."
            exit 1
        fi
    done
    log_ok "Required tools available (az, jq, zip, unzip)"

    # Check Azure login
    if ! az account list --output tsv &>/dev/null; then
        log_error "Azure CLI is not logged in."
        log_info "Run 'az login' first, then re-run this script."
        exit 1
    fi
    log_ok "Azure CLI is authenticated"
}

# ============================================================
# 3. SUBSCRIPTION SELECTION
# ============================================================
select_subscriptions() {
    log_section "Subscription Selection"

    log_info "Fetching your Azure subscriptions ..."
    local subs_json
    subs_json=$(az account list --query '[].{name:name, id:id, state:state}' -o json 2>/dev/null)

    local count
    count=$(echo "$subs_json" | jq 'length')

    if [[ "$count" -eq 0 ]]; then
        log_error "No Azure subscriptions found. Check your login."
        exit 1
    fi

    echo ""
    echo -e "${BOLD}  #   Subscription Name                                    Subscription ID${NC}"
    echo "  ──  ──────────────────────────────────────────────────  ────────────────────────────────────────"

    for i in $(seq 0 $((count - 1))); do
        local name id state
        name=$(echo "$subs_json" | jq -r ".[$i].name")
        id=$(echo "$subs_json" | jq -r ".[$i].id")
        state=$(echo "$subs_json" | jq -r ".[$i].state")
        printf "  %-3s %-54s  %s" "$((i + 1))" "$name" "$id"
        if [[ "$state" != "Enabled" ]]; then
            echo -e "  ${YELLOW}(${state})${NC}"
        else
            echo ""
        fi
    done

    echo ""
    echo -e "${CYAN}Enter the numbers of subscriptions to monitor (comma-separated).${NC}"
    echo -e "${CYAN}Example: 1,3,5  or  2${NC}"
    echo ""
    read -rp "  Your selection: " selection

    # Parse selection into subscription IDs
    SELECTED_SUB_IDS=""
    IFS=',' read -ra choices <<< "$selection"
    for choice in "${choices[@]}"; do
        choice=$(echo "$choice" | xargs)
        if [[ ! "$choice" =~ ^[0-9]+$ ]] || [[ "$choice" -lt 1 ]] || [[ "$choice" -gt "$count" ]]; then
            log_error "Invalid selection: ${choice}. Must be between 1 and ${count}."
            exit 1
        fi
        local idx=$((choice - 1))
        local sub_id
        sub_id=$(echo "$subs_json" | jq -r ".[$idx].id")
        local sub_name
        sub_name=$(echo "$subs_json" | jq -r ".[$idx].name")
        if [[ -n "$SELECTED_SUB_IDS" ]]; then
            SELECTED_SUB_IDS="${SELECTED_SUB_IDS},${sub_id}"
        else
            SELECTED_SUB_IDS="$sub_id"
        fi
        log_ok "Selected: ${sub_name} (${sub_id})"
    done

    if [[ -z "$SELECTED_SUB_IDS" ]]; then
        log_error "No subscriptions selected."
        exit 1
    fi
}

# ============================================================
# 4. CONFIGURE
# ============================================================
configure() {
    log_section "Configuration"

    local config_file="${PROJECT_DIR}/setup/config.env"

    # Ask for optional Site24x7 token
    echo ""
    echo -e "${CYAN}Enter your Site24x7 API token (press Enter to skip for now):${NC}"
    read -rp "  Token: " s247_token
    s247_token="${s247_token:-your-token-here}"

    # Ask for Site24x7 data center
    echo ""
    echo -e "${CYAN}Select your Site24x7 data center:${NC}"
    echo "  1) US     (site24x7.com)"
    echo "  2) India  (site24x7.in)"
    echo "  3) EU     (site24x7.eu)"
    echo "  4) AU     (site24x7.net.au)"
    echo "  5) China  (site24x7.cn)"
    echo "  6) Japan  (site24x7.jp)"
    read -rp "  Selection [1]: " dc_choice
    dc_choice="${dc_choice:-1}"
    case "$dc_choice" in
        1) s247_domain="site24x7.com" ;;
        2) s247_domain="site24x7.in" ;;
        3) s247_domain="site24x7.eu" ;;
        4) s247_domain="site24x7.net.au" ;;
        5) s247_domain="site24x7.cn" ;;
        6) s247_domain="site24x7.jp" ;;
        *) s247_domain="site24x7.com" ;;
    esac
    log_ok "Data center: ${s247_domain}"

    # Ask for region
    echo ""
    echo -e "${CYAN}Enter Azure region for infrastructure (default: eastus):${NC}"
    read -rp "  Region: " region
    region="${region:-eastus}"

    # Generate config.env
    cat > "$config_file" <<EOF
# Auto-generated by install.sh
SUBSCRIPTION_IDS="${SELECTED_SUB_IDS}"
SITE24X7_API_TOKEN="${s247_token}"
SITE24X7_DOMAIN="${s247_domain}"
RESOURCE_GROUP_NAME="s247-diag-logs-rg"
FUNCTION_APP_NAME=""
STORAGE_ACCOUNT_NAME=""
FUNCTION_APP_REGION="${region}"
TIMER_SCHEDULE="0 0 */6 * * *"
GENERAL_LOGTYPE_ENABLED="true"
UPDATE_CHECK_URL=""
EOF

    log_ok "Generated config.env"
    echo ""
    echo -e "  ${BOLD}Subscriptions:${NC}  ${SELECTED_SUB_IDS}"
    echo -e "  ${BOLD}Region:${NC}         ${region}"
    echo -e "  ${BOLD}Site24x7:${NC}       $([ "$s247_token" = "your-token-here" ] && echo "stub mode" || echo "configured") (${s247_domain})"
    echo ""
}

# ============================================================
# 5. RUN SETUP
# ============================================================
run_setup() {
    log_section "Running Setup"

    log_info "Starting infrastructure provisioning and deployment ..."
    echo ""

    cd "$PROJECT_DIR/setup"
    bash setup.sh
}

# ============================================================
# MAIN
# ============================================================
main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  Azure Diagnostic Logs Collection — Installer       ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"

    check_prereqs
    get_package "${1:-}"
    select_subscriptions
    configure
    run_setup
}

main "$@"
