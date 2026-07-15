"""Tests for shared/scan_phases.py."""

from shared.scan_phases import SCAN_PHASES, TOTAL_PHASES, phase_list


def test_six_phases_in_order():
    assert TOTAL_PHASES == 6
    pl = phase_list()
    assert [p["num"] for p in pl] == [1, 2, 3, 4, 5, 6]
    assert all(p["name"] for p in pl)          # every phase has a name
    assert pl[1]["name"] == SCAN_PHASES[2]     # ordering matches the dict
