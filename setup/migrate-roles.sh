#!/usr/bin/env bash
# Administrator-run migration for existing Azure Diagnostic Logs collectors.
set -euo pipefail

role_migration_error() {
    printf 'ERROR: %s\n' "$*" >&2
}

find_rg_delete_role() {
    local subscription="$1" role_name="$2" definitions
    definitions=$(az role definition list --subscription "$subscription" \
        --scope "/subscriptions/$subscription" --name "$role_name" --custom-role-only \
        --only-show-errors -o json 2>/dev/null) || return 1

    # Fail closed if the name is already in use with a different scope or permissions.
    jq -er --arg scope "/subscriptions/$subscription" '
        if length == 0 then ""
        elif length == 1 and (.[0] |
            (.assignableScopes | map(ascii_downcase)) == [($scope | ascii_downcase)] and
            (.permissions | length) == 1 and
            (.permissions[0] |
                (.actions | map(ascii_downcase) | sort) == [
                    "microsoft.resources/subscriptions/resourcegroups/delete",
                    "microsoft.resources/subscriptions/resourcegroups/read"
                ] and ((.notActions // []) + (.dataActions // []) + (.notDataActions // []) | length) == 0)
            ) then .[0].id
        else error("Existing custom role has unexpected scopes or permissions")
        end' <<< "$definitions"
}

ensure_rg_delete_role() {
    local subscription="$1" role_name="$2" role_id="" definition_file
    if ! role_id=$(find_rg_delete_role "$subscription" "$role_name"); then
        role_migration_error "Could not verify the custom role $role_name in subscription $subscription."
        return 1
    fi
    if [[ -n "$role_id" ]]; then
        printf '%s\n' "$role_id"
        return
    fi

    definition_file=$(mktemp) || return 1
    if jq -n --arg name "$role_name" --arg scope "/subscriptions/$subscription" '{
        Name: $name,
        IsCustom: true,
        Description: "Read and delete the resource group assigned to the Site24x7 collector.",
        Actions: [
            "Microsoft.Resources/subscriptions/resourceGroups/delete",
            "Microsoft.Resources/subscriptions/resourceGroups/read"
        ],
        NotActions: [], DataActions: [], NotDataActions: [], AssignableScopes: [$scope]
    }' > "$definition_file"; then
        role_id=$(az role definition create --subscription "$subscription" \
            --role-definition "$definition_file" --query id --only-show-errors -o tsv 2>/dev/null) || role_id=""
    fi
    rm -f "$definition_file"
    if [[ -z "$role_id" ]]; then
        # Another collector installation may have created the role concurrently.
        role_id=$(find_rg_delete_role "$subscription" "$role_name") || return 1
    fi
    if [[ -z "$role_id" ]]; then
        role_migration_error "Could not create $role_name in subscription $subscription. Creating the role needs Microsoft.Authorization/roleDefinitions/write (Owner or Contributor plus User Access Administrator for installation)."
        return 1
    fi
    printf '%s\n' "$role_id"
}

require_collector_role() {
    local subscription="$1" scope="$2" principal="$3" role_id="$4" role_name="$5"
    local assignment_name="${6:-}"
    local -a name_option=()
    if [[ -n "$assignment_name" ]]; then
        name_option=(--name "$assignment_name")
    fi
    if ! az role assignment create --subscription "$subscription" --scope "$scope" \
        --assignee-object-id "$principal" --assignee-principal-type ServicePrincipal \
        --role "$role_id" ${name_option[@]+"${name_option[@]}"} --only-show-errors -o none; then
        role_migration_error "Could not assign $role_name to $principal at $scope. Microsoft.Authorization/roleAssignments/write is required."
        return 1
    fi
}

arm_role_assignment_name() {
    # ARM guid() is UUIDv5 using this namespace and hyphen-separated arguments.
    python3 - "$@" <<'PY'
import sys
from uuid import UUID, uuid5

print(uuid5(UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830"), "-".join(sys.argv[1:])))
PY
}

require_scoped_collector_role() {
    local subscription="$1" scope="$2" principal="$3" role_id="$4" role_name="$5"
    local assignment_name
    assignment_name=$(arm_role_assignment_name "$scope" "$principal" "$role_id") || return 1
    require_collector_role "$subscription" "$scope" "$principal" "$role_id" "$role_name" "$assignment_name"
}

migration_usage() {
    echo "Usage: bash $0 --app NAME --rg NAME [--subscription ID_OR_NAME] [--apply]"
    echo "Defaults to a read-only plan. Apply requires an Azure administrator with role definition and assignment write access and role assignment delete access."
}

# The hosting subscription, collector RG and site, including assignments inherited at the site.
list_collector_roles() {
    az role assignment list --subscription "$subscription" --scope "$site_id" \
        --assignee-object-id "$principal" --include-inherited --fill-principal-name false \
        --fill-role-definition-name false --only-show-errors -o json 2>/dev/null
}

exact_assignment_ids() {
    local assignments="$1" scope="$2" role="$3"
    jq -r --arg scope "$scope" --arg role "${role##*/}" --arg principal "$principal" '
        .[] | select((.scope | ascii_downcase) == ($scope | ascii_downcase)
            and (.principalId | ascii_downcase) == ($principal | ascii_downcase)
            and (.roleDefinitionId | split("/") | last | ascii_downcase) == ($role | ascii_downcase))
        | .id' <<< "$assignments"
}

replacement_roles_visible() {
    local assignments="$1"
    [[ -n "$(exact_assignment_ids "$assignments" "$rg_id" "$storage_role")" &&
        -n "$(exact_assignment_ids "$assignments" "$site_id" "$website_role")" &&
        -n "$(exact_assignment_ids "$assignments" "$rg_id" "$locks_role")" &&
        -n "$(exact_assignment_ids "$assignments" "$rg_id" "$custom_id")" ]]
}

save_migration_progress() {
    local phase="$1" temp_file
    temp_file=$(mktemp "${manifest}.XXXXXX") || return 1
    if ! jq -n --arg subscription "$subscription" --arg group "$rg_id" --arg site "$site_id" \
        --arg principal "$principal" --arg originalAssignment "$rollback_id" --arg phase "$phase" \
        '{$subscription, $group, $site, $principal, $originalAssignment, $phase}' > "$temp_file" ||
        ! mv -f "$temp_file" "$manifest"; then
        rm -f "$temp_file"
        return 1
    fi
}

migrate_collector_roles() {
    local app="" rg="" subscription="" apply=false
    while (( $# )); do
        case "$1" in
            --app|--rg|--subscription)
                if [[ -z "${2:-}" || "$2" == --* ]]; then
                    role_migration_error "Missing value for $1."
                    return 1
                fi
                case "$1" in
                    --app) app="$2" ;;
                    --rg) rg="$2" ;;
                    --subscription) subscription="$2" ;;
                esac
                shift ;;
            --apply) apply=true ;;
            -h|--help) migration_usage; return 0 ;;
            *) migration_usage >&2; return 1 ;;
        esac
        shift
    done
    if [[ -z "$app" || -z "$rg" ]]; then
        migration_usage >&2
        return 1
    fi

    local tool
    for tool in az jq; do
        command -v "$tool" >/dev/null || { role_migration_error "$tool is required."; return 1; }
    done
    local -a subscription_option=()
    if [[ -n "$subscription" ]]; then
        subscription_option=(--subscription "$subscription")
    fi
    subscription=$(az account show ${subscription_option[@]+"${subscription_option[@]}"} \
        --query id -o tsv 2>/dev/null) || subscription=""
    local guid_pattern='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    if [[ ! "$subscription" =~ ^${guid_pattern}$ ]]; then
        role_migration_error "Could not resolve the hosting subscription."
        return 1
    fi

    local rg_id site_id principal site
    if ! rg_id=$(az group show --subscription "$subscription" --name "$rg" --query id -o tsv 2>/dev/null) ||
        ! site=$(az functionapp show --subscription "$subscription" --resource-group "$rg" --name "$app" \
            --query '{id:id, principal:identity.principalId}' -o json 2>/dev/null); then
        role_migration_error "Could not resolve the collector resource group or Function App."
        return 1
    fi
    site_id=$(jq -er '.id' <<< "$site")
    principal=$(jq -er '.principal // ""' <<< "$site")
    if [[ ! "$principal" =~ ^${guid_pattern}$ ]]; then
        role_migration_error "The Function App has no system-assigned managed identity."
        return 1
    fi

    local sub_scope="/subscriptions/$subscription"
    local storage_role="17d1049b-9a84-46fb-8f53-869881c3d3ab"
    local website_role="de139f84-1756-47ae-9be6-808fbbe84772"
    local locks_role="28bf596f-4eb7-45ce-b5bc-6cf482fec137"
    local contributor="b24988ac-6180-42a0-ab88-20f7382dd24c"
    local custom_role="Site24x7 Collector RG Delete v1 CLI (${subscription})"
    local definitions="$sub_scope/providers/Microsoft.Authorization/roleDefinitions"
    local assignments legacy_id custom_id
    if ! assignments=$(list_collector_roles); then
        role_migration_error "Could not list assignments for $principal."
        return 1
    fi
    legacy_id=$(exact_assignment_ids "$assignments" "$rg_id" "$contributor")
    if ! custom_id=$(find_rg_delete_role "$subscription" "$custom_role"); then
        role_migration_error "Could not verify $custom_role in subscription $subscription."
        return 1
    fi
    printf 'Collector %s\nHosting subscription %s\nPrincipal %s\n' "$site_id" "$subscription" "$principal"
    echo "Current assignments at or inherited by the Function App (scope, role ID, assignment ID):"
    jq -r '.[] | [.scope, .roleDefinitionId, .id] | @tsv' <<< "$assignments"
    printf 'Ensure Storage Account Contributor and Locks Contributor at %s.\n' "$rg_id"
    printf 'Ensure Website Contributor at %s.\n' "$site_id"
    printf 'Ensure %s at %s (definition: %s).\n' "$custom_role" "$rg_id" "${custom_id:-create at the hosting subscription}"
    printf 'Exact RG Contributor assignment to remove after the new grants are visible: %s\n' "${legacy_id:-none}"
    echo "Subscription assignments and inherited grants are left in place."
    if [[ "$apply" != true ]]; then
        echo "PLAN only. Use --apply to make these changes."
        return 0
    fi
    if ! command -v python3 >/dev/null; then
        role_migration_error "python3 is required for deterministic role assignment names."
        return 1
    fi

    local manifest="./.s247-role-migration-$principal.json"
    local rollback_id="$legacy_id" saved_id
    if [[ -f "$manifest" ]]; then
        saved_id=$(jq -er --arg subscription "$subscription" --arg group "$rg_id" --arg site "$site_id" \
            --arg principal "$principal" '
            select(.subscription == $subscription and .group == $group and .site == $site and .principal == $principal)
            | .originalAssignment' "$manifest") || {
                role_migration_error "The progress manifest $manifest does not match this collector."
                return 1
            }
        rollback_id="${legacy_id:-$saved_id}"
    fi

    local normalized_rg normalized_id
    normalized_rg=$(printf '%s' "$rg_id" | tr '[:upper:]' '[:lower:]')
    normalized_id=$(printf '%s' "$rollback_id" | tr '[:upper:]' '[:lower:]')
    if [[ -n "$rollback_id" && ! "$normalized_id" =~ ^"${normalized_rg}/providers/microsoft.authorization/roleassignments/"${guid_pattern}$ ]]; then
        role_migration_error "Expected a single exact RG Contributor assignment ID."
        return 1
    fi
    save_migration_progress preparing || { role_migration_error "Could not save $manifest."; return 1; }

    custom_id=$(ensure_rg_delete_role "$subscription" "$custom_role") || return 1
    # Suppress raw Azure errors in this utility. Its output only contains role names and resource IDs.
    require_scoped_collector_role "$subscription" "$rg_id" "$principal" "$definitions/$storage_role" "Storage Account Contributor" 2>/dev/null ||
        { role_migration_error "Could not assign Storage Account Contributor at $rg_id."; return 1; }
    require_scoped_collector_role "$subscription" "$site_id" "$principal" "$definitions/$website_role" "Website Contributor" 2>/dev/null ||
        { role_migration_error "Could not assign Website Contributor at $site_id."; return 1; }
    require_collector_role "$subscription" "$rg_id" "$principal" "$definitions/$locks_role" "Locks Contributor" 2>/dev/null ||
        { role_migration_error "Could not assign Locks Contributor at $rg_id."; return 1; }
    require_scoped_collector_role "$subscription" "$rg_id" "$principal" "$custom_id" "$custom_role" 2>/dev/null ||
        { role_migration_error "Could not assign $custom_role at $rg_id."; return 1; }

    local attempt visible=false propagation_started=$SECONDS
    echo "Waiting up to 5 minutes for the replacement grants to appear in Azure RBAC."
    for (( attempt=0; attempt<30; attempt++ )); do
        if assignments=$(list_collector_roles) && replacement_roles_visible "$assignments"; then
            visible=true
            break
        fi
        sleep 10
    done
    if [[ "$visible" != true ]]; then
        role_migration_error "Timed out waiting for the replacement grants. Rerun --apply to resume."
        return 1
    fi

    if [[ -n "$legacy_id" ]]; then
        # Assignment listing can succeed before ARM's authorization caches refresh.
        local remaining=$((600 - (SECONDS - propagation_started)))
        if (( remaining > 0 )); then
            printf 'Allowing %s more seconds for Azure RBAC propagation.\n' "$remaining"
            sleep "$remaining"
        fi
        local current_principal current_legacy_id
        current_principal=$(az functionapp show --subscription "$subscription" --resource-group "$rg" --name "$app" \
            --query identity.principalId -o tsv 2>/dev/null) || return 1
        if ! assignments=$(list_collector_roles) || ! replacement_roles_visible "$assignments"; then
            role_migration_error "The replacement grants could not be confirmed. Contributor was kept."
            return 1
        fi
        current_legacy_id=$(exact_assignment_ids "$assignments" "$rg_id" "$contributor") || return 1
        if [[ "$current_principal" != "$principal" || "$current_legacy_id" != "$legacy_id" ]]; then
            role_migration_error "The principal or exact Contributor grant changed. Review a fresh plan."
            return 1
        fi
    fi

    if [[ -n "$rollback_id" ]]; then
        echo "Administrator rollback command:"
        printf '  az role assignment create --subscription %q --assignee-object-id %q --assignee-principal-type ServicePrincipal --role %q --scope %q --name %q\n' \
            "$subscription" "$principal" "$contributor" "$rg_id" "${rollback_id##*/}"
    fi
    if [[ -n "$legacy_id" ]]; then
        save_migration_progress ready-to-remove || return 1
        # The collector's delete lock on its group also blocks deleting role assignments there:
        # lift only its own s247-lock-* group locks for this one call, and always put them back.
        local locks lock_name lock_notes lock_level err removed=true
        locks=$(az lock list --subscription "$subscription" --resource-group "${rg_id##*/}" \
            --query "[?starts_with(name, 's247-lock-') && level=='CanNotDelete' && contains(id, '/resourceGroups/${rg_id##*/}/providers/Microsoft.Authorization/locks/')].[name, level, notes]" \
            -o tsv 2>/dev/null) || locks=""
        while IFS=$'\t' read -r lock_name lock_level lock_notes; do
            [[ -n "$lock_name" ]] || continue
            az lock delete --subscription "$subscription" --resource-group "${rg_id##*/}" --name "$lock_name" \
                --only-show-errors -o none || { role_migration_error "Could not lift lock $lock_name."; return 1; }
        done <<< "$locks"
        if ! err=$(az role assignment delete --subscription "$subscription" --ids "$legacy_id" --only-show-errors -o none 2>&1); then
            removed=false
        fi
        while IFS=$'\t' read -r lock_name lock_level lock_notes; do
            [[ -n "$lock_name" ]] || continue
            az lock create --subscription "$subscription" --resource-group "${rg_id##*/}" --name "$lock_name" \
                --lock-type "$lock_level" ${lock_notes:+--notes "$lock_notes"} --only-show-errors -o none ||
                role_migration_error "Could not restore lock $lock_name on ${rg_id##*/}; recreate it (CanNotDelete)."
        done <<< "$locks"
        if [[ "$removed" != true ]]; then
            role_migration_error "Could not remove $legacy_id: $(grep -v -i 'warning' <<< "$err" | head -3)"
            return 1
        fi
        echo "Removed the exact RG Contributor assignment."
    else
        echo "There is no exact RG Contributor assignment to remove."
    fi
    save_migration_progress assignments-updated || return 1
    printf 'Progress saved in %s. Verify storage, Function App operations and teardown with the collector identity.\n' "$manifest"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    migrate_collector_roles "$@"
fi
