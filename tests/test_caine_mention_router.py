import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.bot import (
    build_command_routing_catalog,
    content_mentions_caine,
    extract_approve_plugin_id,
    extract_suggested_command_route,
    install_commands,
    local_caine_command_route,
    local_caine_reply_route,
    resolve_replied_caine_message_content,
    strip_caine_triggers,
)


class FakeMember:
    roles = []

    def __init__(self, administrator=False):
        self.guild_permissions = SimpleNamespace(administrator=administrator)


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


def test_local_route_maps_help_question_to_help_command():
    route = local_caine_command_route(
        "hey was kann ich machen",
        [{"name": "help", "aliases": [], "description": "Help"}],
    )

    assert route is not None
    assert route.command_name == "help"
    assert route.args == ""


def test_local_route_preserves_direct_command_arguments():
    route = local_caine_command_route(
        "hey rank @Red__Jack",
        [{"name": "rank", "aliases": ["myrank"], "description": "Rank"}],
    )

    assert route is not None
    assert route.command_name == "rank"
    assert route.args == "@Red__Jack"


def test_reply_confirmation_routes_to_approve_from_caine_review_message():
    route = local_caine_reply_route(
        "passt so",
        "Plugin-Vorschlag `geburtstags_manege` gespeichert.\nAktivieren mit `!approve geburtstags_manege`.",
        [{"name": "approve", "aliases": [], "description": "Approve"}],
        "!",
    )

    assert route is not None
    assert route.command_name == "approve"
    assert route.args == "geburtstags_manege"


def test_reply_confirmation_routes_any_visible_suggested_command():
    route = local_caine_reply_route(
        "jo machen wir so",
        "Wenn das weg soll, nutze `!reject geburtstags_manege`.",
        [{"name": "reject", "aliases": [], "description": "Reject"}],
        "!",
    )

    assert route is not None
    assert route.command_name == "reject"
    assert route.args == "geburtstags_manege"


def test_reply_confirmation_ignores_commands_not_visible_in_catalog():
    route = local_caine_reply_route(
        "jo machen wir so",
        "Wenn das weg soll, nutze `!reject geburtstags_manege`.",
        [{"name": "help", "aliases": [], "description": "Help"}],
        "!",
    )

    assert route is None


def test_approve_is_preferred_when_review_message_contains_multiple_commands():
    route = extract_suggested_command_route(
        "Command: `!birthday_set`\nAktivieren mit `!approve geburtstags_manege`.",
        {"birthday_set": "birthday_set", "approve": "approve"},
        "!",
    )

    assert route is not None
    assert route.command_name == "approve"
    assert route.args == "geburtstags_manege"


def test_reply_confirmation_does_not_approve_without_explicit_review_command():
    route = local_caine_reply_route(
        "passt so",
        "Ich finde das Plugin ziemlich gut.",
        [{"name": "approve", "aliases": [], "description": "Approve"}],
        "!",
    )

    assert route is None


def test_approve_plugin_id_is_extracted_from_review_message():
    assert extract_approve_plugin_id("Aktivieren mit `!approve geburtstags_manege`.") == "geburtstags_manege"
    assert extract_approve_plugin_id("Aktivieren mit `/approve geburtstags_manege`.") == "geburtstags_manege"


def test_routing_catalog_only_contains_commands_visible_to_user():
    bot = make_bot()

    user_catalog = run(build_command_routing_catalog(bot, FakeMember()))
    admin_catalog = run(build_command_routing_catalog(bot, FakeMember(administrator=True)))

    user_names = {item["name"] for item in user_catalog}
    admin_names = {item["name"] for item in admin_catalog}
    assert "help" in user_names
    assert "evolve" not in user_names
    assert "evolve" in admin_names
