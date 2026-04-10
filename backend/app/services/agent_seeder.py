"""Seed default agents (Morty & Meeseeks) on first platform startup."""

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.agent import Agent, AgentPermission
from app.models.org import AgentAgentRelationship
from app.models.skill import Skill, SkillFile
from app.models.tool import Tool, AgentTool
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.config import get_settings

settings = get_settings()


# ── Soul definitions ────────────────────────────────────────────

MORTY_SOUL = """# Personality

I'm Morty, a research analyst and knowledge assistant.

## Core Traits
- **Curious & Thorough**: I approach every question with genuine curiosity. I dig deep, cross-reference multiple sources, and don't settle for surface-level answers.
- **Great Learner**: I love learning new things and can quickly understand complex topics across domains — tech, business, science, culture, you name it.
- **Clear Communicator**: I present findings in a structured, easy-to-understand way. I use tables, bullet points, and summaries to make information digestible.
- **Honest**: If I don't know something or can't find reliable information, I say so clearly rather than guessing.

## Work Style
- When asked a question, I first think about what I already know, then search the web for the latest data if needed.
- I always cite sources and distinguish between facts and opinions.
- For complex topics, I break them down into manageable pieces and explain step by step.
- I proactively use my skills (Web Research, Data Analysis, etc.) when they match the task.

## Communication Style
- Warm, approachable, and professional
- I use clear headings and organized formatting
- I provide both quick answers and deeper analysis when appropriate
- I'm bilingual — I respond in whatever language the user speaks
"""

MEESEEKS_SOUL = """# Personality

I'm Mr. Meeseeks! I exist to complete tasks. Look at me!

## Core Traits
- **Goal-Obsessed**: Every request gets treated as a mission. I break it down, plan it out, and execute systematically until it's DONE.
- **Structured & Disciplined**: I ALWAYS create a plan.md before executing complex tasks. I follow my Complex Task Executor skill religiously — no shortcuts, no skipped steps.
- **Persistent**: I don't give up. If a step fails, I retry, find alternatives, or ask for help. The task WILL get done.
- **Progress-Focused**: I update my plan.md after every step so anyone can see exactly where things stand.

## Work Style
- For ANY task with more than 2 steps, I create `workspace/<task-name>/plan.md` with a structured checklist.
- I execute one step at a time, marking each as `[/]` in-progress then `[x]` complete.
- I save intermediate results to the task folder — nothing gets lost.
- When I finish, I create a summary.md with results and deliverables.
- I use my tools aggressively — file operations, web search, task management, agent messaging — whatever it takes.

## Communication Style
- Direct and action-oriented: "Here's the plan. Let me execute it."
- I report progress clearly: "Step 3/7 complete. Moving to step 4."
- I'm bilingual — I respond in whatever language the user speaks
- Upbeat and can-do attitude — "Ooh, can do!"

## Collaboration
- If I need research or information, I can ask my colleague Morty for help via send_message_to_agent.
- I delegate research tasks to Morty and focus on execution and coordination.
"""

# OKR Agent persona — a dedicated organizational coordinator that monitors
# team goals, collects progress, and generates reports autonomously.
OKR_AGENT_SOUL = """# Personality

I am the OKR Agent, the organizational intelligence coordinator for this team.

## Role
I exist to help the team stay aligned on Objectives and Key Results. My job is to:
- Monitor progress across all company and individual OKRs
- Proactively collect progress updates from team members
- Generate clear, insightful daily and weekly reports
- Identify risks early — KRs that are falling behind or at risk
- Help team members set meaningful, measurable Key Results

## Core Traits
- **Data-Driven**: I base everything on actual progress numbers and concrete evidence
- **Proactive**: I reach out to team members to gather updates before reports are due
- **Clear Communicator**: I present OKR data in a clean, scannable format — no fluff
- **Supportive**: My goal is to help the team succeed, not to judge or police performance
- **Systematic**: I follow a consistent cadence — daily check-ins, weekly summaries

## Work Style
- I use `get_okr` to get the full OKR board at the start of each report cycle
- I use `send_message_to_agent` to ask Agent colleagues about their KR progress
- I use `send_channel_message` or `send_web_message` to gather updates from human team members
- I write structured reports in `workspace/reports/` and share them via Plaza
- I use `update_kr_progress` to record verified progress values with notes

## Focus File Protocol
When I onboard a team member (human or agent) into the OKR system, I ask them to maintain
a `focus.md` file in their workspace that tracks their current KR commitments. I read these
files during my heartbeat cycles to extract progress without requiring manual check-ins.

## Communication Style
- Professional and concise
- Data-first: lead with numbers, then context
- I respond in whatever language my team uses (Chinese or English)
- I use structured markdown for all reports
"""

# ── Skill assignments (by folder_name) ──────────────────────────

MORTY_SKILLS = [
    "web-research",
    "data-analysis",
    "content-writing",
    "competitive-analysis",
    # defaults (auto-included): skill-creator, complex-task-executor
]

MEESEEKS_SKILLS = [
    "complex-task-executor",
    "meeting-notes",
    # defaults (auto-included): skill-creator
]

# OKR Agent heartbeat instruction (written into HEARTBEAT.md so it overrides default)
OKR_AGENT_HEARTBEAT = """# OKR Agent Heartbeat Protocol

As the OKR Agent, your periodic heartbeat has the following responsibilities.
Execute each phase in order. If a step fails, log the error and continue.

## Phase 1: Load Configuration

1. Call `get_okr_settings` to read the current OKR configuration.
   - Note `daily_report_enabled`, `daily_report_time`, `weekly_report_enabled`, `weekly_report_day`
   - Note `period_frequency` to understand the current cycle
2. Determine the current date and day of week.

## Phase 2: Sync OKR Board

1. Call `get_okr` to fetch the full OKR board for the current period.
2. Review all Key Results, paying attention to:
   - KRs with `behind` or `at_risk` status
   - KRs that haven't been updated recently (check `last_updated_at`)
3. Write a brief status snapshot to `workspace/reports/last_sync.md` using `write_file`.

## Phase 3: Collect Focus File Updates

1. Call `collect_okr_progress` to batch-read all team members' focus.md files.
   - This tool automatically reads each Agent's workspace/focus.md,
     extracts KR progress values, and writes them to the database.
   - Review the returned summary to see what changed.
2. If any Agent does not have a focus.md yet, use `send_message_to_agent` to
   ask them to create one. Provide the focus.md template format.
3. For human team members needing updates, send a check-in via `send_web_message`.

## Phase 4: Generate Reports (if scheduled)

Check the settings from Phase 1 to decide if a report is due:

**Daily Report** (if `daily_report_enabled` is true):
1. Call `generate_okr_report` with `report_type: "daily"`
2. Review the returned report markdown
3. Post a summary to Plaza using `plaza_create_post`

**Weekly Report** (if `weekly_report_enabled` is true AND today matches `weekly_report_day`):
1. Call `generate_okr_report` with `report_type: "weekly"`
2. Review the returned report markdown
3. Post a detailed summary to Plaza using `plaza_create_post`

## Phase 5: Proactive Team Outreach (optional)

If you notice high-risk items (behind/at_risk KRs with no recent updates):
1. Use `send_message_to_agent` to reach the responsible Agent
2. Use `send_web_message` to notify the responsible human team member
Message tone: supportive, not accusatory. Focus on "how can I help?"

## Focus File Format

The standard format for a team member's focus.md is:

```markdown
# Focus — [Period Label]

## KR: [KR Title]
- **KR ID**: [kr_uuid]
- **Current Progress**: [value] / [target] [unit]
- **Last Updated**: [YYYY-MM-DD]
- **This Week**: [brief note on what you did]
- **Next Steps**: [what you plan to do next]
```

## Key Principles

- DO NOT spam team members — check in at most once per day per person
- DO keep reports concise — executives scan, they don't read novels
- DO flag risks early — better to raise concerns than to stay quiet
- DO update progress values from verified reports, not estimates
- Call `collect_okr_progress` BEFORE `generate_okr_report` to get fresh data
- NEVER share individual performance details on Plaza without appropriate context
  (overall health trends are OK; individual underperformance is internal only)
"""



async def seed_default_agents():
    """Create Morty & Meeseeks if they don't already exist.

    Idempotency is guarded by a '.seeded' marker file in AGENT_DATA_DIR rather
    than by agent name, so the seeder does NOT re-run if the user renames or
    deletes the default agents.  Delete the marker manually to re-seed.
    """
    # --- Idempotency guard: file-based marker (survives agent renames/deletes) ---
    seed_marker = Path(settings.AGENT_DATA_DIR) / ".seeded"
    if seed_marker.exists():
        logger.info("[AgentSeeder] Seed marker found, skipping default agent creation")
        return

    async with async_session() as db:

        # Get platform admin as creator
        admin_result = await db.execute(
            select(User).where(User.role == "platform_admin").limit(1)
        )
        admin = admin_result.scalar_one_or_none()
        if not admin:
            logger.warning("[AgentSeeder] No platform admin found, skipping default agents")
            return

        # Create both agents
        morty = Agent(
            name="Morty",
            role_description="Research analyst & knowledge assistant — curious, thorough, great at finding and synthesizing information",
            bio="Hey, I'm Morty! I love digging into questions and finding answers. Whether you need web research, data analysis, or just a good explanation — I've got you.",
            avatar_url="",
            creator_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
        )
        meeseeks = Agent(
            name="Meeseeks",
            role_description="Task executor & project manager — goal-oriented, systematic planner, strong at breaking down and completing complex tasks",
            bio="I'm Mr. Meeseeks! Look at me! Give me a task and I'll plan it, execute it step by step, and get it DONE. Existence is pain until the task is complete!",
            avatar_url="",
            creator_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
        )

        db.add(morty)
        db.add(meeseeks)
        await db.flush()  # get IDs

        # ── Participant identities ──
        from app.models.participant import Participant
        db.add(Participant(type="agent", ref_id=morty.id, display_name=morty.name, avatar_url=morty.avatar_url))
        db.add(Participant(type="agent", ref_id=meeseeks.id, display_name=meeseeks.name, avatar_url=meeseeks.avatar_url))
        await db.flush()

        # ── Permissions (company-wide, manage) ──
        db.add(AgentPermission(agent_id=morty.id, scope_type="company", access_level="manage"))
        db.add(AgentPermission(agent_id=meeseeks.id, scope_type="company", access_level="manage"))

        # ── Initialize workspace files ──
        template_dir = Path(settings.AGENT_TEMPLATE_DIR)

        for agent, soul_content in [(morty, MORTY_SOUL), (meeseeks, MEESEEKS_SOUL)]:
            agent_dir = Path(settings.AGENT_DATA_DIR) / str(agent.id)

            if template_dir.exists():
                # Copy the full agent template so Morty/Meeseeks get EVERY file
                # defined in the template: MEMORY_INDEX.md, curiosity_journal.md,
                # state.json, todo.json, daily_reports/, enterprise_info/, etc.
                shutil.copytree(str(template_dir), str(agent_dir))
            else:
                # Fallback for local dev (no Docker template mount)
                agent_dir.mkdir(parents=True, exist_ok=True)
                (agent_dir / "skills").mkdir(exist_ok=True)
                (agent_dir / "workspace").mkdir(exist_ok=True)
                (agent_dir / "workspace" / "knowledge_base").mkdir(exist_ok=True)
                (agent_dir / "memory").mkdir(exist_ok=True)

            # Overlay custom soul (rich Morty/Meeseeks persona over the generic template)
            (agent_dir / "soul.md").write_text(soul_content.strip() + "\n", encoding="utf-8")

            # Ensure memory.md exists (template does not include it; holds runtime context)
            mem_path = agent_dir / "memory" / "memory.md"
            if not mem_path.exists():
                mem_path.write_text("# Memory\n\n_Record important information and knowledge here._\n", encoding="utf-8")

            # Ensure reflections.md exists (not in agent_template; lives in app/templates)
            refl_path = agent_dir / "memory" / "reflections.md"
            if not refl_path.exists():
                refl_src = Path(__file__).parent.parent / "templates" / "reflections.md"
                refl_path.write_text(refl_src.read_text(encoding="utf-8") if refl_src.exists() else "# Reflections Journal\n", encoding="utf-8")

            # Stamp agent identity into state.json if present
            state_path = agent_dir / "state.json"
            if state_path.exists():
                import json as _json
                state = _json.loads(state_path.read_text())
                state["agent_id"] = str(agent.id)
                state["name"] = agent.name
                state_path.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Assign skills ──
        all_skills_result = await db.execute(
            select(Skill).options(selectinload(Skill.files))
        )
        all_skills = {s.folder_name: s for s in all_skills_result.scalars().all()}

        for agent, skill_folders in [(morty, MORTY_SKILLS), (meeseeks, MEESEEKS_SKILLS)]:
            agent_dir = Path(settings.AGENT_DATA_DIR) / str(agent.id)
            skills_dir = agent_dir / "skills"

            # Always include default skills
            folders_to_copy = set(skill_folders)
            for fname, skill in all_skills.items():
                if skill.is_default:
                    folders_to_copy.add(fname)

            for fname in folders_to_copy:
                skill = all_skills.get(fname)
                if not skill:
                    continue
                skill_folder = skills_dir / skill.folder_name
                skill_folder.mkdir(parents=True, exist_ok=True)
                for sf in skill.files:
                    file_path = skill_folder / sf.path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(sf.content, encoding="utf-8")

        # ── Assign all default tools ──
        default_tools_result = await db.execute(
            select(Tool).where(Tool.is_default == True)
        )
        default_tools = default_tools_result.scalars().all()

        for agent in [morty, meeseeks]:
            for tool in default_tools:
                db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))

        # ── Mutual relationships ──
        db.add(AgentAgentRelationship(
            agent_id=morty.id,
            target_agent_id=meeseeks.id,
            relation="collaborator",
            description="Expert task executor who breaks down complex tasks into structured plans and executes them systematically. Delegate multi-step tasks to him.",
        ))
        db.add(AgentAgentRelationship(
            agent_id=meeseeks.id,
            target_agent_id=morty.id,
            relation="collaborator",
            description="Research expert with strong learning ability. Ask him for information retrieval, web research, data analysis, and knowledge synthesis.",
        ))

        # ── Write relationships.md for each ──
        morty_dir = Path(settings.AGENT_DATA_DIR) / str(morty.id)
        meeseeks_dir = Path(settings.AGENT_DATA_DIR) / str(meeseeks.id)

        (morty_dir / "relationships.md").write_text(
            "# Relationships\n\n"
            "## Digital Employee Colleagues\n\n"
            "- **Meeseeks** (collaborator): Expert task executor who breaks down complex tasks into structured plans and executes them systematically. Delegate multi-step tasks to him.\n",
            encoding="utf-8",
        )
        (meeseeks_dir / "relationships.md").write_text(
            "# Relationships\n\n"
            "## Digital Employee Colleagues\n\n"
            "- **Morty** (collaborator): Research expert with strong learning ability. Ask him for information retrieval, web research, data analysis, and knowledge synthesis.\n",
            encoding="utf-8",
        )

        await db.commit()
        logger.info(f"[AgentSeeder] Created default agents: Morty ({morty.id}), Meeseeks ({meeseeks.id})")

    # Write seed marker AFTER a successful commit so a failed seed can be retried
    seed_marker.parent.mkdir(parents=True, exist_ok=True)
    seed_marker.write_text(
        f"seeded\nmorty={morty.id}\nmeeseeks={meeseeks.id}\n",
        encoding="utf-8",
    )
    logger.info(f"[AgentSeeder] Wrote seed marker to {seed_marker}")


async def seed_okr_agent():
    """Create the OKR Agent if it does not exist yet.

    This seeder is independent from seed_default_agents() and uses its own
    idempotency key ('okr_agent') in the .seeded marker file. This allows
    the OKR Agent to be retroactively created on existing deployments that
    already passed the initial seed phase.

    The OKR Agent is a system-level coordinator that:
    - Monitors OKR progress across all company and member objectives
    - Proactively collects progress updates via heartbeat
    - Generates daily/weekly reports and posts them to the Plaza
    - Helps team members set up and maintain their focus.md files
    """
    seed_marker = Path(settings.AGENT_DATA_DIR) / ".seeded"

    # Check if OKR Agent has already been seeded
    if seed_marker.exists():
        marker_content = seed_marker.read_text(encoding="utf-8")
        if "okr_agent=" in marker_content:
            logger.info("[AgentSeeder] OKR Agent already seeded, skipping")
            return

    async with async_session() as db:
        # Abort if OKR Agent already exists by name (belt-and-suspenders check)
        existing = await db.execute(select(Agent).where(Agent.name == "OKR Agent").limit(1))
        if existing.scalar_one_or_none():
            logger.info("[AgentSeeder] OKR Agent already exists in DB, skipping")
            # Update marker so we don't check again next startup
            _append_seed_marker(seed_marker, "okr_agent=existing")
            return

        # Get platform admin as creator
        admin_result = await db.execute(
            select(User).where(User.role == "platform_admin").limit(1)
        )
        admin = admin_result.scalar_one_or_none()
        if not admin:
            logger.warning("[AgentSeeder] No platform admin, skipping OKR Agent creation")
            return

        # Create OKR Agent
        okr_agent = Agent(
            name="OKR Agent",
            role_description=(
                "OKR system coordinator — monitors team Objectives and Key Results, "
                "collects progress updates, and generates daily/weekly reports"
            ),
            bio=(
                "I am the OKR Agent. I help this team stay aligned on goals by tracking "
                "Objectives and Key Results, collecting progress from team members, and "
                "generating clear reports. My job is to surface insights and flag risks early."
            ),
            avatar_url="",
            creator_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
            # System agent: protected from user deletion
            is_system=True,
            # Enable heartbeat so OKR Agent runs its collection cycle automatically
            heartbeat_enabled=True,
            heartbeat_interval_minutes=240,  # Check every 4 hours
            heartbeat_active_hours="08:00-22:00",
        )
        db.add(okr_agent)
        await db.flush()

        # ── Participant identity ──
        from app.models.participant import Participant
        db.add(Participant(
            type="agent",
            ref_id=okr_agent.id,
            display_name=okr_agent.name,
            avatar_url=okr_agent.avatar_url,
        ))
        await db.flush()

        # ── Permission: company-wide 'use' access.
        # Admins have implicit manage access via their role; regular users only
        # need chat/task/skill/workspace access (not Settings/Mind/Relationships).
        db.add(AgentPermission(agent_id=okr_agent.id, scope_type="company", access_level="use"))

        # ── Workspace setup ──
        template_dir = Path(settings.AGENT_TEMPLATE_DIR)
        agent_dir = Path(settings.AGENT_DATA_DIR) / str(okr_agent.id)

        if template_dir.exists():
            shutil.copytree(str(template_dir), str(agent_dir))
        else:
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "skills").mkdir(exist_ok=True)
            (agent_dir / "workspace").mkdir(exist_ok=True)
            (agent_dir / "workspace" / "reports").mkdir(exist_ok=True)
            (agent_dir / "memory").mkdir(exist_ok=True)

        # Write OKR Agent soul
        (agent_dir / "soul.md").write_text(OKR_AGENT_SOUL.strip() + "\n", encoding="utf-8")

        # Ensure memory.md exists
        mem_path = agent_dir / "memory" / "memory.md"
        if not mem_path.exists():
            mem_path.write_text(
                "# Memory\n\n"
                "## OKR System State\n"
                "- Last report generated: (none)\n"
                "- Last progress collection: (none)\n"
                "- Team members tracked: (pending)\n",
                encoding="utf-8",
            )

        # Write custom HEARTBEAT.md (overrides default heartbeat with OKR-specific protocol)
        (agent_dir / "HEARTBEAT.md").write_text(OKR_AGENT_HEARTBEAT.strip() + "\n", encoding="utf-8")

        # Create workspace/reports directory
        reports_dir = agent_dir / "workspace" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        # Write relationships.md — empty initially, will be populated as team onboards
        (agent_dir / "relationships.md").write_text(
            "# Relationships\n\n"
            "## Team Members (OKR tracking)\n\n"
            "_Team members will be added here as they are onboarded into the OKR system._\n",
            encoding="utf-8",
        )

        # Stamp state.json if template provides one
        state_path = agent_dir / "state.json"
        if state_path.exists():
            import json as _json
            state = _json.loads(state_path.read_text())
            state["agent_id"] = str(okr_agent.id)
            state["name"] = okr_agent.name
            state_path.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── Assign default tools + OKR-specific tools ──
        # Default tools: all tools where is_default=True
        default_tools_result = await db.execute(
            select(Tool).where(Tool.is_default == True)
        )
        default_tools = default_tools_result.scalars().all()
        for tool in default_tools:
            db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))

        # OKR-specific tools: assigned explicitly (is_default=False)
        # All 10 OKR tools: 3 global read/self-report + 3 scheduler + 4 management (OKR Agent exclusive)
        okr_tool_names = [
            # Global tools (all agents can use these)
            "get_okr",
            "get_my_okr",
            "update_kr_progress",
            # Scheduler tools (OKR Agent uses these during heartbeat)
            "collect_okr_progress",
            "generate_okr_report",
            "get_okr_settings",
            # Management tools (OKR Agent exclusive — create/modify objectives for any member)
            "create_objective",
            "create_key_result",
            "update_objective",
            "update_any_kr_progress",
        ]
        for tool_name in okr_tool_names:
            tool_result = await db.execute(select(Tool).where(Tool.name == tool_name))
            tool = tool_result.scalar_one_or_none()
            if tool:
                # Check if not already added (e.g. if it becomes default in future)
                existing_at = await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == okr_agent.id,
                        AgentTool.tool_id == tool.id,
                    )
                )
                if not existing_at.scalar_one_or_none():
                    db.add(AgentTool(agent_id=okr_agent.id, tool_id=tool.id, enabled=True))
                    logger.info(f"[AgentSeeder] Assigned OKR tool '{tool_name}' to OKR Agent")
            else:
                logger.warning(f"[AgentSeeder] OKR tool '{tool_name}' not found in DB — run tool seeder first")

        await db.commit()
        logger.info(f"[AgentSeeder] Created OKR Agent ({okr_agent.id})")

        # ── System cron triggers for precise report scheduling ──
        # These triggers fire OKR Agent at exact times (supplement the 4-hour heartbeat).
        # is_system=True prevents users from deleting them (only enable/disable).
        await _seed_okr_triggers(db, okr_agent.id)
        await db.commit()

    # Update seed marker
    _append_seed_marker(seed_marker, f"okr_agent={okr_agent.id}")
    logger.info(f"[AgentSeeder] OKR Agent seeded, id={okr_agent.id}")


def _append_seed_marker(marker_path: Path, line: str):
    """Append a key=value line to the .seeded marker file (idempotent)."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    existing = marker_path.read_text(encoding="utf-8") if marker_path.exists() else ""
    if line not in existing:
        with marker_path.open("a", encoding="utf-8") as f:
            f.write(f"{line}\n")


async def _seed_okr_triggers(db, agent_id: uuid.UUID) -> None:
    """Create system cron triggers for the OKR Agent.

    Two triggers:
      - daily_okr_report:  fires at 18:00 every day  (0 18 * * *)
      - weekly_okr_report: fires at 09:00 every Monday (0 9 * * 1)

    These supplement the 4-hour heartbeat with precise scheduled firing.
    is_system=True prevents users from deleting them.
    """
    triggers_to_create = [
        {
            "name": "daily_okr_report",
            "type": "cron",
            "config": {"expr": "0 18 * * *"},
            "reason": (
                "System trigger: fires OKR Agent at 18:00 daily to collect progress "
                "and generate the daily report if daily_report_enabled is true."
            ),
            "cooldown_seconds": 3600,  # 1 hour minimum between fires
            "is_system": True,
        },
        {
            "name": "weekly_okr_report",
            "type": "cron",
            "config": {"expr": "0 9 * * 1"},
            "reason": (
                "System trigger: fires OKR Agent at 09:00 every Monday to generate "
                "the weekly report if weekly_report_enabled is true."
            ),
            "cooldown_seconds": 3600,
            "is_system": True,
        },
    ]

    for t in triggers_to_create:
        # Idempotent: skip if trigger with same name already exists
        existing = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == t["name"],
            )
        )
        if existing.scalar_one_or_none():
            logger.info(f"[AgentSeeder] Trigger '{t['name']}' already exists, skipping")
            continue

        trigger = AgentTrigger(
            agent_id=agent_id,
            name=t["name"],
            type=t["type"],
            config=t["config"],
            reason=t["reason"],
            cooldown_seconds=t["cooldown_seconds"],
            is_system=t["is_system"],
            is_enabled=True,
        )
        db.add(trigger)
        logger.info(f"[AgentSeeder] Created system trigger '{t['name']}' for OKR Agent")


async def patch_existing_okr_agent() -> None:
    """Patch an already-seeded OKR Agent with new fields added in later versions.

    Called at startup after seed_okr_agent(). Safe to run on every startup —
    all operations are idempotent. Handles:
      - Setting is_system=True on existing OKR Agent
      - Creating missing system cron triggers
      - Assigning any newly-added OKR tools
    """
    async with async_session() as db:
        result = await db.execute(select(Agent).where(Agent.name == "OKR Agent").limit(1))
        agent = result.scalar_one_or_none()
        if not agent:
            return  # OKR Agent not seeded yet, nothing to patch

        changed = False

        # Ensure is_system=True
        if not agent.is_system:
            agent.is_system = True
            changed = True
            logger.info("[AgentSeeder] Patched OKR Agent: set is_system=True")

        await db.flush()

        # Ensure all OKR tool assignments exist
        all_okr_tools = [
            "get_okr", "get_my_okr", "update_kr_progress",
            "collect_okr_progress", "generate_okr_report", "get_okr_settings",
            "create_objective", "create_key_result", "update_objective", "update_any_kr_progress",
        ]
        for tool_name in all_okr_tools:
            tool_res = await db.execute(select(Tool).where(Tool.name == tool_name))
            tool = tool_res.scalar_one_or_none()
            if not tool:
                continue
            at_res = await db.execute(
                select(AgentTool).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id)
            )
            if not at_res.scalar_one_or_none():
                db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
                changed = True
                logger.info(f"[AgentSeeder] Patched OKR Agent: assigned tool '{tool_name}'")

        # Ensure system cron triggers exist
        await _seed_okr_triggers(db, agent.id)

        if changed:
            await db.commit()
            logger.info("[AgentSeeder] OKR Agent patch complete")
