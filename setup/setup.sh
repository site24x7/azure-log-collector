#!/usr/bin/env bash
# ============================================================
# Azure Diagnostic Logs Collection — Setup Script
# Creates core infrastructure and deploys code + webapp.
# Resource discovery and diagnostic settings are handled by
# the Function App itself (via Manual Scan in the dashboard).
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$(mktemp "${TMPDIR:-/tmp}/s247-setup-$(date +%Y%m%d-%H%M%S)-XXXXXX")"

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*" | tee -a "$LOG_FILE"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*" | tee -a "$LOG_FILE"; }
log_skip()    { echo -e "${YELLOW}[SKIP]${NC}  $*" | tee -a "$LOG_FILE"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" | tee -a "$LOG_FILE"; }
log_section() { echo -e "\n${BOLD}═══ $* ═══${NC}" | tee -a "$LOG_FILE"; }
log_debug()   { echo -e "[DEBUG] $*" >> "$LOG_FILE"; }

# Run an az command with full error capture
run_az() {
    local description="$1"
    shift
    log_debug "Running Azure command: ${description}"
    # Only stdout is returned: az prints warnings on stderr, which must never end up in a
    # captured value (an ID, a key). Stderr goes to the log, and with the error on failure.
    local output errors err_file exit_code=0
    err_file=$(mktemp)
    output=$(az "$@" 2>"$err_file") || exit_code=$?
    errors=$(cat "$err_file")
    rm -f "$err_file"
    if [[ $exit_code -eq 0 ]]; then
        log_debug "Success: ${description}"
        [[ -n "$errors" ]] && log_debug "az stderr: ${errors}"
        echo "$output"
        return 0
    fi
    log_error "${description} failed (exit code: ${exit_code})"
    log_error "Azure operation: ${description}"
    log_error "Output: ${errors}${output:+ ${output}}"
    echo "$output"
    return $exit_code
}

# ============================================================
# 1. PRE-FLIGHT CHECKS
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

# ============================================================
preflight_checks() {
    log_section "Pre-flight Checks"

    local config_file="${SCRIPT_DIR}/config.env"
    if [[ ! -f "$config_file" ]]; then
        log_error "config.env not found at ${config_file}"
        log_error ""
        log_error "Quick setup:"
        log_error "  cp ${SCRIPT_DIR}/config.env.example ${SCRIPT_DIR}/config.env"
        log_error "  code ${SCRIPT_DIR}/config.env    # edit with Cloud Shell editor"
        exit 1
    fi
    read_config_file "$config_file"
    log_ok "Loaded config.env"

    # Defaults
    # Auto-generate unique suffix from subscription ID if names are defaults
    local unique_suffix
    unique_suffix=$(echo "${SUBSCRIPTION_IDS}" | md5sum 2>/dev/null || md5 -q -s "${SUBSCRIPTION_IDS}" 2>/dev/null || echo "$$")
    unique_suffix="${DEPLOYMENT_SUFFIX:-${unique_suffix:0:6}}"
    if [[ ! "$unique_suffix" =~ ^[a-z0-9]{1,13}$ ]]; then
        log_error "DEPLOYMENT_SUFFIX must contain 1–13 lowercase letters or digits"
        exit 1
    fi

    # Same rule as the ARM template: one resource group per suffix, the legacy group without one.
    default_rg="s247-diag-logs-rg"
    [[ -n "${DEPLOYMENT_SUFFIX:-}" ]] && default_rg="s247-diag-logs-${unique_suffix}-rg"
    RG="${RESOURCE_GROUP_NAME:-$default_rg}"
    FUNC_APP="${FUNCTION_APP_NAME:-s247-diag-func-${unique_suffix}}"
    STORAGE="${STORAGE_ACCOUNT_NAME:-s247diag${unique_suffix}}"
    REGION="${FUNCTION_APP_REGION:-eastus}"
    TIMER="${TIMER_SCHEDULE:-0 0 */6 * * *}"
    GENERAL_LOGTYPE="${GENERAL_LOGTYPE_ENABLED:-false}"

    log_info "Resource Group:    ${RG}"
    log_info "Function App:      ${FUNC_APP}"
    log_info "Storage Account:   ${STORAGE}"
    log_info "Region:            ${REGION}"

    # Check required tools
    for cmd in az jq zip python3; do
        if ! command -v $cmd &>/dev/null; then
            log_error "$cmd is required but not installed"
            exit 1
        fi
    done
    log_ok "Required tools available (az, jq, zip, python3)"

    # Check az CLI login
    if ! az account list --output tsv &>/dev/null; then
        log_error "Azure CLI is not logged in. Run 'az login' first."
        exit 1
    fi
    log_ok "Azure CLI is authenticated"

    # Validate SUBSCRIPTION_IDS
    if [[ -z "${SUBSCRIPTION_IDS:-}" || "${SUBSCRIPTION_IDS}" == "sub-id-1,sub-id-2" || "${SUBSCRIPTION_IDS}" == "" ]]; then
        log_error "SUBSCRIPTION_IDS is not set in config.env"
        log_error ""
        log_error "To find your subscription IDs, run:"
        log_error "  az account list --query '[].{Name:name, ID:id}' -o table"
        exit 1
    fi
    IFS=',' read -ra SUBS <<< "$SUBSCRIPTION_IDS"
    log_ok "Found ${#SUBS[@]} subscription(s) to monitor"

    # Set the first subscription as the default context for all az commands
    local primary_sub
    primary_sub="$(echo "${SUBS[0]}" | xargs)"
    az account set --subscription "$primary_sub" 2>/dev/null || true
    log_ok "Set default subscription: ${primary_sub}"

    # Validate subscription access
    for sub in "${SUBS[@]}"; do
        sub="$(echo "$sub" | xargs)"
        if ! az account show --subscription "$sub" &>/dev/null; then
            log_error "Cannot access subscription: ${sub}"
            log_error "Available subscriptions:"
            az account list --query '[].{Name:name, ID:id}' -o table 2>/dev/null || true
            exit 1
        fi
    done
    log_ok "All subscriptions accessible"

    # Accept existing config.env files while standardizing the runtime setting.
    SITE24X7_API_KEY="${SITE24X7_API_KEY:-${SITE24X7_API_TOKEN:-}}"
    if [[ -z "$SITE24X7_API_KEY" || "$SITE24X7_API_KEY" == "your-token-here" || "$SITE24X7_API_KEY" == "your-device-key-here" ]]; then
        log_error "Set SITE24X7_API_KEY to your Site24x7 Device Key in config.env"
        exit 1
    fi

    # Keep the prior suffix inference for custom Function App names on upgrades.
    DIAG_SUFFIX="${DEPLOYMENT_SUFFIX:-$(echo "$FUNC_APP" | grep -oE '[a-z0-9]{6}$' || echo '000000')}"
    local sanitized_region
    sanitized_region=$(echo "$REGION" | tr -dc 'a-z0-9')
    if (( ${#DIAG_SUFFIX} > 6 )); then
        SEED_STORAGE="s247dr${DIAG_SUFFIX}"
        TENANT_STORAGE="s247dt${DIAG_SUFFIX}"
    else
        SEED_STORAGE="s247diag${sanitized_region}${DIAG_SUFFIX}"
        SEED_STORAGE="${SEED_STORAGE:0:24}"
        TENANT_STORAGE="s247diagtenant${DIAG_SUFFIX}"
    fi
    check_storage_name "$STORAGE"
    check_storage_name "$SEED_STORAGE"
    check_storage_name "$TENANT_STORAGE"
    log_ok "Resource names look good"
}

# Check global availability, allowing reuse only within this resource group.
check_storage_name() {
    local name="$1" available
    if ! available=$(az storage account check-name --name "$name" --query nameAvailable -o tsv); then
        log_error "Unable to check storage name '$name'; no infrastructure was provisioned."
        exit 1
    fi
    if [[ "$available" == "false" ]]; then
        if ! az storage account show --name "$name" --resource-group "$RG" -o none &>/dev/null; then
            log_error "Storage name '$name' is unavailable globally, even if not visible in your subscription."
            log_error "For a fresh install, set DEPLOYMENT_SUFFIX to a new 12–13 character lowercase alphanumeric value."
            log_error "For an existing deployment, keep its suffix and investigate ownership; do not rename it during an upgrade."
            exit 1
        fi
        log_skip "Storage account $name already exists in our RG"
    elif [[ "$available" != "true" ]]; then
        log_error "Unexpected name-availability response for '$name'; stopping before provisioning."
        exit 1
    fi
}

# ============================================================
# 2. PROVISION INFRASTRUCTURE
# ============================================================
provision_infrastructure() {
    log_section "Infrastructure Provisioning"

    # ── Resource Group ───────────────────────────────────────
    log_info "Creating resource group ${RG} in ${REGION} ..."
    # Same tags as the one-click template: both teardown paths only remove what carries them.
    run_az "Create resource group" group create --name "$RG" --location "$REGION" \
        --tags managed-by=s247-diag-logs purpose=diagnostic-logs-collection -o none
    log_ok "Resource group ${RG} ready"

    # ── Storage Account ──────────────────────────────────────
    log_info "Creating storage account ${STORAGE} ..."
    if az storage account show --name "$STORAGE" --resource-group "$RG" &>/dev/null; then
        log_skip "Storage account ${STORAGE} already exists"
    else
        run_az "Create storage account" storage account create \
            --name "$STORAGE" \
            --resource-group "$RG" \
            --location "$REGION" \
            --sku Standard_LRS \
            --tags managed-by=s247-diag-logs purpose=function-app-runtime \
            -o none
        log_ok "Created storage account ${STORAGE}"
    fi

    # ── Function App ─────────────────────────────────────────
    log_info "Creating Function App ${FUNC_APP} ..."
    if az functionapp show --name "$FUNC_APP" --resource-group "$RG" &>/dev/null; then
        # Only a Flex Consumption app on Python 3.13 can be reused. An app from an
        # earlier version (Linux Consumption) cannot be moved in place.
        local hosting
        hosting=$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" \
            --query "[(sku || properties.sku), (functionAppConfig || properties.functionAppConfig).runtime.name, (functionAppConfig || properties.functionAppConfig).runtime.version]" \
            -o tsv 2>/dev/null | tr '\t\n' '  ' | xargs)
        if [[ "$hosting" != "FlexConsumption python 3.13" ]]; then
            log_error "Function App ${FUNC_APP} exists but is not a Flex Consumption app on Python 3.13 (found: ${hosting:-unknown})."
            log_error "Azure cannot move an app to Flex Consumption in place. Deploy this collector again"
            log_error "with a new deployment suffix, then remove the old resource group with teardown.sh."
            exit 1
        fi
        log_skip "Function App ${FUNC_APP} already exists (Flex Consumption, Python 3.13)"
    else
        # Same hosting as the one-click template: Flex Consumption, Python 3.13.
        run_az "Create Function App" functionapp create \
            --name "$FUNC_APP" \
            --resource-group "$RG" \
            --storage-account "$STORAGE" \
            --flexconsumption-location "$REGION" \
            --runtime python \
            --runtime-version 3.13 \
            --instance-memory 2048 \
            --maximum-instance-count 10 \
            --tags managed-by=s247-diag-logs \
            -o none
        log_ok "Created Function App ${FUNC_APP} (Flex Consumption)"
    fi
    # Also on a re-run: functionapp create makes an Application Insights named after the app,
    # which is the collector's too, so it carries the same tag.
    local ai_id
    ai_id=$(az resource list --resource-group "$RG" --resource-type microsoft.insights/components \
        --query "[?name=='${FUNC_APP}'].id | [0]" -o tsv 2>/dev/null || true)
    if [[ -n "$ai_id" ]]; then
        run_az "Tag Application Insights" tag update --resource-id "$ai_id" --operation merge \
            --tags managed-by=s247-diag-logs -o none
    fi
    # ... and a Flex plan for the app alone, which the CLI does not tag. Only the app's own
    # plan, and only when it is in this group.
    local plan_id plan_prefix
    plan_id=$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" \
        --query "serverFarmId || properties.serverFarmId" -o tsv 2>/dev/null || true)
    plan_prefix=$(echo "/resourcegroups/${RG}/providers/microsoft.web/serverfarms/" | tr '[:upper:]' '[:lower:]')
    if [[ -n "$plan_id" && "$(echo "$plan_id" | tr '[:upper:]' '[:lower:]')" == *"$plan_prefix"* ]]; then
        # Its generated name does not carry the suffix: the owner tag names the collector.
        run_az "Tag Function App plan" tag update --resource-id "$plan_id" --operation merge \
            --tags managed-by=s247-diag-logs \
            s247-collector="$(echo "$FUNC_APP" | tr '[:upper:]' '[:lower:]')" -o none
    else
        log_error "Could not find the Function App's plan in ${RG}; tag it managed-by=s247-diag-logs and s247-collector=<Function App name> before uninstalling."
    fi

    # ── Managed Identity ─────────────────────────────────────
    log_info "Enabling system-assigned managed identity ..."
    local principal_id
    principal_id=$(run_az "Assign managed identity" functionapp identity assign \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --query principalId -o tsv)
    if [[ ! "$principal_id" =~ ^[0-9a-fA-F-]{36}$ ]]; then
        log_error "Could not read the managed identity's principal ID; no roles were assigned."
        exit 1
    fi
    log_ok "Managed identity enabled (principal: ${principal_id})"

    # ── RBAC Role Assignments ────────────────────────────────
    log_info "Assigning RBAC roles ..."
    local sub scope rg_id site_id hosting_sub definitions custom_role_id
    local custom_role
    hosting_sub=$(az account show --query id -o tsv)
    # Role names are unique per tenant: one CLI-created definition per subscription, named apart
    # from the template's ("... v1 (<sub>)"), so either install path can come first.
    custom_role="Site24x7 Collector RG Delete v1 CLI (${hosting_sub})"
    rg_id=$(az group show --subscription "$hosting_sub" --name "$RG" --query id -o tsv)
    site_id=$(az functionapp show --subscription "$hosting_sub" --resource-group "$RG" \
        --name "$FUNC_APP" --query id -o tsv)
    if [[ -z "$hosting_sub" || -z "$rg_id" || -z "$site_id" ]]; then
        log_error "Could not resolve the Azure scopes for the collector's required roles."
        exit 1
    fi
    if ! custom_role_id=$(ensure_rg_delete_role "$hosting_sub" "$custom_role"); then
        log_error "Setup stopped before code deployment. The installer needs Owner or Contributor plus User Access Administrator to create the custom role and assign roles."
        exit 1
    fi

    for sub in "${SUBS[@]}"; do
        sub="$(echo "$sub" | xargs)"
        scope="/subscriptions/${sub}"
        require_collector_role "$sub" "$scope" "$principal_id" "Reader" "Reader"
        require_collector_role "$sub" "$scope" "$principal_id" "Monitoring Contributor" "Monitoring Contributor"
        log_ok "Reader and Monitoring Contributor on subscription ${sub}"
    done

    definitions="/subscriptions/$hosting_sub/providers/Microsoft.Authorization/roleDefinitions"
    require_scoped_collector_role "$hosting_sub" "$rg_id" "$principal_id" \
        "$definitions/17d1049b-9a84-46fb-8f53-869881c3d3ab" "Storage Account Contributor"
    require_scoped_collector_role "$hosting_sub" "$site_id" "$principal_id" \
        "$definitions/de139f84-1756-47ae-9be6-808fbbe84772" "Website Contributor"
    require_scoped_collector_role "$hosting_sub" "$rg_id" "$principal_id" "$custom_role_id" "$custom_role"
    require_collector_role "$hosting_sub" "$rg_id" "$principal_id" \
        "$definitions/28bf596f-4eb7-45ce-b5bc-6cf482fec137" "Locks Contributor"
    log_ok "Storage Account Contributor, Locks Contributor and ${custom_role} on ${RG}; Website Contributor on ${FUNC_APP}"

    # ── Resource Locks ───────────────────────────────────────
    log_info "Applying CanNotDelete locks ..."

    az lock create --name "s247-lock-rg" \
        --resource-group "$RG" --lock-type CanNotDelete \
        -o none 2>/dev/null || true

    local storage_id
    storage_id=$(az storage account show --name "$STORAGE" --resource-group "$RG" --query id -o tsv)
    az lock create --name "s247-lock-storage" \
        --resource "$storage_id" --lock-type CanNotDelete \
        -o none 2>/dev/null || true

    local func_id
    func_id=$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" --query id -o tsv)
    az lock create --name "s247-lock-funcapp" \
        --resource "$func_id" --lock-type CanNotDelete \
        -o none 2>/dev/null || true

    log_ok "Locks applied on RG, Storage, and Function App"

    # ── Per-region Storage Account (seed region) ─────────────
    # The BlobLogProcessor polls per-region storage accounts for logs.
    # Create the initial storage account in the Function App's region.
    local sanitized_region
    sanitized_region=$(echo "$REGION" | tr -dc 'a-z0-9')
    local diag_suffix
    diag_suffix="$DIAG_SUFFIX"
    local sa_name="$SEED_STORAGE"

    log_info "Creating per-region storage account ${sa_name} in ${REGION} ..."
    if az storage account show --name "$sa_name" --resource-group "$RG" &>/dev/null; then
        log_skip "Storage account ${sa_name} already exists"
    else
        run_az "Create per-region storage account" storage account create \
            --name "$sa_name" \
            --resource-group "$RG" \
            --location "$REGION" \
            --sku Standard_LRS \
            --kind StorageV2 \
            --min-tls-version TLS1_2 \
            --allow-blob-public-access false \
            --tags managed-by=s247-diag-logs purpose=diag-logs-regional region="$REGION" \
            -o none
        log_ok "Created per-region storage account ${sa_name}"
    fi

    # Create insights-logs container
    log_info "Creating insights-logs container ..."
    az storage container create \
        --name "insights-logs" \
        --account-name "$sa_name" \
        --auth-mode login \
        -o none 2>/dev/null || true
    log_ok "Container 'insights-logs' ready"

    # Apply lock on per-region storage account
    local sa_id
    sa_id=$(az storage account show --name "$sa_name" --resource-group "$RG" --query id -o tsv)
    az lock create --name "s247-lock-sa-${sanitized_region}" \
        --resource "$sa_id" --lock-type CanNotDelete \
        -o none 2>/dev/null || true
    log_ok "Lock applied on per-region storage account"

    # ── Function App Settings ────────────────────────────────
    log_info "Configuring Function App settings ..."
    # Values go in a private file (mode 600), not on the command line, where the
    # Device Key would be visible to other local users in the process list.
    local settings_file
    settings_file=$(mktemp)
    chmod 600 "$settings_file"
    SITE24X7_API_KEY="${SITE24X7_API_KEY}" jq -n \
        --arg SUBSCRIPTION_IDS "${SUBSCRIPTION_IDS}" \
        --arg SITE24X7_BASE_URL "https://www.${SITE24X7_DOMAIN:-site24x7.com}" \
        --arg GENERAL_LOGTYPE_ENABLED "${GENERAL_LOGTYPE}" \
        --arg TIMER_SCHEDULE "${TIMER}" \
        --arg RESOURCE_GROUP_NAME "${RG}" \
        --arg DIAG_STORAGE_SUFFIX "${diag_suffix}" \
        --arg DIAG_SEED_REGION "${REGION}" \
        --arg UPDATE_CHECK_URL "${UPDATE_CHECK_URL:-}" \
        '{SUBSCRIPTION_IDS: $SUBSCRIPTION_IDS, SITE24X7_BASE_URL: $SITE24X7_BASE_URL,
          GENERAL_LOGTYPE_ENABLED: $GENERAL_LOGTYPE_ENABLED, TIMER_SCHEDULE: $TIMER_SCHEDULE,
          RESOURCE_GROUP_NAME: $RESOURCE_GROUP_NAME, PROCESSING_ENABLED: "true",
          DIAG_STORAGE_SUFFIX: $DIAG_STORAGE_SUFFIX,
          DIAG_SEED_REGION: $DIAG_SEED_REGION, UPDATE_CHECK_URL: $UPDATE_CHECK_URL,
          SITE24X7_API_KEY: env.SITE24X7_API_KEY}
         | to_entries | map({name: .key, value: .value, slotSetting: false})' > "$settings_file"
    if ! run_az "Set Function App settings" functionapp config appsettings set \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --settings "@${settings_file}" \
        -o none; then
        rm -f "$settings_file"
        return 1
    fi
    rm -f "$settings_file"
    log_ok "Function App settings configured"
}

# ============================================================
# 3. DEPLOY FUNCTION APP CODE
# ============================================================
deploy_function_app() {
    log_section "Function App Deployment"

    local func_src="${SCRIPT_DIR}/../function-app"
    # A private temp directory: a fixed /tmp name could be replaced by another
    # local user, or clash with a parallel deployment.
    local zip_dir zip_path
    zip_dir=$(mktemp -d)
    chmod 700 "$zip_dir"
    zip_path="${zip_dir}/s247-function-app.zip"
    # Removed when this function returns; the trap then clears itself, or it would fire
    # again on later returns with zip_dir unset (and fail the script at the end).
    trap 'rm -rf "$zip_dir"; trap - RETURN' RETURN

    if [[ ! -d "$func_src" ]]; then
        log_error "Function App source not found at ${func_src}"
        exit 1
    fi

    # Wait for Function App to be fully ready before deploying
    log_info "Waiting for Function App to be ready ..."
    local retries=0
    local max_retries=12
    while [[ $retries -lt $max_retries ]]; do
        local state
        state=$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" --query "state || properties.state" -o tsv 2>/dev/null || echo "Unknown")
        if [[ "$state" == "Running" ]]; then
            log_ok "Function App is ready (state: ${state})"
            break
        fi
        retries=$((retries + 1))
        log_info "Function App state: ${state} — waiting 10s (${retries}/${max_retries}) ..."
        sleep 10
    done
    if [[ $retries -eq $max_retries ]]; then
        log_error "Function App did not become ready after $((max_retries * 10))s"
        log_error "Check Azure Portal for details: https://portal.azure.com/#resource/subscriptions/$(echo "${SUBS[0]}" | xargs)/resourceGroups/${RG}/providers/Microsoft.Web/sites/${FUNC_APP}"
        exit 1
    fi

    # Create deployment package
    rm -f "$zip_path"
    (cd "$func_src" && zip -r "$zip_path" . -x "*.pyc" "__pycache__/*" ".venv/*" "local.settings.json" > /dev/null)
    local zip_size
    zip_size=$(du -h "$zip_path" | cut -f1 | xargs)
    log_info "Created deployment package (${zip_size})"
    log_debug "Zip contents:"
    unzip -l "$zip_path" >> "$LOG_FILE" 2>&1 || true

    # Flex Consumption installs a zip with OneDeploy. Local source has no
    # .python_packages, so Azure builds it (remote build) from requirements.txt.
    local deploy_output
    local deployed=false

    # Method 1: az CLI OneDeploy with remote build
    log_info "Deploying code (az functionapp deployment source config-zip, remote build) ..."
    if deploy_output=$(az functionapp deployment source config-zip \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --src "$zip_path" \
        --build-remote true \
        -o json 2>&1); then
        log_ok "Function App code deployed successfully (config-zip)"
        log_debug "Deploy response: ${deploy_output}"
        deployed=true
    else
        log_info "Method 1 failed: ${deploy_output}"
    fi

    # Method 2: Azure Functions Core Tools (builds remotely by default)
    if [[ "$deployed" != "true" ]]; then
        if command -v func &>/dev/null; then
            log_info "Deploying code (method 2: func azure functionapp publish) ..."
            if deploy_output=$(cd "$func_src" && func azure functionapp publish "$FUNC_APP" --python 2>&1); then
                log_ok "Function App code deployed successfully (func CLI)"
                log_debug "Deploy response: ${deploy_output}"
                deployed=true
            else
                log_info "Method 2 (func CLI) failed: ${deploy_output}"
            fi
        else
            log_debug "func CLI not found, skipping method 2"
        fi
    fi

    if [[ "$deployed" != "true" ]]; then
        log_error "All deployment methods failed."
        log_error ""
        log_error "You can deploy manually from Cloud Shell:"
        log_error "  cd ${func_src}"
        log_error "  func azure functionapp publish ${FUNC_APP} --python"
        log_error ""
        log_error "Or from Azure Portal: Function App → Deployment Center → Local Git/ZIP"
        log_error "Full log: ${LOG_FILE}"
        rm -f "$zip_path"
        exit 1
    fi

    rm -f "$zip_path"
}

# ============================================================
# 4. DEPLOY WEB DASHBOARD
# ============================================================
deploy_webapp() {
    log_section "Web Dashboard Deployment"

    local webapp_src="${SCRIPT_DIR}/../webapp"
    if [[ ! -d "$webapp_src" ]]; then
        log_info "No webapp/ directory found — skipping"
        return 0
    fi

    # Enable static website hosting on storage account
    log_info "Enabling static website on storage account ..."
    run_az "Enable static website" storage blob service-properties update \
        --account-name "$STORAGE" \
        --static-website \
        --index-document "index.html" \
        --404-document "index.html" \
        -o none 2>/dev/null

    # Upload files
    log_info "Uploading webapp files ..."
    local storage_key
    storage_key=$(az storage account keys list \
        --account-name "$STORAGE" \
        --resource-group "$RG" \
        --query "[0].value" -o tsv)

    # The key goes in the environment (only this user can read it), not on the command line.
    if AZURE_STORAGE_KEY="$storage_key" run_az "Upload webapp files" storage blob upload-batch \
        --account-name "$STORAGE" \
        --destination '$web' \
        --source "$webapp_src" \
        --overwrite \
        -o none 2>&1; then
        log_ok "Webapp files uploaded"
    else
        log_error "Failed to upload webapp files. Check log: ${LOG_FILE}"
        return 1
    fi

    # Configure CORS
    local web_url
    web_url=$(az storage account show \
        --name "$STORAGE" \
        --resource-group "$RG" \
        --query "primaryEndpoints.web" -o tsv)

    run_az "Add CORS origin" functionapp cors add \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --allowed-origins "${web_url%/}" \
        -o none 2>/dev/null || true

    log_ok "Web dashboard deployed at: ${web_url}"
}

# ============================================================
# 5. VERIFY & PRINT SUMMARY
# ============================================================
verify() {
    log_section "Verification"

    log_info "Checking Function App status ..."
    local func_state
    func_state=$(az functionapp show \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --query "state || properties.state" -o tsv 2>/dev/null || echo "NOT_FOUND")
    if [[ "$func_state" == "Running" ]]; then
        log_ok "Function App ${FUNC_APP}: Running ✓"
    else
        log_error "Function App ${FUNC_APP}: ${func_state}"
    fi

    local func_url
    func_url="https://$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" --query "defaultHostName || properties.defaultHostName" -o tsv 2>/dev/null)"

    log_section "Setup Complete"
    echo ""
    echo -e "  Resource Group:   ${GREEN}${RG}${NC}"
    echo -e "  Function App:     ${GREEN}${FUNC_APP}${NC}"
    echo -e "  Storage Account:  ${GREEN}${STORAGE}${NC}"
    echo -e "  Region:           ${REGION}"
    echo -e "  Subscriptions:    ${#SUBS[@]}"
    echo ""
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${BOLD}Function App:${NC}   ${func_url}"
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  ${BOLD}Auth:${NC}           Function keys (x-functions-key header)"
    echo ""
    echo -e "  ${YELLOW}Next step:${NC} The collector scans your resources on its first run and starts"
    echo -e "            collecting. Follow it in Site24x7 (AppLogs > Azure Diagnostic Logs), or open"
    echo -e "            ${func_url}/api/dashboard with the host key named default."
    echo ""
    log_info "To tear down all resources: bash ${SCRIPT_DIR}/teardown.sh"
}

# ============================================================
# MAIN
# ============================================================
main() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║  Azure Diagnostic Logs Collection — Setup       ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════╝${NC}"
    echo "" >> "$LOG_FILE"
    echo "=== Setup started at $(date) ===" >> "$LOG_FILE"
    log_info "Full log: ${LOG_FILE}"

    # Reuse the RBAC provisioning helpers without running the migration.
    source "${SCRIPT_DIR}/migrate-roles.sh"
    preflight_checks
    provision_infrastructure
    deploy_function_app
    deploy_webapp
    verify

    echo ""
    log_ok "All done! 🎉"
    log_info "Full log saved to: ${LOG_FILE}"
}

main "$@"
