import asyncio

import discord
from discord.ext import commands

from caine.plugin_manager import PluginManager


OLD_PLUGIN = """
PLUGIN = {"name": "demo", "description": "old"}


async def setup_plugin(api):
    @api.command("demo", description="old")
    async def demo(ctx, args):
        await api.reply(ctx, "old")
"""


NEW_PLUGIN = """
PLUGIN = {"name": "demo", "description": "new"}


async def setup_plugin(api):
    @api.command("demo", description="new")
    async def demo(ctx, args):
        await api.reply(ctx, "new")
"""


HELP_PLUGIN = """
PLUGIN = {"name": "circus_help", "description": "help replacement"}


async def setup_plugin(api):
    @api.command("help", description="plugin help")
    async def help_command(ctx, args):
        await api.reply(ctx, "plugin help")
"""


def run(coro):
    return asyncio.run(coro)


def make_bot():
    return commands.Bot(
        command_prefix="!",
        intents=discord.Intents.default(),
        help_command=commands.DefaultHelpCommand(no_category="CAINE"),
    )


def make_manager(tmp_path, bot):
    return PluginManager(
        bot=bot,
        pending_dir=tmp_path / "plugins" / "pending",
        approved_dir=tmp_path / "plugins" / "approved",
        data_dir=tmp_path / "data",
    )


def test_approve_existing_plugin_archives_old_version_and_moves_pending(tmp_path):
    bot = make_bot()
    manager = make_manager(tmp_path, bot)
    manager.ensure_dirs()

    approved_path = manager.approved_dir / "demo.py"
    approved_path.write_text(OLD_PLUGIN, encoding="utf-8")
    run(manager.load_plugin_file(approved_path, forced_plugin_name="demo"))
    pending_id, pending_path, validation = manager.save_pending_revision("demo", NEW_PLUGIN, approved_path)

    loaded = run(manager.approve(pending_id))

    archive_path = manager.archive_dir / "demoV1.py"
    assert validation.ok
    assert loaded.name == "demo"
    assert loaded.path == approved_path
    assert not pending_path.exists()
    assert approved_path.read_text(encoding="utf-8") == NEW_PLUGIN
    assert archive_path.read_text(encoding="utf-8") == OLD_PLUGIN


def test_plugin_can_replace_builtin_help_command(tmp_path):
    bot = make_bot()
    manager = make_manager(tmp_path, bot)
    manager.ensure_dirs()
    assert "help" in bot.all_commands

    manager.save_pending("circus_help", HELP_PLUGIN)
    loaded = run(manager.approve("circus_help"))

    assert loaded.commands == ["help"]
    assert bot.all_commands["help"].module != "discord.ext.commands.help"

    manager.unload_plugin("circus_help")

    assert bot.all_commands["help"].module == "discord.ext.commands.help"
