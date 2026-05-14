#!/usr/bin/env bash
# ============================================================
# Azure Diagnostic Logs Collection — Setup Script
# Creates core infrastructure and deploys code + webapp.
# Resource discovery and diagnostic settings are handled by
# the Function App itself (via Manual Scan in the dashboard).
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/s247-setup-$(date +%Y%m%d-%H%M%S).log"

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
    log_debug "Running: az $*"
    local output
    if output=$(az "$@" 2>&1); then
        log_debug "Success: ${description}"
        echo "$output"
        return 0
    else
        local exit_code=$?
        log_error "${description} failed (exit code: ${exit_code})"
        log_error "Command: az $*"
        log_error "Output: ${output}"
        echo "$output"
        return $exit_code
    fi
}

# ============================================================
# 1. PRE-FLIGHT CHECKS
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
    # shellcheck disable=SC1090
    source "$config_file"
    log_ok "Loaded config.env"

    # Defaults
    # Auto-generate unique suffix from subscription ID if names are defaults
    local unique_suffix
    unique_suffix=$(echo "${SUBSCRIPTION_IDS}" | md5sum 2>/dev/null || md5 -q -s "${SUBSCRIPTION_IDS}" 2>/dev/null || echo "$$")
    unique_suffix="${unique_suffix:0:6}"

    RG="${RESOURCE_GROUP_NAME:-s247-diag-logs-rg}"
    FUNC_APP="${FUNCTION_APP_NAME:-s247-diag-func-${unique_suffix}}"
    STORAGE="${STORAGE_ACCOUNT_NAME:-s247diag${unique_suffix}}"
    REGION="${FUNCTION_APP_REGION:-eastus}"
    TIMER="${TIMER_SCHEDULE:-0 0 */6 * * *}"
    GENERAL_LOGTYPE="${GENERAL_LOGTYPE_ENABLED:-true}"

    log_info "Resource Group:    ${RG}"
    log_info "Function App:      ${FUNC_APP}"
    log_info "Storage Account:   ${STORAGE}"
    log_info "Region:            ${REGION}"

    # Check required tools
    for cmd in az jq zip; do
        if ! command -v $cmd &>/dev/null; then
            log_error "$cmd is required but not installed"
            exit 1
        fi
    done
    log_ok "Required tools available (az, jq, zip)"

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

    # Site24x7 token
    if [[ -z "${SITE24X7_API_TOKEN:-}" || "${SITE24X7_API_TOKEN}" == "your-token-here" ]]; then
        log_info "SITE24X7_API_TOKEN is placeholder — Site24x7 integration will use stubs"
    fi

    # Check storage account name availability (skip if it already belongs to our RG)
    local sa_available
    sa_available=$(az storage account check-name --name "$STORAGE" --query "nameAvailable" -o tsv 2>/dev/null || echo "true")
    if [[ "$sa_available" == "false" ]]; then
        if ! az storage account show --name "$STORAGE" --resource-group "$RG" &>/dev/null; then
            log_error "Storage account name '${STORAGE}' is already taken globally."
            log_error "Change STORAGE_ACCOUNT_NAME in config.env to something unique."
            log_error "Tip: Try '${STORAGE}$(openssl rand -hex 2 2>/dev/null || echo $$)'"
            exit 1
        fi
        log_skip "Storage account ${STORAGE} already exists in our RG"
    fi
    log_ok "Resource names look good"
}

# ============================================================
# 2. PROVISION INFRASTRUCTURE
# ============================================================
provision_infrastructure() {
    log_section "Infrastructure Provisioning"

    # ── Resource Group ───────────────────────────────────────
    log_info "Creating resource group ${RG} in ${REGION} ..."
    run_az "Create resource group" group create --name "$RG" --location "$REGION" -o none
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
            -o none
        log_ok "Created storage account ${STORAGE}"
    fi

    # ── Function App ─────────────────────────────────────────
    log_info "Creating Function App ${FUNC_APP} ..."
    if az functionapp show --name "$FUNC_APP" --resource-group "$RG" &>/dev/null; then
        log_skip "Function App ${FUNC_APP} already exists"
    else
        run_az "Create Function App" functionapp create \
            --name "$FUNC_APP" \
            --resource-group "$RG" \
            --storage-account "$STORAGE" \
            --consumption-plan-location "$REGION" \
            --runtime python \
            --runtime-version 3.11 \
            --os-type Linux \
            --functions-version 4 \
            -o none
        log_ok "Created Function App ${FUNC_APP} (Linux Consumption)"
    fi

    # ── Managed Identity ─────────────────────────────────────
    log_info "Enabling system-assigned managed identity ..."
    local principal_id
    principal_id=$(run_az "Assign managed identity" functionapp identity assign \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --query principalId -o tsv)
    log_ok "Managed identity enabled (principal: ${principal_id})"

    # ── RBAC Role Assignments ────────────────────────────────
    log_info "Assigning RBAC roles ..."
    local rbac_failed=false

    for sub in "${SUBS[@]}"; do
        sub="$(echo "$sub" | xargs)"
        local scope="/subscriptions/${sub}"

        if az role assignment create \
            --assignee-object-id "$principal_id" \
            --assignee-principal-type ServicePrincipal \
            --role "Reader" \
            --scope "$scope" \
            -o none 2>>"$LOG_FILE"; then
            log_ok "Reader on subscription ${sub}"
        else
            log_error "Failed to assign Reader on ${sub} (need Owner/RBAC Admin role)"
            rbac_failed=true
        fi

        if az role assignment create \
            --assignee-object-id "$principal_id" \
            --assignee-principal-type ServicePrincipal \
            --role "Monitoring Contributor" \
            --scope "$scope" \
            -o none 2>>"$LOG_FILE"; then
            log_ok "Monitoring Contributor on subscription ${sub}"
        else
            log_error "Failed to assign Monitoring Contributor on ${sub}"
            rbac_failed=true
        fi
    done

    # Contributor on RG only (for dynamic Event Hub + lock management)
    local rg_id
    rg_id=$(az group show --name "$RG" --query id -o tsv)
    if az role assignment create \
        --assignee-object-id "$principal_id" \
        --assignee-principal-type ServicePrincipal \
        --role "Contributor" \
        --scope "$rg_id" \
        -o none 2>>"$LOG_FILE"; then
        log_ok "Contributor on resource group ${RG}"
    else
        log_error "Failed to assign Contributor on RG ${RG}"
        rbac_failed=true
    fi

    if [[ "$rbac_failed" == "true" ]]; then
        log_error ""
        log_error "RBAC assignments failed. Your account needs Owner or RBAC Administrator"
        log_error "role on the subscription to assign roles. Ask your Azure admin to either:"
        log_error "  1. Grant you 'Role Based Access Control Administrator' role, OR"
        log_error "  2. Manually assign these roles to the managed identity (principal: ${principal_id}):"
        log_error "     - Reader on monitored subscriptions"
        log_error "     - Monitoring Contributor on monitored subscriptions"
        log_error "     - Contributor on resource group ${RG}"
        log_error ""
        log_info "Continuing setup — RBAC can be assigned later via Azure Portal."
    fi

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
    diag_suffix=$(echo "$FUNC_APP" | grep -oP '[a-z0-9]{6}$' || echo "000000")
    local sa_name="s247diag${sanitized_region}${diag_suffix}"
    sa_name="${sa_name:0:24}"

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
    run_az "Set Function App settings" functionapp config appsettings set \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --settings \
            "SUBSCRIPTION_IDS=${SUBSCRIPTION_IDS}" \
            "SITE24X7_API_TOKEN=${SITE24X7_API_TOKEN:-}" \
            "SITE24X7_BASE_URL=https://www.${SITE24X7_DOMAIN:-site24x7.com}" \
            "GENERAL_LOGTYPE_ENABLED=${GENERAL_LOGTYPE}" \
            "TIMER_SCHEDULE=${TIMER}" \
            "RESOURCE_GROUP_NAME=${RG}" \
            "PROCESSING_ENABLED=true" \
            "FUNCTIONS_WORKER_RUNTIME=python" \
            "DIAG_STORAGE_SUFFIX=${diag_suffix}" \
            "UPDATE_CHECK_URL=${UPDATE_CHECK_URL:-}" \
            "ENABLE_ORYX_BUILD=true" \
            "SCM_DO_BUILD_DURING_DEPLOYMENT=true" \
        -o none
    log_ok "Function App settings configured (storage account polling + remote build)"
}

# ============================================================
# 3. DEPLOY FUNCTION APP CODE
# ============================================================
deploy_function_app() {
    log_section "Function App Deployment"

    local func_src="${SCRIPT_DIR}/../function-app"
    local zip_path="/tmp/s247-function-app.zip"

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
        state=$(az functionapp show --name "$FUNC_APP" --resource-group "$RG" --query "state" -o tsv 2>/dev/null || echo "Unknown")
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

    # Dev deploys push local code; clear the URL-based package setting (used by
    # the customer-facing one-click ARM flow) and enable Oryx remote build so
    # the zipdeploy below actually becomes the running code.
    log_info "Switching to Oryx build mode for local code deploy ..."
    az functionapp config appsettings delete \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --setting-names WEBSITE_RUN_FROM_PACKAGE \
        -o none 2>/dev/null || true
    az functionapp config appsettings set \
        --name "$FUNC_APP" \
        --resource-group "$RG" \
        --settings ENABLE_ORYX_BUILD=true SCM_DO_BUILD_DURING_DEPLOYMENT=true \
        -o none 2>/dev/null || true
    log_ok "Function App switched to Oryx build mode"

    # Deploy with full error output — try multiple methods
    local deploy_output
    local deployed=false

    # Method 0: Azure Functions Core Tools (most reliable for Linux Python)
    if command -v func &>/dev/null; then
        log_info "Deploying code (method 0: func azure functionapp publish) ..."
        if deploy_output=$(cd "$func_src" && func azure functionapp publish "$FUNC_APP" --python 2>&1); then
            log_ok "Function App code deployed successfully (func CLI)"
            log_debug "Deploy response: ${deploy_output}"
            deployed=true
        else
            log_info "Method 0 (func CLI) failed: ${deploy_output}"
        fi
    else
        log_debug "func CLI not found, skipping method 0"
    fi

    # Method 1: az functionapp deploy (newer az CLI command)
    if [[ "$deployed" != "true" ]]; then
        log_info "Deploying code (method 1: az functionapp deploy) ..."
        if deploy_output=$(az functionapp deploy \
            --name "$FUNC_APP" \
            --resource-group "$RG" \
            --src-path "$zip_path" \
            --type zip \
            --async true \
            -o json 2>&1); then
            log_ok "Function App code deployed successfully (az functionapp deploy)"
            log_debug "Deploy response: ${deploy_output}"
            deployed=true
        else
            log_info "Method 1 failed: ${deploy_output}"
        fi
    fi

    # Method 2: config-zip (classic)
    if [[ "$deployed" != "true" ]]; then
        log_info "Trying method 2: az functionapp deployment source config-zip ..."
        if deploy_output=$(az functionapp deployment source config-zip \
            --name "$FUNC_APP" \
            --resource-group "$RG" \
            --src "$zip_path" \
            -o json 2>&1); then
            log_ok "Function App code deployed successfully (config-zip)"
            log_debug "Deploy response: ${deploy_output}"
            deployed=true
        else
            log_info "Method 2 failed: ${deploy_output}"
        fi
    fi

    # Method 3: Direct Kudu API
    if [[ "$deployed" != "true" ]]; then
        log_info "Trying method 3: Kudu zipdeploy API ..."
        local creds
        creds=$(az functionapp deployment list-publishing-credentials \
            --name "$FUNC_APP" \
            --resource-group "$RG" \
            -o json 2>>"$LOG_FILE")
        local scm_uri username password
        scm_uri=$(echo "$creds" | jq -r '.scmUri')
        username=$(echo "$creds" | jq -r '.publishingUserName')
        password=$(echo "$creds" | jq -r '.publishingPassword')

        if deploy_output=$(curl -s -w "\nHTTP_STATUS:%{http_code}" \
            -X POST \
            -u "${username}:${password}" \
            -H "Content-Type: application/zip" \
            --data-binary "@${zip_path}" \
            "${scm_uri}/api/zipdeploy?isAsync=true" 2>&1); then
            local http_status
            http_status=$(echo "$deploy_output" | grep "HTTP_STATUS:" | sed 's/HTTP_STATUS://')
            local body
            body=$(echo "$deploy_output" | grep -v "HTTP_STATUS:")
            log_debug "Kudu response (HTTP ${http_status}): ${body}"
            if [[ "$http_status" =~ ^2 ]]; then
                log_ok "Function App code deployed successfully (Kudu API)"
                deployed=true
            else
                log_info "Method 3 failed (HTTP ${http_status}): ${body}"
            fi
        else
            log_info "Method 3 curl failed: ${deploy_output}"
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

    if run_az "Upload webapp files" storage blob upload-batch \
        --account-name "$STORAGE" \
        --account-key "$storage_key" \
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
        --query "state" -o tsv 2>/dev/null || echo "NOT_FOUND")
    if [[ "$func_state" == "Running" ]]; then
        log_ok "Function App ${FUNC_APP}: Running ✓"
    else
        log_error "Function App ${FUNC_APP}: ${func_state}"
    fi

    local web_url
    web_url=$(az storage account show \
        --name "$STORAGE" \
        --resource-group "$RG" \
        --query "primaryEndpoints.web" -o tsv 2>/dev/null || echo "N/A")
    local func_url="https://${FUNC_APP}.azurewebsites.net"

    log_section "Setup Complete"
    echo ""
    echo -e "  Resource Group:   ${GREEN}${RG}${NC}"
    echo -e "  Function App:     ${GREEN}${FUNC_APP}${NC}"
    echo -e "  Storage Account:  ${GREEN}${STORAGE}${NC}"
    echo -e "  Region:           ${REGION}"
    echo -e "  Subscriptions:    ${#SUBS[@]}"
    echo ""
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${BOLD}Web Dashboard:${NC}  ${web_url}?api=${func_url}"
    echo -e "  ${BOLD}Function App:${NC}   ${func_url}"
    echo -e "  ${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  ${BOLD}Auth:${NC}           Function Keys (pass ?code=<key> or x-functions-key header)"
    echo -e "  ${BOLD}Access:${NC}         Contributor role on ${RG} required"
    echo ""
    echo -e "  ${YELLOW}Next step:${NC} Open the Function App URL above in your browser"
    echo -e "            Log in with your Azure AD account"
    echo -e "            Go to ${BOLD}Manual Scan${NC} and click ${BOLD}Trigger Scan${NC}"
    echo -e "            This will discover your resources and configure log streaming."
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
