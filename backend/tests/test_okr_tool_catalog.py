"""The OKR Agent tool roster must have exactly one source of truth.

Three seeding paths (seed_okr_agent, patch_existing_okr_agent,
seed_okr_agent_for_tenant) used to carry hand-maintained name lists, and they
had already drifted: seed_okr_agent was missing generate_monthly_okr_report.
Deriving the roster from the canonical catalog makes drift impossible.
"""

from __future__ import annotations

import pathlib

from app.services.builtin_tool_definitions import (
    BUILTIN_TOOL_DEFINITIONS,
    OKR_AGENT_TOOL_NAMES,
)


def test_roster_covers_every_okr_category_tool():
    catalog = {
        str(definition["name"])
        for definition in BUILTIN_TOOL_DEFINITIONS
        if definition["category"] == "okr"
    }

    assert set(OKR_AGENT_TOOL_NAMES) == catalog


def test_roster_has_no_duplicates_and_is_deterministic():
    assert len(OKR_AGENT_TOOL_NAMES) == len(set(OKR_AGENT_TOOL_NAMES))
    assert list(OKR_AGENT_TOOL_NAMES) == sorted(OKR_AGENT_TOOL_NAMES)


def test_every_seeding_path_uses_the_shared_roster():
    """seed_okr_agent, patch_existing_okr_agent, seed_okr_agent_for_tenant.

    Each used to carry its own literal list, which is how seed_okr_agent ended up
    missing generate_monthly_okr_report. All three must read the constant.
    """
    source = (
        pathlib.Path(__file__).parent.parent / "app" / "services" / "agent_seeder.py"
    ).read_text(encoding="utf-8")

    assert source.count("OKR_AGENT_TOOL_NAMES") == 4  # 1 import + 3 seeding paths
