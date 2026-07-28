"""Feishu "敲键盘" reaction lifecycle tests."""

from types import SimpleNamespace

import pytest

from app.services import feishu_reaction
from app.services.feishu_service import feishu_service


def _reaction(reaction_id: str, *, operator_type: str = "app", operator_id: str = "app-1"):
    return SimpleNamespace(
        reaction_id=reaction_id,
        operator=SimpleNamespace(operator_type=operator_type, operator_id=operator_id),
    )


@pytest.mark.asyncio
async def test_add_typing_reaction_sends_the_keyboard_emoji(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def add_message_reaction(app_id, app_secret, message_id, emoji_type):
        calls["args"] = (app_id, app_secret, message_id, emoji_type)
        return "reaction-1"

    monkeypatch.setattr(feishu_service, "add_message_reaction", add_message_reaction)

    reaction_id = await feishu_reaction.add_typing_reaction("app-1", "secret-1", "om-1")

    assert reaction_id == "reaction-1"
    assert calls["args"] == ("app-1", "secret-1", "om-1", "Typing")


@pytest.mark.asyncio
async def test_add_typing_reaction_never_raises(monkeypatch) -> None:
    async def add_message_reaction(*_args, **_kwargs):
        raise RuntimeError("Feishu rejected the reaction")

    monkeypatch.setattr(feishu_service, "add_message_reaction", add_message_reaction)

    assert await feishu_reaction.add_typing_reaction("app-1", "secret-1", "om-1") is None


@pytest.mark.asyncio
async def test_add_typing_reaction_skips_when_message_id_missing(monkeypatch) -> None:
    async def add_message_reaction(*_args, **_kwargs):
        raise AssertionError("must not call Feishu without a message id")

    monkeypatch.setattr(feishu_service, "add_message_reaction", add_message_reaction)

    assert await feishu_reaction.add_typing_reaction("app-1", "secret-1", "") is None


@pytest.mark.asyncio
async def test_remove_typing_reaction_deletes_the_known_reaction_id(monkeypatch) -> None:
    deleted: list[tuple] = []

    async def list_message_reactions(*_args, **_kwargs):
        raise AssertionError("a known reaction_id must not trigger a lookup")

    async def delete_message_reaction(app_id, app_secret, message_id, reaction_id):
        deleted.append((app_id, app_secret, message_id, reaction_id))

    monkeypatch.setattr(feishu_service, "list_message_reactions", list_message_reactions)
    monkeypatch.setattr(feishu_service, "delete_message_reaction", delete_message_reaction)

    await feishu_reaction.remove_typing_reaction(
        "app-1", "secret-1", "om-1", reaction_id="reaction-1"
    )

    assert deleted == [("app-1", "secret-1", "om-1", "reaction-1")]


@pytest.mark.asyncio
async def test_remove_typing_reaction_looks_up_its_own_reaction(monkeypatch) -> None:
    """The webhook process adds the reaction; the delivery worker removes it.

    The delivery worker never learns the reaction_id, so it must resolve the id
    by emoji type and delete only the reaction this app owns.
    """
    listed: dict[str, object] = {}
    deleted: list[str] = []

    async def list_message_reactions(app_id, app_secret, message_id, emoji_type):
        listed["args"] = (app_id, app_secret, message_id, emoji_type)
        return [
            _reaction("user-reaction", operator_type="user", operator_id="ou-9"),
            _reaction("other-bot-reaction", operator_type="app", operator_id="app-2"),
            _reaction("our-reaction", operator_type="app", operator_id="app-1"),
        ]

    async def delete_message_reaction(_app_id, _app_secret, _message_id, reaction_id):
        deleted.append(reaction_id)

    monkeypatch.setattr(feishu_service, "list_message_reactions", list_message_reactions)
    monkeypatch.setattr(feishu_service, "delete_message_reaction", delete_message_reaction)

    await feishu_reaction.remove_typing_reaction("app-1", "secret-1", "om-1")

    assert listed["args"] == ("app-1", "secret-1", "om-1", "Typing")
    assert deleted == ["our-reaction"]


@pytest.mark.asyncio
async def test_remove_typing_reaction_never_raises(monkeypatch) -> None:
    async def list_message_reactions(*_args, **_kwargs):
        raise RuntimeError("Feishu list failed")

    monkeypatch.setattr(feishu_service, "list_message_reactions", list_message_reactions)

    await feishu_reaction.remove_typing_reaction("app-1", "secret-1", "om-1")


@pytest.mark.asyncio
async def test_remove_typing_reaction_survives_a_failed_delete(monkeypatch) -> None:
    async def list_message_reactions(*_args, **_kwargs):
        return [_reaction("our-reaction")]

    async def delete_message_reaction(*_args, **_kwargs):
        raise RuntimeError("reaction already gone")

    monkeypatch.setattr(feishu_service, "list_message_reactions", list_message_reactions)
    monkeypatch.setattr(feishu_service, "delete_message_reaction", delete_message_reaction)

    await feishu_reaction.remove_typing_reaction("app-1", "secret-1", "om-1")
