"""Canonical ordered list of scan phases.

Single source of truth shared by DiagSettingsManager (which reports the current
phase during a scan) and GetStatus (which sends the full list to the dashboard
so it can render a stepper). Keep names concise and user-facing.
"""

SCAN_PHASES = {
    1: "Fetching supported log types from Site24x7",
    2: "Discovering Azure resources",
    3: "Provisioning regional storage accounts",
    4: "Mapping diagnostic categories to resources",
    5: "Creating log types in Site24x7",
    6: "Configuring diagnostic settings",
}

TOTAL_PHASES = len(SCAN_PHASES)


def phase_list():
    """Return the phases as an ordered list of {num, name} dicts."""
    return [{"num": n, "name": SCAN_PHASES[n]} for n in sorted(SCAN_PHASES)]
