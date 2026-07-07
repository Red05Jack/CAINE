import asyncio
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.bot import build_general_help_text, build_plugin_help_text, install_commands
from caine.plugin_manager import PluginManager


HELLO_PLUGIN = """
PLUGIN = {"name": "hello_plugin", "description": "Begruesst Nutzer."}


async def setup_plugin(api):
    @api.command("hello", description="Begruesst dich.")
    async def hello(ctx, args):
        await api.reply(ctx, "Hallo.")

    @api.command({"names": ["adminhello"], "description": "Admin hello.", "level": "admin"})
    async def adminhello(ctx, args):
        await api.reply(ctx, "Admin hallo.")

    @api.command({"names": ["kinghello"], "description": "Kinger hello.", "level": "kinger"})
    async def kinghello(ctx, args):
        await api.reply(ctx, "Kinger hallo.")
"""


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeMember:
    def __init__(self, member_id=1, roles=(), administrator=False):
        self.id = member_id
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=administrator)


def run(coro):
    return asyncio.run(coro)


def make_bot(tmp_path):
    bot = commands.Bot(
        command_prefix="!",
        intents=discord.Intents.default(),
        help_command=None,
    )
    bot.owner_id = 999999
    bot.settings = SimpleNamespace(command_prefix="!", command_permissions_path=None)
    bot.plugins = PluginManager(
        bot=bot,
        pending_dir=tmp_path / "plugins" / "pending",
        approved_dir=tmp_path / "plugins" / "approved",
        data_dir=tmp_path / "data",
    )

    bot.agent = None
    install_commands(bot)

    return bot


def load_hello_plugin(bot):
    bot.plugins.ensure_dirs()
    path = bot.plugins.approved_dir / "hello_plugin.py"
    path.write_text(HELLO_PLUGIN, encoding="utf-8")
    run(bot.plugins.load_plugin_file(path))


def test_general_help_groups_core_commands_and_summarizes_plugins(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)
    user = FakeMember()

    text = run(build_general_help_text(bot, user))

    assert "Commands:" in text
    assert "General:" not in text
    assert "`!help`: Shows core commands and plugin details." in text
    assert "`!ask`: Asks C.A.I.N.E. via OpenAI." in text
    assert "[S3]" not in text
    assert "`!evolve` [S2]" not in text
    assert "`!health` [S1]" not in text
    assert "Plugins:" in text
    assert "`hello_plugin` (1 Commands)" in text
    assert "`!help hello_plugin`" in text
    assert "`!hello`" not in text


def test_plugin_help_shows_exact_plugin_commands(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)
    user = FakeMember()

    text = run(build_plugin_help_text(bot, "hello_plugin", user))

    assert text is not None
    assert "Plugin `hello_plugin`" in text
    assert "Commands:" in text
    assert "`!hello`: Begruesst dich." in text
    assert "`!adminhello` [S2]" not in text
    assert "`!kinghello` [S1]" not in text


def test_admin_help_includes_admin_commands_but_not_kinger(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)
    admin = FakeMember(administrator=True)

    text = run(build_general_help_text(bot, admin))
    plugin_text = run(build_plugin_help_text(bot, "hello_plugin", admin))

    assert "Commands:" in text
    assert "Admin commands:" in text
    assert "`!evolve`: Creates a pending plugin from a feature request." in text
    assert "[S2]" not in text
    assert "`!health` [S1]" not in text
    assert "Commands:" in plugin_text
    assert "Admin commands:" in plugin_text
    assert "`!adminhello`: Admin hello." in plugin_text
    assert "`!kinghello` [S1]" not in plugin_text


def test_kinger_help_includes_kinger_commands_but_not_admin(tmp_path):
    bot = make_bot(tmp_path)
    load_hello_plugin(bot)
    kinger = FakeMember(roles=[FakeRole(1523734381146148864)])

    text = run(build_general_help_text(bot, kinger))
    plugin_text = run(build_plugin_help_text(bot, "hello_plugin", kinger))

    assert "Commands:" in text
    assert "Kinger:" in text
    assert "`!help`: Shows core commands and plugin details." in text
    assert "`!health`: Checks CAINE runtime state." in text
    assert "`!chatgpt-logs`" in text
    assert "Shows recent ChatGPT activity audit logs." in text
    assert "`!evolve` [S2]" not in text
    assert "Commands:" in plugin_text
    assert "Kinger:" in plugin_text
    assert "`!hello`: Begruesst dich." in plugin_text
    assert "`!kinghello`: Kinger hello." in plugin_text
    assert "`!adminhello` [S2]" not in plugin_text
