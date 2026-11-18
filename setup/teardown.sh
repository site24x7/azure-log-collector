#!/usr/bin/env bash
# ============================================================
# Azure Diagnostic Logs Collection — Teardown Script
# Removes all infrastructure provisioned by setup.sh.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_skip()    { echo -e "${YELLOW}[SKIP]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_section() { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

# ── Parse flags ──────────────────────────────────────────────
CLEAN_DIAG_SETTINGS=false
for arg in "$@"; do
    case "$arg" in
        --clean-diag-settings) CLEAN_DIAG_SETTINGS=true ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--clean-diag-settings]"
            echo ""
            echo "Options:"
            echo "  --clean-diag-settings   Remove this collector's diagnostic setting"
            echo "                          (s247-diag-logs-<suffix>) from the resources in the"
            echo "                          configured subscriptions, when it sends to this"
            echo "                          deployment's storage"
            exit 0
            ;;
        *)
            log_error "Unknown argument: ${arg}"
            echo "Usage: $(basename "$0") [--clean-diag-settings]"
            exit 1
            ;;
    esac
done

# Read config.env as KEY=VALUE lines only (never run it as shell code).
# Known keys only; quotes around a value are removed; nothing is expanded.
CONFIG_KEYS=" SUBSCRIPTION_IDS SITE24X7_API_KEY SITE24X7_API_TOKEN SITE24X7_DOMAIN SITE24X7_BASE_URL RESOURCE_GROUP_NAME DEPLOYMENT_SUFFIX DEPLOYMENT_SUBSCRIPTION_ID FUNCTION_APP_NAME STORAGE_ACCOUNT_NAME FUNCTION_APP_REGION TIMER_SCHEDULE GENERAL_LOGTYPE_ENABLED UPDATE_CHECK_URL AUTO_UPDATE_ENABLED DIAG_SEED_REGION DIAG_STORAGE_SUFFIX "
read_config_file() {
    local file="$1" line key value n=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        n=$((n + 1))
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*(#.*)?$ ]] && continue
        if [[ ! "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
            log_error "config.env line ${n} is not KEY=VALUE"
            exit 1
        fi
        key="${BASH_REMATCH[2]}"
        value="${BASH_REMATCH[3]}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        elif [[ "$value" =~ ^([^#]*[^#[:space:]])?[[:space:]]+#.*$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        if [[ "$CONFIG_KEYS" != *" ${key} "* ]]; then
            log_skip "config.env line ${n}: unknown setting ${key} ignored"
            continue
        fi
        printf -v "$key" '%s' "$value"
    done < "$file"
}

# ── Load config ──────────────────────────────────────────────
load_config() {
    log_section "Loading Configuration"

    local config_file="${SCRIPT_DIR}/config.env"
    if [[ ! -f "$config_file" ]]; then
        log_error "config.env not found at ${config_file}"
        exit 1
    fi
    read_config_file "$config_file"
    log_ok "Loaded config.env"

    # Same rule as the ARM template: one resource group per suffix, the legacy group without one.
    default_rg="s247-diag-logs-rg"
    [[ -n "${DEPLOYMENT_SUFFIX:-}" ]] && default_rg="s247-diag-logs-${DEPLOYMENT_SUFFIX}-rg"
    RG="${RESOURCE_GROUP_NAME:-$default_rg}"

    if [[ -z "${SUBSCRIPTION_IDS:-}" ]]; then
        log_error "SUBSCRIPTION_IDS is not set in config.env"
        exit 1
    fi
    IFS=',' read -ra SUBS <<< "$SUBSCRIPTION_IDS"
    # The collector's resource group lives in the subscription setup.sh deployed to
    # (the first one), unless DEPLOYMENT_SUBSCRIPTION_ID says otherwise. Every
    # group/lock command names it, so the CLI's current subscription never matters.
    DEPLOY_SUB="$(echo "${DEPLOYMENT_SUBSCRIPTION_ID:-${SUBS[0]}}" | xargs)"
    # This collector's own diagnostic setting (same rule as the collector).
    SETTING_NAME="s247-diag-logs"
    [[ -n "${DEPLOYMENT_SUFFIX:-}" ]] && SETTING_NAME="s247-diag-logs-$(echo "$DEPLOYMENT_SUFFIX" | tr '[:upper:]' '[:lower:]')"

    # Check az CLI login
    if ! az account show &>/dev/null; then
        log_error "Azure CLI is not logged in. Run 'az login' first."
        exit 1
    fi
    log_ok "Azure CLI is authenticated"

    # Only a group this collector created (the template tags it) is unlocked or deleted.
    if az group show --name "$RG" --subscription "$DEPLOY_SUB" &>/dev/null; then
        local managed_by
        managed_by=$(az group show --name "$RG" --subscription "$DEPLOY_SUB" --query "tags.\"managed-by\"" -o tsv 2>/dev/null)
        if [[ "$managed_by" != "s247-diag-logs" ]]; then
            log_error "Resource group ${RG} in subscription ${DEPLOY_SUB} is not tagged managed-by=s247-diag-logs; not touching it."
            exit 1
        fi
        check_group_contents
    fi
    log_info "Resource group: ${RG} (subscription ${DEPLOY_SUB}); diagnostic setting: ${SETTING_NAME}"
}

# ── The group holds only the collector's resources and lock ──
# A group tag does not prove that everything in it is the collector's: a group chosen on the
# deployment form can also hold the customer's resources. Every resource must carry the collector's
# managed-by tag (and, when it names its collector in s247-collector, name a Function App in this
# group), or be what Azure adds by itself for a tagged Function App's Application Insights
# (its "Failure Anomalies - <app>-ai" rule and the "Application Insights Smart Detection" action
# group; a shell install names its Application Insights after the app). Only the collector's own
# locks (s247-lock-*) are allowed. Anything else stops here,
# before any lock is removed or anything is deleted.
check_group_contents() {
    local resources apps foreign locks
    resources=$(az resource list --resource-group "$RG" --subscription "$DEPLOY_SUB" -o json 2>/dev/null) || {
        log_error "Could not list the resources in ${RG}; not touching it."
        exit 1
    }
    apps=$(echo "$resources" | jq -r '[.[] | select((.type | ascii_downcase) == "microsoft.web/sites" and .tags["managed-by"] == "s247-diag-logs") | .name | ascii_downcase]')
    foreign=$(echo "$resources" | jq -r --argjson apps "$apps" '
        .[] | (.type | ascii_downcase) as $t | (.name | ascii_downcase) as $n
        | ((.tags // {})["s247-collector"] // "" | ascii_downcase) as $owner
        | select(.tags["managed-by"] != "s247-diag-logs"
                 or ($owner != "" and ($apps | index($owner)) == null))
        | select(($t == "microsoft.alertsmanagement/smartdetectoralertrules"
                  and ([$apps[] | ("failure anomalies - " + . + "-ai"), ("failure anomalies - " + .)] | index($n))) | not)
        | select(($t == "microsoft.insights/actiongroups" and $n == "application insights smart detection"
                  and ($apps | length) > 0) | not)
        | "\(.name) (\(.type))"')
    if [[ -n "$foreign" ]]; then
        log_error "Resource group ${RG} also holds resources that are not this collector's; nothing was changed:"
        echo "$foreign" | sed 's/^/    /'
        log_error "Move or delete them first, or remove the collector's own resources yourself."
        exit 1
    fi
    locks=$(az lock list --resource-group "$RG" --subscription "$DEPLOY_SUB" -o json 2>/dev/null | jq -r '.[].name | select(startswith("s247-lock-") | not)') || {
        log_error "Could not list the locks in ${RG}; not touching it."
        exit 1
    }
    if [[ -n "$locks" ]]; then
        log_error "Resource group ${RG} has locks the collector did not create; nothing was changed:"
        echo "$locks" | sed 's/^/    /'
        exit 1
    fi
    log_ok "Resource group ${RG} holds only this collector's resources"
}

# ── Remove the collector's lock in the resource group ────────
remove_locks() {
    log_section "Removing Resource Locks"

    # Check if resource group exists
    if ! az group show --name "$RG" --subscription "$DEPLOY_SUB" &>/dev/null; then
        log_skip "Resource group ${RG} does not exist — nothing to unlock"
        return
    fi

    local lock_ids
    # Only the collector's own locks (s247-lock-*): check_group_contents refused any other.
    lock_ids=$(az lock list --resource-group "$RG" --subscription "$DEPLOY_SUB" -o json | jq -r '.[] | select(.name | startswith("s247-lock-")) | .id')

    if [[ -z "$lock_ids" ]]; then
        log_skip "No locks found in resource group ${RG}"
        return
    fi

    local count=0
    while IFS= read -r lock_id; do
        [[ -z "$lock_id" ]] && continue
        local lock_name
        lock_name=$(basename "$lock_id")
        log_info "Removing lock: ${lock_name} ..."
        az lock delete --ids "$lock_id" -o none
        log_ok "Removed lock: ${lock_name}"
        count=$((count + 1))
    done <<< "$lock_ids"

    log_ok "Removed ${count} lock(s)"
}

# ── Clean up diagnostic settings on resources ────────────────
clean_diagnostic_settings() {
    log_section "Cleaning Diagnostic Settings"

    local total_removed=0
    local total_failed=0
    local total_skipped=0

    for sub in "${SUBS[@]}"; do
        sub="$(echo "$sub" | xargs)"
        log_info "Listing resources in subscription ${sub} ..."
        local resources
        resources=$(az resource list --subscription "$sub" -o json 2>/dev/null || echo "[]")
        local count
        count=$(echo "$resources" | jq 'length')
        log_info "Found ${count} resources in subscription ${sub}"

        for i in $(seq 0 $((count - 1))); do
            local id name
            id=$(echo "$resources" | jq -r ".[$i].id")
            name=$(echo "$resources" | jq -r ".[$i].name")

            # Only this collector's setting, and only when it sends to this
            # deployment's storage (another collector may use the same name).
            local destination
            destination=$(az monitor diagnostic-settings show --name "$SETTING_NAME" --resource "$id" \
                --query storageAccountId -o tsv 2>/dev/null || true)
            local dest_lc own_prefix
            dest_lc=$(echo "$destination" | tr '[:upper:]' '[:lower:]')
            own_prefix=$(echo "/subscriptions/${DEPLOY_SUB}/resourcegroups/${RG}/providers/microsoft.storage/storageaccounts/" | tr '[:upper:]' '[:lower:]')
            if [[ -n "$destination" && "$dest_lc" != "$own_prefix"* ]]; then
                log_skip "${name}: ${SETTING_NAME} sends to another deployment's storage; left in place"
                total_skipped=$((total_skipped + 1))
            elif [[ -n "$destination" ]]; then
                if az monitor diagnostic-settings delete \
                        --name "$SETTING_NAME" \
                        --resource "$id" \
                        -o none 2>/dev/null; then
                    log_ok "Removed diagnostic setting from ${name}"
                    total_removed=$((total_removed + 1))
                else
                    log_error "Failed to remove diagnostic setting from ${name}"
                    total_failed=$((total_failed + 1))
                fi
            else
                total_skipped=$((total_skipped + 1))
            fi

            # Progress indicator every 25 resources
            if (( (i + 1) % 25 == 0 )); then
                log_info "  Processed $((i + 1))/${count} resources ..."
            fi
        done
    done

    log_section "Diagnostic Settings Cleanup Summary"
    echo -e "  Removed: ${GREEN}${total_removed}${NC}"
    echo -e "  Skipped: ${YELLOW}${total_skipped}${NC}"
    echo -e "  Failed:  ${RED}${total_failed}${NC}"
}

# ── Remove role assignments for the managed identity ─────────
delete_role_assignments() {
    local assignment_ids="$1" aid
    while IFS= read -r aid; do
        [[ -z "$aid" ]] && continue
        log_info "Deleting role assignment: $(basename "$aid")"
        if az role assignment delete --ids "$aid" -o none 2>/dev/null; then
            count=$((count + 1))
        else
            log_error "Failed to delete role assignment ${aid}"
        fi
    done <<< "$assignment_ids"
}

remove_collector_role_assignments() {
    log_section "Removing Collector Role Assignments"

    local rg_id sites
    if ! rg_id=$(az group show --name "$RG" --subscription "$DEPLOY_SUB" --query id -o tsv 2>/dev/null); then
        log_skip "Resource group ${RG} does not exist — skipping role assignment cleanup"
        return
    fi
    if ! sites=$(az functionapp list --resource-group "$RG" --subscription "$DEPLOY_SUB" \
        --query "[?identity.principalId!=null && tags.\"managed-by\"=='s247-diag-logs'].[identity.principalId,id]" -o tsv 2>/dev/null); then
        log_error "Could not list the collectors' managed identities. Check for leftover subscription assignments."
        return
    fi
    if [[ -z "$sites" ]]; then
        log_skip "No managed identities found in ${RG} — nothing to clean up"
        return
    fi

    local count=0 pid site_id sub scope assignment_ids
    while IFS=$'\t' read -r pid site_id; do
        for sub in "${SUBS[@]}"; do
            sub="$(echo "$sub" | xargs)"
            scope="/subscriptions/${sub}"
            if assignment_ids=$(az role assignment list \
                --assignee-object-id "$pid" --fill-principal-name false --fill-role-definition-name false \
                --subscription "$sub" --scope "$scope" --query "[?scope=='${scope}'].id" -o tsv 2>/dev/null); then
                delete_role_assignments "$assignment_ids"
            else
                log_error "Could not list subscription assignments for ${pid} on ${sub}."
            fi
        done
        # Look only at the collector's own RG and site. Any scoped grants still
        # present when Azure deletes the group are removed with those resources.
        if assignment_ids=$(az role assignment list --subscription "$DEPLOY_SUB" \
            --assignee-object-id "$pid" --fill-principal-name false --scope "$site_id" \
            --include-inherited --query "[?(scope=='${rg_id}' && (contains(['Storage Account Contributor', 'Locks Contributor'], roleDefinitionName) || starts_with(roleDefinitionName, 'Site24x7 Collector RG Delete v1'))) || (scope=='${site_id}' && roleDefinitionName=='Website Contributor')].id" \
            -o tsv 2>/dev/null); then
            delete_role_assignments "$assignment_ids"
        else
            log_skip "Could not list scoped assignments for ${pid}; Azure removes them with the group."
        fi
    done <<< "$sites"

    log_ok "Removed ${count} role assignment(s)"
}

# ── Delete the resource group ────────────────────────────────
delete_resource_group() {
    log_section "Deleting Resource Group"

    if ! az group show --name "$RG" --subscription "$DEPLOY_SUB" &>/dev/null; then
        log_skip "Resource group ${RG} does not exist — nothing to delete"
        return
    fi

    log_info "Deleting resource group ${RG} (--no-wait) ..."
    az group delete --name "$RG" --subscription "$DEPLOY_SUB" --yes --no-wait
    log_ok "Resource group ${RG} deletion initiated (async)"
    log_info "It may take a few minutes for Azure to fully remove all resources."
    log_info "Check status: az group show --name ${RG} --query properties.provisioningState -o tsv"
}

# ── Summary ──────────────────────────────────────────────────
print_summary() {
    log_section "Teardown Summary"
    echo -e "  Resource Group:            ${GREEN}${RG}${NC} (deletion initiated)"
    if [[ "$CLEAN_DIAG_SETTINGS" == "true" ]]; then
        echo -e "  Diagnostic Settings:       ${GREEN}Cleaned${NC}"
    else
        echo -e "  Diagnostic Settings:       ${YELLOW}Not cleaned${NC} (use --clean-diag-settings)"
    fi
    echo -e "  Resource Locks:            ${GREEN}Removed${NC}"
    echo "  Shared custom role definition retained: Site24x7 Collector RG Delete v1 (one per subscription)"
    echo ""
    log_ok "Teardown complete! 🧹"
}

# ============================================================
# MAIN
# ============================================================
main() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  Azure Diagnostic Logs Collection — Teardown    ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"

    load_config
    remove_locks

    if [[ "$CLEAN_DIAG_SETTINGS" == "true" ]]; then
        clean_diagnostic_settings
    else
        log_info "Skipping diagnostic settings cleanup (pass --clean-diag-settings to enable)"
    fi

    remove_collector_role_assignments
    delete_resource_group
    print_summary
}

main "$@"
