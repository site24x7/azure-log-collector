#!/usr/bin/env python3
"""Read-only preflight for ARM deployments using an explicit deploymentSuffix."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "function-app"))
from shared.storage_names import regional_storage_name, tenant_storage_name, validate_suffix


def check_names(suffix, region, resource_group, subscription=None, run=subprocess.run):
    suffix = validate_suffix(suffix)
    names = [f"s247diag{suffix}", regional_storage_name(region, suffix, region), tenant_storage_name(suffix)]
    subscription_args = ["--subscription", subscription] if subscription else []
    for name in names:
        response = run(["az", "storage", "account", "check-name", "--name", name,
                        *subscription_args, "-o", "json", "--only-show-errors"],
                       check=True, capture_output=True, text=True)
        available = json.loads(response.stdout).get("nameAvailable")
        if available is True:
            print(f"{name}: available")
            continue
        if available is not False:
            raise RuntimeError(f"Could not determine availability of {name}")
        existing = run(["az", "storage", "account", "show", "--name", name,
                        "--resource-group", resource_group, *subscription_args,
                        "-o", "json", "--only-show-errors"], capture_output=True, text=True)
        if existing.returncode != 0:
            raise RuntimeError(f"{name} is globally unavailable or ownership cannot be verified. "
                               "For a fresh install choose another deploymentSuffix; "
                               "keep the existing suffix for upgrades.")
        account = json.loads(existing.stdout)
        if account.get("tags", {}).get("managed-by") != "s247-diag-logs":
            raise RuntimeError(f"{name} exists but is not tagged as collector storage")
        if account.get("primaryLocation", "").replace(" ", "").lower() != region.replace(" ", "").lower():
            raise RuntimeError(f"{name} already exists in a different region; keep the original deployment location")
        print(f"{name}: existing collector account in {resource_group}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suffix", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--resource-group", default="s247-diag-logs-rg")
    parser.add_argument("--subscription")
    args = parser.parse_args()
    try:
        check_names(args.suffix, args.region, args.resource_group, args.subscription)
    except (ValueError, RuntimeError, subprocess.CalledProcessError, FileNotFoundError) as error:
        parser.exit(1, f"Preflight failed: {error}\n")
