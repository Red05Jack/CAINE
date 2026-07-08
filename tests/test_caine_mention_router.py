import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.bot import (
    build_command_routing_catalog,
    content_mentions_caine,
    install_commands,
    resolve_replied_caine_message_content,
    route_caine_mention_to_command,
    strip_caine_triggers,
)
from caine.openai_agent import CommandRoute


class FakeMember:
    roles = []

    def __init__(self, administrator=False):
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.display_name = "Jakob"
        self.name = "Jakob"
        self.bot = False


class FakeAgent:
    def __init__(self, route):
        self.route = route
        self.calls = []

    async def select_command_for_message(
        self,
        message_text,
        cleaned_request,
        author_name,
        command_catalog,
        prefix,
        replied_to_message_text="",
    ):
        self.calls.append(
            {
                "message_text": message_text,
                "cleaned_request": cleaned_request,
                "author_name": author_name,
                "command_catalog": command_catalog,
                "prefix": prefix,
                "replied_to_message_text": replied_to_message_text,
            }
        )
        return self.route


class FakeContext:
    def __init__(self, invoked):
        self.invoked = invoked

    async def invoke(self, command, *, args=""):
        self.invoked.append((command.name, args))


def run(coro):
    return asyncio.run(coro)


def make_bot():
    bot = commands.Bot(
        command_prefix="!",
        intents=discord.Intents.default(),
        help_command=None,
    )
    bot.owner_id = 999999
    bot.settings = SimpleNamespace(command_prefix="!", command_permissions_path=None)
    bot.plugins = SimpleNamespace(loaded={})
    install_commands(bot)
    return bot


def route_bot_context_to(invoked):
    async def get_context(message):
        return FakeContext(invoked)

    return get_context


def test_caine_trigger_spellings_are_detected_without_substring_noise():
    assert content_mentions_caine("hey caine was kann ich machen?")
    assert content_mentions_caine("Hallo C.A.I.N.E, hilf mal")
    assert content_mentions_caine("c a i n e help")
    assert not content_mentions_caine("cocaine ist kein Bot-Aufruf")


def test_caine_trigger_is_stripped_for_routing_prompt():
    assert strip_caine_triggers("hey caine was kann ich machen?") == "hey was kann ich machen"


def test_replied_caine_message_content_is_used_as_router_trigger():
    bot = SimpleNamespace(user=SimpleNamespace(id=42))
    replied_message = SimpleNamespace(author=SimpleNamespace(id=42), content="Du kannst `!help` nutzen.")
    message = SimpleNamespace(reference=SimpleNamespace(resolved=replied_message))

    assert run(resolve_replied_caine_message_content(bot, message)) == "Du kannst `!help` nutzen."


def test_reply_to_other_user_is_not_a_caine_router_trigger():
    bot = SimpleNamespace(user=SimpleNamespace(id=42))
    replied_message = SimpleNamespace(author=SimpleNamespace(id=77), content="Du kannst `!help` nutzen.")
    message = SimpleNamespace(reference=SimpleNamespace(resolved=replied_message))

    assert run(resolve_replied_caine_message_content(bot, message)) == ""


def test_replied_caine_message_can_be_fetched_when_not_resolved():
    class FakeChannel:
        async def fetch_message(self, message_id):
            assert message_id == 123
            return SimpleNamespace(author=SimpleNamespace(id=42), content="Alte CAINE-Antwort")

    bot = SimpleNamespace(user=SimpleNamespace(id=42))
    message = SimpleNamespace(
        channel=FakeChannel(),
        reference=SimpleNamespace(resolved=None, message_id=123),
    )

    assert run(resolve_replied_caine_message_content(bot, message)) == "Alte CAINE-Antwort"


def test_caine_mention_is_always_sent_to_api_router():
    bot = make_bot()
    bot.agent = FakeAgent(CommandRoute(command_name="ask", args="was geht", confidence=0.9))
    invoked = []
    bot.get_context = route_bot_context_to(invoked)
    message = SimpleNamespace(
        content="caine was geht?",
        author=FakeMember(),
        channel=SimpleNamespace(),
        reference=None,
    )

    assert run(route_caine_mention_to_command(bot, message)) is True

    assert invoked == [("ask", "was geht")]
    assert len(bot.agent.calls) == 1
    assert bot.agent.calls[0]["message_text"] == "caine was geht?"
    assert bot.agent.calls[0]["cleaned_request"] == "was geht"


def test_reply_to_caine_message_is_sent_to_api_router_with_context():
    bot = make_bot()
    bot._connection.user = SimpleNamespace(id=42)
    bot.agent = FakeAgent(CommandRoute(command_name="help", args="geburtstags_manege", confidence=0.9))
    invoked = []
    bot.get_context = route_bot_context_to(invoked)
    replied_message = SimpleNamespace(
        author=SimpleNamespace(id=42),
        content="Plugin-Vorschlag `geburtstags_manege` gespeichert. Aktivieren mit `!approve geburtstags_manege`.",
    )
    message = SimpleNamespace(
        content="passt so",
        author=FakeMember(),
        channel=SimpleNamespace(),
        reference=SimpleNamespace(resolved=replied_message),
    )

    assert run(route_caine_mention_to_command(bot, message)) is True

    assert invoked == [("help", "geburtstags_manege")]
    assert len(bot.agent.calls) == 1
    assert bot.agent.calls[0]["cleaned_request"] == "passt so"
    assert bot.agent.calls[0]["replied_to_message_text"] == replied_message.content


def test_routing_catalog_only_contains_commands_visible_to_user():
    bot = make_bot()

    user_catalog = run(build_command_routing_catalog(bot, FakeMember()))
    admin_catalog = run(build_command_routing_catalog(bot, FakeMember(administrator=True)))

    user_names = {item["name"] for item in user_catalog}
    admin_names = {item["name"] for item in admin_catalog}
    assert "help" in user_names
    assert "evolve" not in user_names
    assert "evolve" in admin_names


def test_routing_catalog_includes_options_and_routing_hints():
    bot = make_bot()

    catalog = run(build_command_routing_catalog(bot, FakeMember()))
    by_name = {item["name"]: item for item in catalog}

    ask = by_name["ask"]
    help_command = by_name["help"]
    assert ask["options"] == [
        {
            "name": "prompt",
            "description": "Question or prompt.",
            "type": "string",
            "required": True,
        }
    ]
    assert ask["routing"]["priority"] == 80
    assert "broad questions" in ask["routing"]["use_when"]
    assert help_command["routing"]["priority"] == 35
    assert "vague help" in help_command["routing"]["avoid_when"]
