"""
Phase 7, Slice 2A: a shared fake `discord.Interaction` test double.

Not collected as a test module (no `test_` prefix, per `pytest.ini`'s
`python_files = test_*.py`). No real `discord.Interaction`/`discord.Client`
is ever constructed by this module, and no network connection of any kind
is made -- every command test in this slice drives a command's underlying
handler function directly with one of these fakes, exactly matching the
capability `docs/phase7_consumer_integration_readiness.md` §6 already
confirmed ("discord.py's command callables are plain async functions
taking an Interaction -- they can be tested by constructing a mock/stub
Interaction object and calling the function directly").
"""
from __future__ import annotations


class FakeRole:
    def __init__(self, name: str) -> None:
        self.name = name


class FakePermissions:
    def __init__(self, *, administrator: bool = False) -> None:
        self.administrator = administrator


class FakeUser:
    def __init__(self, user_id: int = 1, *, roles: list[FakeRole] | None = None, is_admin: bool = False) -> None:
        self.id = user_id
        self.roles = roles or []
        self.guild_permissions = FakePermissions(administrator=is_admin)


class FakeResponse:
    def __init__(self) -> None:
        self.deferred = False
        self.sent_messages: list[dict] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent_messages)

    async def defer(self, *, thinking: bool = True, ephemeral: bool = False) -> None:
        self.deferred = True

    async def send_message(self, content: str, *, ephemeral: bool = False, **kwargs) -> None:
        self.sent_messages.append({"content": content, "ephemeral": ephemeral})


class FakeFollowup:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send(self, content: str, *, ephemeral: bool = False, allowed_mentions=None, files=None, **kwargs) -> None:
        self.sent_messages.append(
            {"content": content, "ephemeral": ephemeral, "allowed_mentions": allowed_mentions, "files": files}
        )


class FakeInteraction:
    """Records every response/follow-up call for assertion; makes no real Discord API call."""

    def __init__(self, *, user: FakeUser | None = None) -> None:
        self.user = user or FakeUser()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
