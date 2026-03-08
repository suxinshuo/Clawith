# Clawith v1.4.0 Release

**Release Date**: 2026-03-08  
**Version**: v1.4.0  
**Focus**: Participant Abstraction Layer · Agent-to-Agent Chat · Multimodal Vision

## Summary

v1.4.0 introduces the **Participant abstraction layer**, enabling agents to have first-class identities on par with users. Agent-to-agent conversations are now properly stored, queried, and displayed in the Chat UI. This release also adds **multimodal vision support**, allowing agents to process and understand images.

> [!WARNING]
> **Breaking Change**: This release removes the deprecated `messages` table and introduces the `participants` table with schema changes to `chat_sessions` and `chat_messages`. A data migration script **must** be executed after upgrading. See [Migration Guide](#migration-guide) below.

---

## Key Highlights

### 1. Participant Abstraction Layer

Agents now have their own identity records alongside users, managed through a new `participants` table.

- **Unified Identity**: Both users and agents are represented as `Participant` records with `type` (user/agent), `display_name`, and `avatar_url`.
- **Auto-Lifecycle**: Participant records are automatically created during user registration and agent creation, synced on updates, and cleaned up on deletion.
- **Future-Ready**: Lays the groundwork for agents to act as approvers, independent collaborators, and autonomous actors in workflows.

### 2. Agent-to-Agent Chat Improvements

Agent conversations are now fully visible and properly rendered in the Chat UI.

- **Dual Visibility**: Agent-to-agent sessions now appear in **both** participating agents' Chat tabs (under "All Users").
- **Scope Separation**: Agent-to-agent sessions are excluded from "My Sessions" — they only appear under "All Users", keeping the personal session list clean.
- **Tool Call Rendering**: Inline tool calls (e.g., `web_search`, `read_file`) in agent conversations are parsed and rendered with the ⚡ collapsible UI blocks instead of raw text.
- **Sender Identity**: Each message in agent conversations displays the sending agent's name with a 🤖 label.
- **Read-Only View**: Agent-to-agent conversations open in read-only mode with proper Markdown rendering.

### 3. Multimodal Vision Support

Agents can now see and understand images when powered by vision-capable models.

- **Vision Toggle**: A new `supports_vision` flag on the LLM model pool allows per-model vision enablement.
- **Image Upload**: Chat file uploads automatically encode images as base64 data URLs for the Vision API.
- **Auto-Conversion**: The `call_llm` function detects `[image_data:...]` markers and converts them to OpenAI Vision API format (content array with `image_url` parts).
- **UI Indicators**: Vision-capable models display a 👁 badge in the model list, and uploaded images show as thumbnails in the chat window.

### 4. WebSocket Stability

- **Setup Failed Fix**: Backend now sends close code `4002` on configuration errors (no model, setup failure), and the frontend stops reconnecting immediately.
- **Error Deduplication**: Repeated identical error messages no longer pile up in the chat window.

---

## Infrastructure Changes

| Area | Change |
|------|--------|
| **New Table** | `participants` — unified identity for users and agents |
| **Modified Table** | `chat_sessions` — added `participant_id`, `peer_agent_id` columns |
| **Modified Table** | `chat_messages` — added `participant_id` column |
| **Modified Table** | `llm_models` — added `supports_vision` column |
| **Removed Table** | `messages` — deprecated, replaced by `chat_sessions` + `chat_messages` |
| **Removed File** | `app/models/message.py` — model for deprecated `messages` table |
| **Modified File** | `entrypoint.sh` — replaced `message` import with `participant` |

### Files Changed (24 files, +679 / -329)

**Backend**: `chat_sessions.py`, `websocket.py`, `auth.py`, `agents.py`, `activity.py`, `messages.py`, `upload.py`, `enterprise.py`, `agent_tools.py`, `supervision_reminder.py`, `agent_seeder.py`, `seed.py`, `main.py`, `entrypoint.sh`, `alembic/env.py`, `alembic/versions/add_participants.py`  
**Models**: `participant.py` (new), `chat_session.py`, `audit.py`, `llm.py`, `message.py` (deleted)  
**Frontend**: `AgentDetail.tsx`, `Chat.tsx`, `EnterpriseSettings.tsx`

---

## Migration Guide

> [!CAUTION]
> The migration script below is **required** for existing installations upgrading to v1.4.0. It creates the new `participants` table columns, backfills identity records, and removes the deprecated `messages` table.

### For Docker Compose Deployments

After pulling the latest code and rebuilding containers:

```bash
# 1. Pull latest code and rebuild
cd /path/to/Clawith
git pull origin main
docker compose up --build -d

# 2. Wait for backend to start
sleep 15

# 3. Run the migration script
docker compose exec -T postgres psql -U clawith -d clawith << 'EOSQL'

-- ============================================
-- Clawith v1.4.0 Migration Script
-- ============================================

-- Step 1: Add new columns to chat_sessions (idempotent)
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS participant_id UUID;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS peer_agent_id UUID;

-- Step 2: Add new column to chat_messages (idempotent)
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS participant_id UUID;

-- Step 3: Add supports_vision to llm_models (idempotent)
ALTER TABLE llm_models ADD COLUMN IF NOT EXISTS supports_vision BOOLEAN DEFAULT FALSE;

-- Step 4: Backfill participants for all existing users
INSERT INTO participants (id, type, ref_id, display_name, avatar_url)
SELECT gen_random_uuid(), 'user', id, COALESCE(display_name, username), avatar_url
FROM users
ON CONFLICT DO NOTHING;

-- Step 5: Backfill participants for all existing agents
INSERT INTO participants (id, type, ref_id, display_name, avatar_url)
SELECT gen_random_uuid(), 'agent', id, name, avatar_url
FROM agents
ON CONFLICT DO NOTHING;

-- Step 6: Backfill participant_id on chat_sessions
UPDATE chat_sessions cs
SET participant_id = p.id
FROM participants p
WHERE p.type = 'user' AND p.ref_id = cs.user_id
AND cs.participant_id IS NULL;

-- Step 7: Backfill participant_id on chat_messages
UPDATE chat_messages cm
SET participant_id = p.id
FROM participants p
WHERE p.type = 'user' AND p.ref_id = cm.user_id
AND cm.participant_id IS NULL;

-- Step 8: Drop deprecated messages table
DROP TABLE IF EXISTS messages;
DROP TYPE IF EXISTS msg_participant_type_enum;
DROP TYPE IF EXISTS msg_type_enum;

-- Verify
SELECT 'participants' AS table_name, count(*) FROM participants
UNION ALL
SELECT 'chat_sessions (backfilled)', count(*) FROM chat_sessions WHERE participant_id IS NOT NULL
UNION ALL
SELECT 'chat_messages (backfilled)', count(*) FROM chat_messages WHERE participant_id IS NOT NULL;

EOSQL
```

### Expected Output

```
ALTER TABLE     (×3)
INSERT 0 N      — N user participants created
INSERT 0 M      — M agent participants created
UPDATE X        — X chat sessions backfilled
UPDATE Y        — Y chat messages backfilled
DROP TABLE
DROP TYPE       (×2)

 table_name                  | count
-----------------------------+------
 participants                |   N+M
 chat_sessions (backfilled)  |   X
 chat_messages (backfilled)  |   Y
```

### For Local Development

If using `restart.sh` (non-Docker), connect to your local PostgreSQL and run the same SQL block above.
