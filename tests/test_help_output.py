import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.bot import build_general_help_text, build_plugin_help_text
from caine.plugin_manager import PluginManager


HELLO_PLUGIN = """
PLUGIN = {"name": "hello_plugin", "description": "Begruesst Nutzer."}


async def setup_plugin(api):
    @api.command("hello", description="Begruesst dich.")
    async def hello(ctx, args):
        await api.reply(ctx, "Hallo.")
"""


def run(coro):
    return asyncio.run(coro)


def make_bot(tmp_path):
    bot = commands.Bot(
        command_prefix="!",
        intents=discord.Intents.default(),
        help_command=None,
    )
    bot.settings = SimpleNamespace(command_prefix="!")
    bot.plugins = PluginManager(
        bot=bot,
        pending_dir=tmp_path / "plugins" / "pending",
        approved_dir=tmp_path / "plugins" / "approved",
        data_dir=tmp_path / "data",
    )

    @bot.command(name="help", aliases=["hilfe"])
    async def help_command(ctx, *, topic=""):
        pass

    @bot.command(name="ask", aliases=["frag"])
    async def ask(ctx, *, prompt):
        pass

    return bot


def load_hello_plugin(bot):
    bot.plugins.ensure_dirs()
    path = bot.plugins.approved_dir / "hello_plugin.py"
    path.write_text(HELLO_PLUGIN, encoding="utf-8")
    run(bot.plugins.load_plugin_file(path))


def test_general_help_groups_core_commands_and_summarizes_plugins(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)

    text = build_general_help_text(bot)

    assert "Normale Commands:" in text
    assert "Allgemein:" in text
    assert "`!help`" in text
    assert "`!ask`" in text
    assert "Plugins:" in text
    assert "`hello_plugin` (1 Commands)" in text
    assert "`!help hello_plugin`" in text
    assert "`!hello`" not in text


def test_plugin_help_shows_exact_plugin_commands(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)

    text = build_plugin_help_text(bot, "hello_plugin")

    assert text is not None
    assert "Plugin `hello_plugin`" in text
    assert "`!hello`: Begruesst dich." in text
