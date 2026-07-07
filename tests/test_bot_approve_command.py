import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.bot import approved_plugin_source_for_reference, install_commands, pending_activation_hint
from caine.plugin_manager import PluginManager


PLUGIN_SOURCE = """
PLUGIN = {"name": "demo_plugin", "description": "Demo."}


async def setup_plugin(api):
    @api.command({"names": ["demo"], "description": "Demo command.", "level": "user"})
    async def demo(ctx, args):
        await api.reply(ctx, "Demo.")
"""


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAuthor:
    id = 123
    guild_permissions = SimpleNamespace(administrator=True)
    roles = []


class FakeCtx:
    def __init__(self, bot):
        self.bot = bot
        self.author = FakeAuthor()
        self.guild = SimpleNamespace(id=1)
        self.replies = []

    def typing(self):
        return FakeTyping()

    async def reply(self, content, mention_author=False):
        self.replies.append(content)


def run(coro):
    return asyncio.run(coro)


def make_bot(tmp_path):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default(), help_command=None)
    bot.owner_id = 999999
    bot.settings = SimpleNamespace(
        command_prefix="!",
        command_permissions_path=None,
        allowed_guild_ids=set(),
        data_dir=tmp_path / "data",
    )
    bot.agent = None
    bot.plugins = PluginManager(
        bot=bot,
        pending_dir=tmp_path / "plugins" / "pending",
        approved_dir=tmp_path / "plugins" / "approved",
        data_dir=tmp_path / "data",
        trusted_plugins=True,
    )
    install_commands(bot)
    return bot


def test_approve_command_reports_already_active_when_pending_is_gone(tmp_path):
    bot = make_bot(tmp_path)
    bot.plugins.ensure_dirs()
    approved_path = bot.plugins.approved_dir / "demo_plugin.py"
    approved_path.write_text(PLUGIN_SOURCE, encoding="utf-8")
    run(bot.plugins.load_plugin_file(approved_path))

    ctx = FakeCtx(bot)
    command = bot.get_command("approve")

    run(command.callback(ctx, args="demo_plugin"))

    assert ctx.replies == ["Plugin `demo_plugin` ist bereits aktiv. Kein pending Review offen. Commands: `!demo`"]


def test_approve_command_reports_missing_pending_when_plugin_is_unknown(tmp_path):
    bot = make_bot(tmp_path)
    bot.plugins.ensure_dirs()
    ctx = FakeCtx(bot)
    command = bot.get_command("approve")

    run(command.callback(ctx, args="missing_plugin"))

    assert ctx.replies == ["`missing_plugin` existiert nicht in pending."]


def test_approve_command_reports_validation_failure_without_raising(tmp_path):
    bot = make_bot(tmp_path)
    bot.plugins.ensure_dirs()
    pending_path = bot.plugins.pending_dir / "broken_plugin.py"
    pending_path.write_text("def broken(:\n", encoding="utf-8")
    ctx = FakeCtx(bot)
    command = bot.get_command("approve")

    run(command.callback(ctx, args="broken_plugin"))

    assert len(ctx.replies) == 1
    assert "Plugin-Validation fehlgeschlagen" in ctx.replies[0]
    assert "Kein Approve ausgefuehrt" in ctx.replies[0]
    assert "!review broken_plugin" in ctx.replies[0]
    assert pending_path.exists()


def test_invalid_pending_activation_hint_does_not_suggest_approve():
    hint = pending_activation_hint("!", "broken_plugin", SimpleNamespace(ok=False))

    assert "!approve" not in hint
    assert hint == "Nicht aktivierbar, bis Validation OK ist. Pruefen mit `!review broken_plugin`."


def test_approved_plugin_source_is_reference_for_pending_edits(tmp_path):
    bot = make_bot(tmp_path)
    bot.plugins.ensure_dirs()
    approved_path = bot.plugins.approved_dir / "demo_plugin.py"
    pending_path = bot.plugins.pending_dir / "demo_plugin.py"
    approved_path.write_text(PLUGIN_SOURCE, encoding="utf-8")
    pending_path.write_text("def broken(:\n", encoding="utf-8")

    reference = approved_plugin_source_for_reference(bot, "demo_plugin", pending_path)

    assert reference == PLUGIN_SOURCE
    assert approved_plugin_source_for_reference(bot, "demo_plugin", approved_path) == ""
