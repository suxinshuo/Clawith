"""Feishu emoji reaction service — "敲键盘" indicator on user messages.

Mirrors `dingtalk_reaction.py`: the webhook adds the indicator the moment a
message is accepted, and the Runtime channel delivery removes it once the reply
lands. Every function here is fire-and-forget and never raises — a cosmetic
reaction must not fail an accepted message or a confirmed reply.
"""

from loguru import logger

from app.services.feishu_service import feishu_service

# Feishu's emoji_type for 敲键盘. Case-sensitive: the API rejects unknown keys,
# and this one is not all-caps like most of the list.
TYPING_EMOJI_TYPE = "Typing"


async def add_typing_reaction(
    app_id: str,
    app_secret: str,
    message_id: str,
) -> str | None:
    """Add the 敲键盘 reaction to a user message.

    Returns the new reaction_id, or None when Feishu refused it.
    """
    if not message_id or not app_id:
        return None

    try:
        reaction_id = await feishu_service.add_message_reaction(
            app_id, app_secret, message_id, TYPING_EMOJI_TYPE
        )
        logger.info(f"[Feishu Reaction] Typing reaction added for msg {message_id[:16]}")
        return reaction_id
    except Exception as e:
        logger.warning(f"[Feishu Reaction] Add typing reaction failed: {e}")
        return None


async def remove_typing_reaction(
    app_id: str,
    app_secret: str,
    message_id: str,
    reaction_id: str | None = None,
) -> None:
    """Remove this app's 敲键盘 reaction from a user message.

    Feishu needs the reaction_id to delete a reaction and only lets an app
    delete its own. The webhook process creates the reaction but the Runtime
    delivery worker removes it, and the id does not travel between them, so
    fall back to resolving it by emoji type and keep only the reactions this
    app owns.
    """
    if not message_id or not app_id:
        return

    try:
        if reaction_id:
            reaction_ids = [reaction_id]
        else:
            reaction_ids = await _own_typing_reaction_ids(app_id, app_secret, message_id)
    except Exception as e:
        logger.warning(f"[Feishu Reaction] Typing reaction lookup failed: {e}")
        return

    for target_id in reaction_ids:
        try:
            await feishu_service.delete_message_reaction(
                app_id, app_secret, message_id, target_id
            )
            logger.info(
                f"[Feishu Reaction] Typing reaction removed for msg {message_id[:16]}"
            )
        except Exception as e:
            # Already gone, or removed by someone else — nothing to recover.
            logger.warning(f"[Feishu Reaction] Remove typing reaction failed: {e}")


async def _own_typing_reaction_ids(
    app_id: str,
    app_secret: str,
    message_id: str,
) -> list[str]:
    """Resolve the 敲键盘 reactions on a message that this app itself added."""
    items = await feishu_service.list_message_reactions(
        app_id, app_secret, message_id, TYPING_EMOJI_TYPE
    )
    own_ids: list[str] = []
    for item in items:
        operator = getattr(item, "operator", None)
        # For an app operator Feishu reports the bot's app_id as operator_id.
        if getattr(operator, "operator_type", None) != "app":
            continue
        if getattr(operator, "operator_id", None) != app_id:
            continue
        candidate = getattr(item, "reaction_id", None)
        if candidate:
            own_ids.append(candidate)
    return own_ids
