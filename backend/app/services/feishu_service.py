"""Feishu (Lark) OAuth and API integration service."""

import json
from collections import OrderedDict
from contextvars import ContextVar

import httpx
from loguru import logger

try:
    import lark_oapi as lark
    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    _HAS_LARK = False
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.models.user import User, Identity
from app.models.identity import IdentityProvider

settings = get_settings()

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/oidc/access_token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
FEISHU_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# --- Per-user identity fallback -----------------------------------------------
# Agent tools call Feishu with the app identity.  When the app was never granted
# a scope, Feishu answers with a permission error and the caller may retry under
# the end user's own Feishu identity.  These context variables carry that retry
# across the whole call tree without threading a token through every signature:
#
#   feishu_user_token_override  set for the duration of a user-identity retry, so
#                               every token lookup returns the user token
#   feishu_permission_denied    set when Feishu rejected a call for lack of
#                               permission, so the caller knows a retry is worth
#                               attempting
#   feishu_calls_succeeded      how many calls already took effect, so a
#                               partially applied write is never replayed under
#                               a second identity
feishu_user_token_override: ContextVar[str | None] = ContextVar(
    "feishu_user_token_override",
    default=None,
)
feishu_permission_denied: ContextVar[bool] = ContextVar(
    "feishu_permission_denied",
    default=False,
)
feishu_calls_succeeded: ContextVar[int] = ContextVar(
    "feishu_calls_succeeded",
    default=0,
)

# Feishu reports a missing scope or a forbidden resource through several codes;
# 99991672 is the one returned for "app has not been granted this scope".
FEISHU_PERMISSION_CODES = frozenset(
    {
        99991672,
        99991663,
        99991661,
        99991668,
        10006,
        91403,
        1063001,
        1063004,
        230002,
    }
)
FEISHU_PERMISSION_KEYWORDS = (
    "permission",
    "forbidden",
    "no access",
    "access denied",
    "scope",
    "403",
)


def feishu_denies_permission(code: object, msg: object) -> bool:
    """Return whether a Feishu code/msg pair means "identity lacks access"."""
    if code is None or code == 0:
        return False
    if code in FEISHU_PERMISSION_CODES:
        return True
    msg_lower = str(msg or "").lower()
    return any(keyword in msg_lower for keyword in FEISHU_PERMISSION_KEYWORDS)


class FeishuAPIError(RuntimeError):
    """Structured Feishu API error that preserves provider-returned details."""

    def __init__(
        self,
        *,
        stage: str,
        http_status: int | None = None,
        code: int | None = None,
        msg: str = "",
        log_id: str | None = None,
        troubleshooter: str | None = None,
        message_id: str | None = None,
    ):
        self.stage = stage
        self.http_status = http_status
        self.code = code
        self.msg = msg or "Unknown Feishu error"
        self.log_id = log_id
        self.troubleshooter = troubleshooter
        self.message_id = message_id

        parts = [f"Feishu {stage} failed"]
        if self.http_status is not None:
            parts.append(f"HTTP {self.http_status}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        parts.append(f"msg={self.msg}")
        if self.log_id:
            parts.append(f"log_id={self.log_id}")
        if self.troubleshooter:
            parts.append(f"troubleshooter={self.troubleshooter}")
        super().__init__(", ".join(parts))

    @property
    def user_message(self) -> str:
        base = self.msg
        if self.code is not None:
            base = f"{base} (code {self.code})"
        if self.troubleshooter:
            return (
                f"{base}\n"
                f"{self.troubleshooter}"
            )
        return base


# ──────────────────────────────────────────────────────────────────
# CardKit 2.0 card builders
#
# Action button callback protocol (event.action.value when user clicks):
#   {
#     "v": 1,                              # schema version
#     "kind": "card_action" | "approval",  # routing kind
#     "agent_id": "<uuid>",                # source agent (server validates)
#     "action_id": "<string>",             # business id (idempotency)
#     "label": "通过",                      # button text → synthetic user msg
#     "context": { ... }                   # optional structured payload
#   }
# ──────────────────────────────────────────────────────────────────

_CARD_HEADER_TEMPLATE_DEFAULT = "blue"


def _build_card_header(title: str | None, template: str = _CARD_HEADER_TEMPLATE_DEFAULT) -> dict | None:
    if not title:
        return None
    return {
        "template": template,
        "title": {"tag": "plain_text", "content": str(title)[:100]},
    }


def _build_button_element(
    *,
    label: str,
    action_value: dict,
    button_type: str = "default",
    confirm: dict | None = None,
) -> dict:
    """Build a single CardKit 2.0 button with a callback behavior."""
    el: dict = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": str(label)[:30]},
        "type": button_type if button_type in ("default", "primary", "danger", "text") else "default",
        "behaviors": [{"type": "callback", "value": action_value}],
    }
    if confirm:
        el["confirm"] = {
            "title": {"tag": "plain_text", "content": str(confirm.get("title", "确认"))[:50]},
            "text": {"tag": "plain_text", "content": str(confirm.get("text", ""))[:200]},
        }
    return el


def _wrap_buttons_in_row(buttons: list[dict]) -> dict:
    # CardKit Schema 2.0 dropped `{tag: action, actions: [...]}`. Use column_set
    # with one column per button for a horizontal row layout.
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "horizontal_align": "left",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [btn]}
            for btn in buttons
        ],
    }


def _wrap_card(elements: list[dict], title: str | None, summary: str | None) -> dict:
    config: dict = {"wide_screen_mode": True, "update_multi": True}
    if summary:
        config["summary"] = {"content": str(summary)[:60]}
    card: dict = {
        "schema": "2.0",
        "config": config,
        "body": {"elements": elements},
    }
    header = _build_card_header(title)
    if header:
        card["header"] = header
    return card


def build_kv_card(
    title: str | None,
    fields: list[dict],
    summary: str | None = None,
) -> dict:
    """Title + key/value field list. `fields`: [{"key": "...", "value": "...", "color"?: "..."}]."""
    if not fields:
        elements: list[dict] = [{"tag": "markdown", "content": "_（无字段）_"}]
    else:
        lines = []
        for f in fields:
            key = str(f.get("key", "")).strip()
            value = str(f.get("value", "")).strip()
            color = f.get("color")
            if not key and not value:
                continue
            value_md = f"<font color='{color}'>{value}</font>" if color else value
            lines.append(f"**{key}**: {value_md}" if key else value_md)
        elements = [{"tag": "markdown", "content": "\n".join(lines) or "_（无字段）_"}]
    return _wrap_card(elements, title, summary)


def build_actions_card(
    title: str | None,
    body: str,
    actions: list[dict],
    summary: str | None = None,
    *,
    agent_id: str,
) -> dict:
    """Body markdown + a row of buttons. `actions`: [{"label": "...", "action_id": "...", "style"?: "primary|default|danger", "context"?: {...}, "confirm"?: {...}}]."""
    elements: list[dict] = []
    if body:
        elements.append({"tag": "markdown", "content": body})
    button_elements = []
    for a in actions or []:
        label = a.get("label")
        action_id = a.get("action_id") or ""
        if not label:
            continue
        action_value = {
            "v": 1,
            "kind": "card_action",
            "agent_id": agent_id,
            "action_id": str(action_id),
            "label": str(label),
            "context": a.get("context") or {},
        }
        button_elements.append(_build_button_element(
            label=label,
            action_value=action_value,
            button_type=a.get("style") or "default",
            confirm=a.get("confirm"),
        ))
    if button_elements:
        elements.append(_wrap_buttons_in_row(button_elements))
    return _wrap_card(elements, title, summary)


def build_table_card(
    title: str | None,
    columns: list[str],
    rows: list[list[str]],
    summary: str | None = None,
) -> dict:
    """Title + table. CardKit 2.0 has a `table` element."""
    cols = [str(c) for c in (columns or [])]
    safe_rows = []
    for row in (rows or []):
        safe_rows.append([str(cell) for cell in row][: len(cols)] if cols else [str(cell) for cell in row])
    if not cols or not safe_rows:
        elements: list[dict] = [{"tag": "markdown", "content": "_（空表）_"}]
    else:
        # Use markdown table fallback (universally supported, simpler than `tag: table`).
        # If we ever need sortable / scrollable tables, switch to tag=table here.
        header_line = "| " + " | ".join(cols) + " |"
        sep_line = "| " + " | ".join(["---"] * len(cols)) + " |"
        body_lines = ["| " + " | ".join(r + [""] * (len(cols) - len(r))) + " |" for r in safe_rows]
        md = "\n".join([header_line, sep_line] + body_lines)
        elements = [{"tag": "markdown", "content": md}]
    return _wrap_card(elements, title, summary)


def build_approval_card(
    title: str,
    summary_text: str,
    approval_id: str,
    *,
    agent_id: str,
    approve_label: str = "通过",
    reject_label: str = "拒绝",
    require_reason: bool = False,
    summary: str | None = None,
    extra_context: dict | None = None,
) -> dict:
    """Approval card: body + 通过/拒绝 buttons. action.value.kind = 'approval'."""
    elements: list[dict] = []
    if summary_text:
        elements.append({"tag": "markdown", "content": summary_text})

    base_ctx = {"approval_id": str(approval_id), "require_reason": bool(require_reason)}
    if extra_context:
        base_ctx.update(extra_context)

    approve_value = {
        "v": 1,
        "kind": "approval",
        "agent_id": agent_id,
        "action_id": f"approve:{approval_id}",
        "label": approve_label,
        "context": {**base_ctx, "decision": "approve"},
    }
    reject_value = {
        "v": 1,
        "kind": "approval",
        "agent_id": agent_id,
        "action_id": f"reject:{approval_id}",
        "label": reject_label,
        "context": {**base_ctx, "decision": "reject"},
    }
    elements.append(_wrap_buttons_in_row([
        _build_button_element(label=approve_label, action_value=approve_value, button_type="primary"),
        _build_button_element(label=reject_label, action_value=reject_value, button_type="danger"),
    ]))
    return _wrap_card(elements, title, summary)


class FeishuService:
    """Service for Feishu OAuth login and message API."""

    # Maximum number of lark SDK client instances to keep alive simultaneously.
    # Each entry corresponds to a unique (app_id, app_secret) pair.  Excess entries
    # are evicted in LRU order (oldest-accessed first) to bound memory usage in
    # long-running multi-tenant deployments.
    _LARK_CLIENT_CACHE_MAX = 50

    def __init__(self):
        self.app_id = settings.FEISHU_APP_ID
        self.app_secret = settings.FEISHU_APP_SECRET
        self._app_access_token: str | None = None
        # OrderedDict is used as a simple LRU cache: move_to_end() on each hit
        # keeps the most-recently-used entries at the tail so we can evict from
        # the head when the cache is full.
        self._lark_clients: OrderedDict[str, lark.Client] = OrderedDict()

    @staticmethod
    def _parse_api_response(
        resp: httpx.Response,
        *,
        stage: str,
        message_id: str | None = None,
    ) -> dict:
        """Parse Feishu API response and verify both HTTP status and business code."""
        try:
            data = resp.json()
        except Exception as e:
            logger.warning(
                f"[Feishu] {stage} returned non-JSON response "
                f"(http_status={resp.status_code}, message_id={message_id}): {e}"
            )
            raise FeishuAPIError(
                stage=stage,
                http_status=resp.status_code,
                msg="Provider returned invalid JSON",
                message_id=message_id,
            ) from e

        error_info = data.get("error") if isinstance(data, dict) else {}
        log_id = error_info.get("log_id") if isinstance(error_info, dict) else None
        troubleshooter = error_info.get("troubleshooter") if isinstance(error_info, dict) else None

        code = data.get("code") if isinstance(data, dict) else None
        msg = data.get("msg", "") if isinstance(data, dict) else ""

        if not 200 <= resp.status_code < 300:
            logger.warning(
                f"[Feishu] {stage} HTTP failure "
                f"(http_status={resp.status_code}, message_id={message_id}, body={str(data)[:300]})"
            )
            if resp.status_code == 403 or feishu_denies_permission(code, msg):
                feishu_permission_denied.set(True)
            raise FeishuAPIError(
                stage=stage,
                http_status=resp.status_code,
                code=code,
                msg=msg or "Provider rejected the HTTP request",
                log_id=log_id,
                troubleshooter=troubleshooter,
                message_id=message_id,
            )

        if code != 0:
            logger.warning(
                f"[Feishu] {stage} business failure "
                f"(message_id={message_id}, code={code}, msg={msg})"
            )
            if feishu_denies_permission(code, msg):
                feishu_permission_denied.set(True)
            raise FeishuAPIError(
                stage=stage,
                http_status=resp.status_code,
                code=code,
                msg=msg or "Provider response omitted a successful business code",
                log_id=log_id,
                troubleshooter=troubleshooter,
                message_id=message_id,
            )

        # Counted so a caller retrying under another identity can tell whether
        # any call in the current operation already took effect.
        feishu_calls_succeeded.set(feishu_calls_succeeded.get() + 1)
        return data

    async def get_app_access_token(self) -> str:
        """Get or refresh the app-level access token. Deprecated: Use get_tenant_access_token instead."""
        return await self.get_tenant_access_token(self.app_id, self.app_secret)
        
    async def get_tenant_access_token(self, app_id: str = None, app_secret: str = None) -> str:
        """Get or refresh the app-level access token (tenant_access_token).

        While a user-identity retry is in flight the override wins, so every call
        in that retry — including nested lookups — acts as the authorizing user.
        """
        override = feishu_user_token_override.get()
        if override:
            return override

        target_app_id = app_id or self.app_id
        target_app_secret = app_secret or self.app_secret
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": target_app_id,
                "app_secret": target_app_secret,
            })
            data = resp.json()
            
            token = data.get("tenant_access_token") or data.get("app_access_token", "")
            if not app_id: # only cache default app token
                self._app_access_token = token
                
            return token

    async def exchange_code_for_user(self, code: str) -> dict:
        """Exchange OAuth authorization code for user info.

        Returns dict with: open_id, union_id, user_id, name, email, avatar_url
        """
        app_token = await self.get_app_access_token()

        async with httpx.AsyncClient() as client:
            # Get user access token
            token_resp = await client.post(FEISHU_TOKEN_URL, json={
                "grant_type": "authorization_code",
                "code": code,
            }, headers={"Authorization": f"Bearer {app_token}"})
            token_data = token_resp.json()
            user_access_token = token_data.get("data", {}).get("access_token", "")

            # Get user info
            info_resp = await client.get(FEISHU_USER_INFO_URL, headers={
                "Authorization": f"Bearer {user_access_token}",
            })
            info_data = info_resp.json().get("data", {})

            return {
                "open_id": info_data.get("open_id"),
                "union_id": info_data.get("union_id"),
                "user_id": info_data.get("user_id"),
                "name": info_data.get("name", ""),
                "email": info_data.get("email", ""),
                "avatar_url": info_data.get("avatar_url", ""),
            }

    async def login_or_register(self, db: AsyncSession, feishu_user: dict, tenant_id: str | None = None) -> tuple[User, str]:
        """Login existing user or register new one via Feishu SSO.

        Uses OrgMember as the identity anchor (synced from Feishu org directory).
        Returns (user, jwt_token)
        """
        from app.models.org import OrgMember

        open_id = feishu_user["open_id"]
        user_id = feishu_user.get("user_id", "")
        union_id = feishu_user.get("union_id")
        fs_email = feishu_user.get("email", "")
        fs_name = feishu_user.get("name", "")
        fs_avatar = feishu_user.get("avatar_url", "")

        # Resolve provider (needed for OrgMember.provider_id scoping)
        provider_query = select(IdentityProvider).where(IdentityProvider.provider_type == "feishu")
        provider_query = provider_query.where(IdentityProvider.tenant_id == tenant_id)
        provider_result = await db.execute(provider_query)
        provider = provider_result.scalars().first()
        if not provider:
            provider = IdentityProvider(
                provider_type="feishu",
                name="Feishu",
                is_active=True,
                config={"app_id": self.app_id, "app_secret": self.app_secret},
                tenant_id=tenant_id,
            )
            db.add(provider)
            await db.flush()

        # 1. Look up OrgMember by open_id (primary) or external_id (user_id)
        #    Also filter by tenant_id and provider_id for accuracy
        member = None
        if open_id:
            member_r = await db.execute(
                select(OrgMember).where(
                    OrgMember.open_id == open_id,
                    OrgMember.provider_id == provider.id,
                    OrgMember.status == "active",
                )
            )
            member = member_r.scalars().first()
        if not member and user_id:
            member_r = await db.execute(
                select(OrgMember).where(
                    OrgMember.external_id == user_id,
                    OrgMember.provider_id == provider.id,
                    OrgMember.status == "active",
                )
            )
            member = member_r.scalars().first()

        # 2. Resolve User from OrgMember
        user = None
        if member and member.user_id:
            u_result = await db.execute(select(User).where(User.id == member.user_id))
            user = u_result.scalars().first()

        # 3. Fallback: find by email matching (exact match)
        if not user and fs_email:
            query = select(User).join(User.identity).where(Identity.email == fs_email)
            if tenant_id:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query)
            user = result.scalars().first()

        if user:
            # Existing user — sync latest profile from Feishu
            if fs_avatar:
                user.avatar_url = fs_avatar
            if (not user.email or user.email.endswith("@feishu.local")) and fs_email:
                user.email = fs_email
            if fs_name:
                user.display_name = fs_name
            # Update identity fields (user_id only)
            if user_id:
                user.external_id = user_id
                user.feishu_user_id = user_id
            # Link to OrgMember if not yet bound
            if member and not member.user_id:
                member.user_id = user.id
        else:
            # New user — create account
            username = fs_email.split("@")[0] if fs_email else f"feishu_{open_id[:8]}"
            email = fs_email or f"{username}@feishu.local"

            # Ensure unique username within tenant
            query = (
                select(User)
                .join(User.identity)
                .where(Identity.username == username)
            )
            if tenant_id:
                query = query.where(User.tenant_id == tenant_id)
            
            existing = await db.execute(query)
            if existing.scalar_one_or_none():
                import uuid
                username = f"{username}_{uuid.uuid4().hex[:6]}"

            # Step 1: Find or create global Identity using unified registration service
            from app.services.registration_service import registration_service
            # No phone available in this specific Feishu login block, but it handles email/username matching
            identity = await registration_service.find_or_create_identity(
                email=email,
                phone=feishu_user.get("mobile"),
                username=username,
                password=open_id,
            )

            # Step 2: Create tenant-scoped User linked to Identity
            user = User(
                identity_id=identity.id,
                display_name=fs_name or username,
                avatar_url=fs_avatar or None,
                registration_source="feishu",
                tenant_id=tenant_id,
                is_active=True,
            )

            db.add(user)
            await db.flush()

            # Link back to OrgMember if found
            if member:
                member.user_id = user.id

        await db.flush()

        token = create_access_token(str(user.id), user.role)
        return user, token


    async def send_message(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        msg_type: str,
        content: str,
        receive_id_type: str = "open_id",
        stage: str = "send_message",
    ) -> dict:
        """Send a message via a specific Feishu bot (per-agent credentials).

        Args:
            app_id: The Feishu app's App ID (per-agent)
            app_secret: The Feishu app's App Secret (per-agent)
            receive_id: Target user's open_id
            msg_type: "text", "interactive", etc.
            content: JSON string of message content
            receive_id_type: "open_id" or "chat_id"
        """
        # Get app access token for this specific agent's bot
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={
                    "receive_id": receive_id,
                    "msg_type": msg_type,
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = self._parse_api_response(resp, stage=stage)
            return data

    async def patch_message(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        content: str,
        stage: str = "patch_message",
    ) -> dict:
        """Patch an existing message (e.g. updating an interactive card for streaming)."""
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            resp = await client.patch(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                json={
                    "content": content,
                },
                headers={"Authorization": f"Bearer {app_token}"},
            )
            data = self._parse_api_response(resp, stage=stage, message_id=message_id)
            return data

    async def resolve_open_id(self, app_id: str, app_secret: str,
                               email: str | None = None, mobile: str | None = None) -> str | None:
        """Resolve a user's open_id for a specific app using email or mobile.

        Each Feishu app gets a unique open_id per user. This method looks up the
        correct open_id for the given app's credentials.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            body: dict = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp = await client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "open_id"},
            )
            data = resp.json()
            if data.get("code") != 0:
                return None

            user_list = data.get("data", {}).get("user_list", [])
            for u in user_list:
                oid = u.get("user_id")
                if oid:
                    return oid
            return None

    async def resolve_user_id(self, app_id: str, app_secret: str,
                               email: str | None = None, mobile: str | None = None) -> str | None:
        """Resolve a user's tenant-level user_id using email or mobile.

        Unlike open_id, user_id is stable across all apps within the same tenant.
        Requires contact:user.employee_id:readonly permission.
        """
        if not email and not mobile:
            return None

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")

            body: dict = {}
            if email:
                body["emails"] = [email]
            if mobile:
                body["mobiles"] = [mobile]

            resp = await client.post(
                "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
                json=body,
                headers={"Authorization": f"Bearer {app_token}"},
                params={"user_id_type": "user_id"},
            )
            data = resp.json()
            if data.get("code") != 0:
                return None

            user_list = data.get("data", {}).get("user_list", [])
            for u in user_list:
                uid = u.get("user_id")
                if uid:
                    return uid
            return None

    async def send_card_message(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        card_dict: dict,
        receive_id_type: str = "open_id",
        stage: str = "send_card_message",
    ) -> dict:
        """Send a card inline as an `interactive` message.

        Unlike the CardKit entity flow this keeps the card JSON in the IM
        message itself, so the returned `message_id` can later be handed to
        `patch_message` to update the very same bubble in place.
        """
        return await self.send_message(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=receive_id,
            msg_type="interactive",
            content=json.dumps(card_dict, ensure_ascii=False),
            receive_id_type=receive_id_type,
            stage=stage,
        )

    async def send_markdown_message(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
        title: str | None = None,
        stage: str = "send_markdown_message",
    ) -> dict:
        """Send a message that renders markdown to a Feishu user or chat.

        Uses CardKit (schema 2.0) to render headings, bold, lists, tables,
        code blocks etc. — the same path the streaming reply uses. Falls
        back to a plain `text` message if CardKit fails.
        """
        if not text or not text.strip():
            return {"code": 0, "msg": "skipped_empty"}

        card_dict: dict = {
            "schema": "2.0",
            "config": {"streaming_mode": False},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": text, "text_align": "left"},
                ],
            },
        }
        if title:
            card_dict["header"] = {
                "template": "blue",
                "title": {"tag": "plain_text", "content": title[:100]},
            }

        try:
            card_id = await self.create_card_entity(app_id, app_secret, card_dict)
            await self.send_card_by_card_id(
                app_id, app_secret, receive_id, card_id,
                receive_id_type=receive_id_type,
            )
            return {"code": 0, "msg": "ok", "card_id": card_id}
        except Exception as e:
            logger.warning(f"[Feishu] CardKit markdown send failed: {e}; falling back to plain text")

        return await self.send_message(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=receive_id,
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            receive_id_type=receive_id_type,
            stage=f"{stage}_text_fallback",
        )

    async def send_approval_card(self, app_id: str, app_secret: str,
                                  creator_open_id: str, agent_name: str,
                                  action_type: str, details: str, approval_id: str) -> dict:
        """Send an interactive approval card to the agent creator via Feishu."""
        import json
        card_content = json.dumps({
            "type": "template",
            "data": {
                "template_id": "",  # Use custom card
                "template_variable": {
                    "agent_name": agent_name,
                    "action_type": action_type,
                    "details": details,
                    "approval_id": approval_id,
                }
            }
        })
        # Simplified — in production, use Feishu interactive card JSON
        text_content = json.dumps({
            "text": f"🔴 [{agent_name}] 请求审批\n操作: {action_type}\n详情: {details}\n\n请在 Clawith 平台审批。"
        })
        return await self.send_message(app_id, app_secret, creator_open_id, "text", text_content)

    async def download_message_resource(self, app_id: str, app_secret: str,
                                         message_id: str, file_key: str,
                                         resource_type: str = "file") -> bytes:
        """Download a file or image from a Feishu message.

        Args:
            resource_type: "file" or "image"
        Returns raw file bytes.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id,
                "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {app_token}"},
            )
            resp.raise_for_status()
            return resp.content

    async def upload_and_send_file(self, app_id: str, app_secret: str,
                                    receive_id: str, file_path,
                                    receive_id_type: str = "open_id",
                                    accompany_msg: str = "") -> dict:
        """Upload a local file to Feishu and send it as a file message.

        Returns the send_message response dict.
        """
        import json as _json
        from pathlib import Path as _Path
        fp = _Path(file_path)
        async with httpx.AsyncClient(timeout=60) as client:
            # Get token
            token_resp = await client.post(FEISHU_APP_TOKEN_URL, json={
                "app_id": app_id, "app_secret": app_secret,
            })
            app_token = token_resp.json().get("app_access_token", "")
            headers = {"Authorization": f"Bearer {app_token}"}

            # Upload file
            with open(fp, "rb") as f:
                file_bytes = f.read()
            # Determine file type for Feishu upload
            ext = fp.suffix.lower()
            feishu_file_type = "stream"  # generic binary
            if ext in (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md"):
                feishu_file_type = "stream"
            upload_resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                files={"file": (fp.name, file_bytes, "application/octet-stream")},
                data={"file_type": feishu_file_type, "file_name": fp.name},
                headers=headers,
            )
            upload_data = upload_resp.json()
            if upload_data.get("code") != 0:
                raise RuntimeError(f"Feishu file upload failed: {upload_data.get('msg')}")
            file_key = upload_data["data"]["file_key"]

            # Send text accompany message first if provided
            if accompany_msg:
                text_resp = await client.post(
                    f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                    json={"receive_id": receive_id, "msg_type": "text",
                          "content": _json.dumps({"text": accompany_msg})},
                    headers=headers,
                )
                if text_resp.status_code != 200:
                    logger.error(
                        f"[Feishu] Failed to send text accompany message: "
                        f"status={text_resp.status_code}, body={text_resp.text}, "
                        f"receive_id={receive_id}, receive_id_type={receive_id_type}"
                    )

            # Send file message
            resp = await client.post(
                f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
                json={"receive_id": receive_id, "msg_type": "file",
                      "content": _json.dumps({"file_key": file_key})},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error(
                    f"[Feishu] Failed to send file message: "
                    f"status={resp.status_code}, body={resp.text}, "
                    f"receive_id={receive_id}, receive_id_type={receive_id_type}, "
                    f"file_key={file_key}"
                )
            return resp.json()

    # --- Bitable (多维表格) API ---

    async def bitable_list_tables(self, app_id: str, app_secret: str, app_token: str, *, access_token: str | None = None) -> dict:
        """List all tables in a Bitable app."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_list_tables",
            )

    async def bitable_list_fields(self, app_id: str, app_secret: str, app_token: str, table_id: str, *, access_token: str | None = None) -> dict:
        """List all fields in a specific table."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_list_fields",
            )

    async def bitable_query_records(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        filters: dict | None = None,
        *,
        access_token: str | None = None,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> dict:
        """Query records in a specific table."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        body = dict(filters) if filters else {}
        params: dict[str, object] = {
            "page_size": max(1, min(page_size, 500)),
        }
        if page_token:
            params["page_token"] = page_token
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
                json=body,
                params=params,
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_query_records",
            )

    async def bitable_create_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, fields: dict, *, access_token: str | None = None) -> dict:
        """Create a new record in a specific table."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_create_record",
            )

    async def bitable_update_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str, fields: dict, *, access_token: str | None = None) -> dict:
        """Update an existing record in a specific table."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                json={"fields": fields},
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_update_record",
            )
            
    async def bitable_delete_record(self, app_id: str, app_secret: str, app_token: str, table_id: str, record_id: str, *, access_token: str | None = None) -> dict:
        """Delete an existing record in a specific table."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(
                resp,
                stage="bitable_delete_record",
            )

    async def bitable_create_app(self, app_id: str, app_secret: str, name: str, folder_token: str = "", *, access_token: str | None = None) -> dict:
        """Create a new Bitable (多维表格) app.

        Uses the Bitable v1 apps API: POST /open-apis/bitable/v1/apps
        If folder_token is empty, the file is created in the root 'My Drive'.

        Args:
            name:         The display name of the new Bitable (max 255 chars).
            folder_token: Parent folder token (optional). Leave empty for root.
        Returns:
            API response dict containing 'data.app.app_token' as the new app_token.
        """
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        body: dict = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/bitable/v1/apps",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            return self._parse_api_response(
                resp,
                stage="bitable_create_app",
            )


    # --- Docs API ---
    async def read_feishu_doc(self, app_id: str, app_secret: str, document_id: str, *, access_token: str | None = None) -> dict:
        """Get pure text content of a new-version Feishu Doc (docx)."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content",
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(resp, stage="doc_read")

    async def search_feishu_doc(self, app_id: str, app_secret: str, payload: dict, *, access_token: str | None = None) -> dict:
        """Search Feishu documents by keyword via the Docs Search API."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            return resp.json()

    async def create_feishu_doc(self, app_id: str, app_secret: str, folder_token: str | None = None, title: str = "Untitled Document", *, access_token: str | None = None) -> dict:
        """Create a new Feishu Doc (docx)."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        body = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                json=body,
                headers={"Authorization": f"Bearer {token}"}
            )
            return self._parse_api_response(resp, stage="doc_create")

    async def append_feishu_doc(self, app_id: str, app_secret: str, document_id: str, content: str, *, access_token: str | None = None) -> dict:
        """Append text to the end of a Feishu Doc (document_id is also the root block_id)."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        # Convert plain text to a text block
        body = {
            "children": [
                {
                    "block_type": 2, # Text block (paragraph)
                    "text": {
                        "elements": [
                            {
                                "text_run": {
                                    "content": content
                                }
                            }
                        ]
                    }
                }
            ]
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                json=body,
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.json()

    async def append_feishu_doc_blocks(self, app_id: str, app_secret: str, document_id: str, block_id: str, blocks: list, *, access_token: str | None = None) -> dict:
        """Append pre-parsed Markdown blocks to a Feishu doc block (e.g., body_block_id)."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children",
                json={"children": blocks},
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.json()

    # --- Approval API ---
    async def create_approval_instance(self, app_id: str, app_secret: str, approval_code: str, user_id: str, form_data: str, *, access_token: str | None = None) -> dict:
        """Create a Feishu approval instance."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        body = {
            "approval_code": approval_code,
            "user_id": user_id,
            "form": form_data
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances",
                json=body,
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.json()

    async def query_approval_instances(self, app_id: str, app_secret: str, approval_code: str, status: str = None, *, access_token: str | None = None) -> dict:
        """Query Feishu approval instances."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        body = {"approval_code": approval_code}
        if status:
            body["status"] = status
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/approval/v4/instances/query",
                json=body,
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.json()

    async def get_approval_instance(self, app_id: str, app_secret: str, instance_id: str, *, access_token: str | None = None) -> dict:
        """Get details of a specific Feishu approval instance."""
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_id}",
                headers={"Authorization": f"Bearer {token}"}
            )
            return resp.json()

    # --- IM Chat API ---

    async def search_chats(
        self,
        app_id: str,
        app_secret: str,
        query: str,
        page_size: int = 20,
        page_token: str | None = None,
        *,
        access_token: str | None = None,
    ) -> dict:
        """Search for group chats visible to the bot or user.

        GET /open-apis/im/v1/chats/search
        """
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        params: dict[str, str | int] = {"query": query, "page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/im/v1/chats/search",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.json()

    async def list_chat_messages(
        self,
        app_id: str,
        app_secret: str,
        chat_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        sort_type: str = "ByCreateTimeDesc",
        page_size: int = 50,
        page_token: str | None = None,
        *,
        access_token: str | None = None,
    ) -> dict:
        """List messages in a chat (group or P2P).

        GET /open-apis/im/v1/messages
        start_time/end_time are Unix timestamps in seconds (string).
        """
        token = access_token or await self.get_tenant_access_token(app_id, app_secret)
        params: dict[str, str | int] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": sort_type,
            "page_size": page_size,
        }
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        if page_token:
            params["page_token"] = page_token
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.json()

    # --- CardKit Streaming API ---

    def _get_lark_client(self, app_id: str, app_secret: str):
        """Get or create a cached lark-oapi SDK client for the given app credentials.

        Implements a simple LRU eviction policy: when the cache exceeds
        _LARK_CLIENT_CACHE_MAX entries, the least-recently-used client is removed.
        """
        if not _HAS_LARK:
            raise RuntimeError("lark-oapi package is not installed. Install with: pip install lark-oapi")
        cache_key = f"{app_id}:{app_secret}"
        client = self._lark_clients.get(cache_key)
        if client is None:
            # Evict the oldest entry if the cache is at capacity.
            if len(self._lark_clients) >= self._LARK_CLIENT_CACHE_MAX:
                evicted_key, _ = self._lark_clients.popitem(last=False)
                logger.debug(f"[Feishu] _lark_clients LRU evict: {evicted_key[:8]}...")
            client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
            self._lark_clients[cache_key] = client
        else:
            # Move hit entry to the tail so it is considered most-recently-used.
            self._lark_clients.move_to_end(cache_key)
        return client

    async def create_card_entity(
        self,
        app_id: str,
        app_secret: str,
        card_dict: dict,
    ) -> str:
        """Create a CardKit card entity and return its card_id."""
        from lark_oapi.api.cardkit.v1.model import (
            CreateCardRequest, CreateCardRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        body = CreateCardRequestBody.builder() \
            .type("card_json") \
            .data(json.dumps(card_dict)) \
            .build()
        request = CreateCardRequest.builder().request_body(body).build()

        try:
            resp = await client.cardkit.v1.card.acreate(request)
            logger.info(
                f"[Feishu CardKit] create_card_entity response: "
                f"code={resp.code}, msg={resp.msg}"
            )
            if not resp.success():
                raise RuntimeError(
                    f"Feishu CardKit create_card_entity failed: code={resp.code}, msg={resp.msg}"
                )
            return resp.data.card_id
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] create_card_entity error: {e}")
            raise RuntimeError(f"Feishu CardKit create_card_entity error: {e}") from e

    async def send_card_by_card_id(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        card_id: str,
        receive_id_type: str = "open_id",
    ) -> dict:
        """Send an interactive message referencing an existing card_id.

        Returns the raw send response so callers can keep the Feishu
        `message_id` — patching or reacting to the card later needs it.
        """
        content = json.dumps({
            "type": "card",
            "data": {"card_id": card_id},
        })
        return await self.send_message(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=receive_id,
            msg_type="interactive",
            content=content,
            receive_id_type=receive_id_type,
            stage="send_card_by_card_id",
        )

    async def send_card_with_fallback(
        self,
        app_id: str,
        app_secret: str,
        receive_id: str,
        receive_id_type: str,
        card_dict: dict,
        fallback_text: str,
        stage: str = "send_card_with_fallback",
    ) -> dict:
        """Create a CardKit card entity and send it; fall back to markdown text on any error.

        Returns {"code": 0, "msg": "ok", "card_id": "..."} on card success,
        or the underlying markdown send response on fallback.
        """
        try:
            card_id = await self.create_card_entity(app_id, app_secret, card_dict)
            await self.send_card_by_card_id(
                app_id, app_secret, receive_id, card_id,
                receive_id_type=receive_id_type,
            )
            return {"code": 0, "msg": "ok", "card_id": card_id}
        except Exception as e:
            logger.warning(f"[Feishu] {stage} CardKit send failed: {e}; falling back to markdown")
            return await self.send_markdown_message(
                app_id=app_id,
                app_secret=app_secret,
                receive_id=receive_id,
                text=fallback_text,
                receive_id_type=receive_id_type,
                stage=f"{stage}_md_fallback",
            )

    async def stream_card_content(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        element_id: str,
        content: str,
        sequence: int,
    ) -> None:
        """Stream content to a specific card element via CardKit API."""
        from lark_oapi.api.cardkit.v1.model import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        body = ContentCardElementRequestBody.builder() \
            .content(content) \
            .sequence(sequence) \
            .build()
        request = ContentCardElementRequest.builder() \
            .card_id(card_id) \
            .element_id(element_id) \
            .request_body(body) \
            .build()

        try:
            resp = await client.cardkit.v1.card_element.acontent(request)
            logger.info(
                f"[Feishu CardKit] stream_card_content response: "
                f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                f"element_id={element_id}, sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(
                    f"Feishu CardKit stream_card_content failed: "
                    f"code={resp.code}, msg={resp.msg}"
                )
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] stream_card_content error: {e}")
            raise RuntimeError(f"Feishu CardKit stream_card_content error: {e}") from e

    async def set_card_streaming_mode(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        streaming_mode: int,
        sequence: int,
    ) -> None:
        """Toggle streaming mode on a card via CardKit settings API."""
        from lark_oapi.api.cardkit.v1.model import (
            SettingsCardRequest, SettingsCardRequestBody,
        )

        client = self._get_lark_client(app_id, app_secret)
        body = SettingsCardRequestBody.builder() \
            .settings(json.dumps({"streaming_mode": streaming_mode})) \
            .sequence(sequence) \
            .build()
        request = SettingsCardRequest.builder() \
            .card_id(card_id) \
            .request_body(body) \
            .build()

        try:
            resp = await client.cardkit.v1.card.asettings(request)
            logger.info(
                f"[Feishu CardKit] set_card_streaming_mode response: "
                f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                f"streaming_mode={streaming_mode}, sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(
                    f"Feishu CardKit set_card_streaming_mode failed: "
                    f"code={resp.code}, msg={resp.msg}"
                )
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] set_card_streaming_mode error: {e}")
            raise RuntimeError(f"Feishu CardKit set_card_streaming_mode error: {e}") from e

    async def update_cardkit_card(
        self,
        app_id: str,
        app_secret: str,
        card_id: str,
        card_dict: dict,
        sequence: int,
    ) -> None:
        """Full card update via CardKit API."""
        from lark_oapi.api.cardkit.v1.model import (
            UpdateCardRequest, UpdateCardRequestBody, Card,
        )

        client = self._get_lark_client(app_id, app_secret)
        card = Card.builder() \
            .type("card_json") \
            .data(json.dumps(card_dict)) \
            .build()
        body = UpdateCardRequestBody.builder() \
            .card(card) \
            .sequence(sequence) \
            .build()
        request = UpdateCardRequest.builder() \
            .card_id(card_id) \
            .request_body(body) \
            .build()

        try:
            resp = await client.cardkit.v1.card.aupdate(request)
            logger.info(
                f"[Feishu CardKit] update_cardkit_card response: "
                f"code={resp.code}, msg={resp.msg}, card_id={card_id}, "
                f"sequence={sequence}"
            )
            if not resp.success():
                raise RuntimeError(
                    f"Feishu CardKit update_cardkit_card failed: "
                    f"code={resp.code}, msg={resp.msg}"
                )
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu CardKit] update_cardkit_card error: {e}")
            raise RuntimeError(f"Feishu CardKit update_cardkit_card error: {e}") from e

    async def add_message_reaction(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        emoji_type: str,
    ) -> str:
        """Add one emoji reaction to a message and return its reaction_id.

        `emoji_type` must be one of Feishu's documented keys and is
        case-sensitive (e.g. "Typing", "THUMBSUP").
        """
        from lark_oapi.api.im.v1.model import (
            CreateMessageReactionRequest, CreateMessageReactionRequestBody, Emoji,
        )

        client = self._get_lark_client(app_id, app_secret)
        body = CreateMessageReactionRequestBody.builder() \
            .reaction_type(Emoji.builder().emoji_type(emoji_type).build()) \
            .build()
        request = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()

        try:
            resp = await client.im.v1.message_reaction.acreate(request)
            if not resp.success():
                raise RuntimeError(
                    f"Feishu add_message_reaction failed: code={resp.code}, msg={resp.msg}"
                )
            return resp.data.reaction_id
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu] add_message_reaction error: {e}")
            raise RuntimeError(f"Feishu add_message_reaction error: {e}") from e

    async def list_message_reactions(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        emoji_type: str,
    ) -> list:
        """List a message's reactions of one emoji type."""
        from lark_oapi.api.im.v1.model import ListMessageReactionRequest

        client = self._get_lark_client(app_id, app_secret)
        request = ListMessageReactionRequest.builder() \
            .message_id(message_id) \
            .reaction_type(emoji_type) \
            .build()

        try:
            resp = await client.im.v1.message_reaction.alist(request)
            if not resp.success():
                raise RuntimeError(
                    f"Feishu list_message_reactions failed: code={resp.code}, msg={resp.msg}"
                )
            return list(getattr(resp.data, "items", None) or [])
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu] list_message_reactions error: {e}")
            raise RuntimeError(f"Feishu list_message_reactions error: {e}") from e

    async def delete_message_reaction(
        self,
        app_id: str,
        app_secret: str,
        message_id: str,
        reaction_id: str,
    ) -> None:
        """Remove one reaction this app previously added to a message."""
        from lark_oapi.api.im.v1.model import DeleteMessageReactionRequest

        client = self._get_lark_client(app_id, app_secret)
        request = DeleteMessageReactionRequest.builder() \
            .message_id(message_id) \
            .reaction_id(reaction_id) \
            .build()

        try:
            resp = await client.im.v1.message_reaction.adelete(request)
            if not resp.success():
                raise RuntimeError(
                    f"Feishu delete_message_reaction failed: code={resp.code}, msg={resp.msg}"
                )
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            logger.error(f"[Feishu] delete_message_reaction error: {e}")
            raise RuntimeError(f"Feishu delete_message_reaction error: {e}") from e


feishu_service = FeishuService()
