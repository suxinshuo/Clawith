"""Agent collaboration and template market API routes."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.permissions import check_agent_access
from app.core.security import get_current_user, get_current_admin
from app.dao.system_setting_dao import system_setting_dao
from app.database import get_db
from app.models.activity_log import DailyTokenUsage
from app.models.agent import Agent, AgentTemplate
from app.models.tenant import Tenant
from app.models.user import User
from app.services.collaboration import collaboration_service
from app.services.token_accounting import (
    SETTING_CALIBRATION_SWITCHED_AT,
    cache_hit_rate,
    current_enforcement_mode,
    effective_timezone,
    estimated_share,
    local_day_start,
    local_month_start,
)

router = APIRouter(tags=["advanced"])


# ─── Collaboration ──────────────────────────────────────

class DelegateRequest(BaseModel):
    to_agent_id: uuid.UUID
    task_title: str
    task_description: str = ""


class InterAgentMessage(BaseModel):
    to_agent_id: uuid.UUID
    message: str
    msg_type: str = "notify"  # notify | consult


@router.get("/agents/{agent_id}/collaborators")
async def list_collaborators(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List agents that can collaborate with this agent."""
    await check_agent_access(db, current_user, agent_id)
    return await collaboration_service.list_collaborators(db, agent_id)


@router.post("/agents/{agent_id}/collaborate/delegate")
async def delegate_task(
    agent_id: uuid.UUID,
    data: DelegateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delegate a task from one agent to another."""
    await check_agent_access(db, current_user, agent_id)
    try:
        result = await collaboration_service.delegate_task(
            db, agent_id, data.to_agent_id, data.task_title, data.task_description
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/agents/{agent_id}/collaborate/message")
async def send_inter_agent_message(
    agent_id: uuid.UUID,
    data: InterAgentMessage,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message between agents."""
    await check_agent_access(db, current_user, agent_id)
    return await collaboration_service.send_message_between_agents(
        db, agent_id, data.to_agent_id, data.message, data.msg_type
    )


# ─── Template Market ────────────────────────────────────

class TemplateCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "🤖"
    category: str = "general"
    soul_template: str = ""
    default_skills: list[str] = []
    default_autonomy_policy: dict = {}


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    icon: str
    category: str
    soul_template: str
    default_skills: list
    default_autonomy_policy: dict
    is_builtin: bool
    created_at: str | None = None

    model_config = {"from_attributes": True}


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List available agent templates."""
    query = select(AgentTemplate).order_by(AgentTemplate.name)
    if category:
        query = query.where(AgentTemplate.category == category)
    result = await db.execute(query)
    return [TemplateOut.model_validate(t) for t in result.scalars().all()]


@router.get("/templates/{template_id}", response_model=TemplateOut)
async def get_template(template_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get template details."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return TemplateOut.model_validate(template)


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    data: TemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new agent template (share to template market)."""
    template = AgentTemplate(
        name=data.name,
        description=data.description,
        icon=data.icon,
        category=data.category,
        soul_template=data.soul_template,
        default_skills=data.default_skills,
        default_autonomy_policy=data.default_autonomy_policy,
        created_by=current_user.id,
    )
    db.add(template)
    await db.flush()
    return TemplateOut.model_validate(template)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a template (admin or creator)."""
    result = await db.execute(select(AgentTemplate).where(AgentTemplate.id == template_id))
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    await db.delete(template)


# ─── Agent Handover ─────────────────────────────────────

class HandoverRequest(BaseModel):
    new_creator_id: uuid.UUID


@router.post("/agents/{agent_id}/handover")
async def handover_agent(
    agent_id: uuid.UUID,
    data: HandoverRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Transfer ownership of a digital employee to another user."""
    from app.core.permissions import is_agent_creator
    from app.models.audit import AuditLog

    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent):
        raise HTTPException(status_code=403, detail="Only creator can handover agent")

    # Verify new creator exists
    new_creator_result = await db.execute(select(User).where(User.id == data.new_creator_id))
    new_creator = new_creator_result.scalar_one_or_none()
    if not new_creator:
        raise HTTPException(status_code=404, detail="Target user not found")

    old_creator_id = agent.creator_id
    agent.creator_id = data.new_creator_id

    db.add(AuditLog(
        user_id=current_user.id,
        agent_id=agent_id,
        action="agent:handover",
        details={
            "from_creator": str(old_creator_id),
            "to_creator": str(data.new_creator_id),
        },
    ))
    await db.flush()

    return {
        "status": "transferred",
        "agent_name": agent.name,
        "new_creator": new_creator.display_name,
    }


# ─── Observability ──────────────────────────────────────

@router.get("/agents/{agent_id}/metrics")
async def get_agent_metrics(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get observability metrics for an agent."""
    from app.models.task import Task
    from app.models.audit import AuditLog, ApprovalRequest

    agent, _access = await check_agent_access(db, current_user, agent_id)

    # Task stats
    total_tasks = await db.execute(select(func.count(Task.id)).where(Task.agent_id == agent_id))
    done_tasks = await db.execute(
        select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "done")
    )
    pending_tasks = await db.execute(
        select(func.count(Task.id)).where(Task.agent_id == agent_id, Task.status == "pending")
    )

    # Approval stats
    total_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(ApprovalRequest.agent_id == agent_id)
    )
    pending_approvals = await db.execute(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.agent_id == agent_id, ApprovalRequest.status == "pending"
        )
    )

    # Recent activity count (last 24h)
    from datetime import timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_actions = await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.agent_id == agent_id, AuditLog.created_at >= cutoff
        )
    )

    # Container status
    from app.services.agent_manager import agent_manager
    container_status = agent_manager.get_container_status(agent)

    # 估算占比：Agent 表没有 estimated_tokens_* 计数列，只能读 daily_token_usage 日
    # 汇总，所以这三个窗口锚在汇总行自己的 `date` 上（ledger 写入时取
    # local_day_start(tz)），而不是 Agent.*_today/_month 计数器。用 CASE 在一趟查询
    # 里拆出三个窗口，避免三次往返。
    #
    # 关键：读取侧必须解析出与写入侧**完全相同**的时区，否则窗口边界会错位。
    # ledger._write_once() 用 effective_timezone(agent, tenant) 定锚点（见
    # ledger.py），agent.timezone 为 NULL（常见情况）时回落到租户时区。这里若只传
    # agent，effective_timezone 会直接回落到 UTC：租户时区为 Asia/Shanghai 时写入侧
    # 把"今天"锚在 16:00Z，读取侧的 day_start 却是 00:00Z，今天整行被窗口排除、
    # estimated_share_today 静默读成 0。所以这里必须自己把租户捞出来。
    #
    # load_only 让编译出的 SELECT 只取 timezone（外加主键），其余列被声明为 deferred。
    # 这里安全，因为这个实例只交给 effective_timezone()，它只读 tenant.timezone；千万
    # 别把它传给会读其他 Tenant 属性的代码 —— 那会触发懒加载，在异步 session 里同步执行
    # 而抛 MissingGreenlet。
    tenant = None
    if agent.tenant_id is not None:
        tenant = (await db.execute(
            select(Tenant).options(load_only(Tenant.timezone)).where(Tenant.id == agent.tenant_id)
        )).scalar_one_or_none()
    now = datetime.now(UTC)
    tz_name = effective_timezone(agent, tenant)
    day_start = local_day_start(tz_name, now=now)
    month_start = local_month_start(tz_name, now=now)

    def _window_sum(column, boundary):
        if boundary is None:
            return func.coalesce(func.sum(column), 0)
        return func.coalesce(func.sum(case((DailyTokenUsage.date >= boundary, column), else_=0)), 0)

    share_row = (await db.execute(
        select(
            _window_sum(DailyTokenUsage.estimated_tokens, day_start).label("est_today"),
            _window_sum(DailyTokenUsage.tokens_used, day_start).label("used_today"),
            _window_sum(DailyTokenUsage.estimated_tokens, month_start).label("est_month"),
            _window_sum(DailyTokenUsage.tokens_used, month_start).label("used_month"),
            _window_sum(DailyTokenUsage.estimated_tokens, None).label("est_total"),
            _window_sum(DailyTokenUsage.tokens_used, None).label("used_total"),
        ).where(DailyTokenUsage.agent_id == agent_id)
    )).one()

    calibration_value = await system_setting_dao.get_value(SETTING_CALIBRATION_SWITCHED_AT, {})
    calibration_switched_at = (
        calibration_value.get("at") if isinstance(calibration_value, dict) else None
    )

    # Extract scalar values (each result can only be consumed once)
    _total_tasks = total_tasks.scalar() or 0
    _done_tasks = done_tasks.scalar() or 0
    _pending_tasks = pending_tasks.scalar() or 0
    _total_approvals = total_approvals.scalar() or 0
    _pending_approvals = pending_approvals.scalar() or 0
    _recent_actions = recent_actions.scalar() or 0

    return {
        "agent_id": str(agent_id),
        "agent_name": agent.name,
        "status": agent.status,
        "container": container_status,
        "tokens": {
            "used_today": agent.tokens_used_today,
            "used_month": agent.tokens_used_month,
            "used_total": agent.tokens_used_total,
            "input_today": agent.input_tokens_today,
            "input_month": agent.input_tokens_month,
            "input_total": agent.input_tokens_total,
            "cache_read_today": agent.cache_read_tokens_today,
            "cache_read_month": agent.cache_read_tokens_month,
            "cache_read_total": agent.cache_read_tokens_total,
            "cache_creation_today": agent.cache_creation_tokens_today,
            "cache_creation_month": agent.cache_creation_tokens_month,
            "cache_creation_total": agent.cache_creation_tokens_total,
            "cache_hit_rate_today": cache_hit_rate(agent.cache_read_tokens_today, agent.input_tokens_today),
            "cache_hit_rate_month": cache_hit_rate(agent.cache_read_tokens_month, agent.input_tokens_month),
            "cache_hit_rate_total": cache_hit_rate(agent.cache_read_tokens_total, agent.input_tokens_total),
            "estimated_share_today": estimated_share(share_row.est_today, share_row.used_today),
            "estimated_share_month": estimated_share(share_row.est_month, share_row.used_month),
            "estimated_share_total": estimated_share(share_row.est_total, share_row.used_total),
            # estimated_share_* 的分母不是上面的 used_today/_month/_total，而是
            # daily_token_usage 汇总出来的这三个 basis（见上面那段注释）。两者可以差很
            # 多：020 迁移建 daily_token_usage 时不回填，所以在 020 之前就有用量的 Agent
            # 满足 SUM(daily_token_usage.tokens_used) < Agent.tokens_used_total（比值内部
            # 自洽，但绝对量不是 used_* 那个量）。把 basis 一起返回，调用方才能算出绝对
            # 量，而不是拿 share 去乘一个不相干的 used_*。
            #
            # 另外注意 basis 里 050 之前的行 estimated_tokens 恒为 0（050 建列时也不回
            # 填），那段历史的 share 因此偏低 —— 这是"少报估算"，不是"多报"，方向上安全。
            "estimated_basis_today": int(share_row.used_today or 0),
            "estimated_basis_month": int(share_row.used_month or 0),
            "estimated_basis_total": int(share_row.used_total or 0),
            "limit_day": agent.max_tokens_per_day,
            "limit_month": agent.max_tokens_per_month,
            "calibration_switched_at": calibration_switched_at,
            "budget_enforcement_mode": await current_enforcement_mode(),
        },
        "tasks": {
            "total": _total_tasks,
            "done": _done_tasks,
            "pending": _pending_tasks,
            "completion_rate": round(
                _done_tasks / max(_total_tasks, 1) * 100, 1
            ),
        },
        "approvals": {
            "total": _total_approvals,
            "pending": _pending_approvals,
        },
        "activity": {
            "actions_last_24h": _recent_actions,
        },
    }
