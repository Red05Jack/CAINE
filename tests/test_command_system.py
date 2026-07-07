from pathlib import Path
from types import SimpleNamespace

import discord
from discord.ext import commands

from caine.command_system import (
    CommandLevel,
    command_spec,
    has_command_access,
    load_command_permission_config,
    register_dual_command,
    should_register_slash_command,
    sync_application_commands,
    slash_sync_guild_ids,
)


class FakeBot:
    settings = SimpleNamespace(command_permissions_path=None)

    async def is_owner(self, user):
        return False


class FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class FakeUser:
    roles = []


class FakeMember:
    def __init__(self, roles=(), administrator=False):
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=administrator)


def run(coro):
    import asyncio

    return asyncio.run(coro)


def test_command_object_stores_names_description_and_level():
    spec = command_spec(
        {
            "names": ["help", "hilfe"],
            "description": "Shows help.",
            "level": "admin",
        }
    )

    assert spec.names == ("help", "hilfe")
    assert spec.description == "Shows help."
    assert spec.level is CommandLevel.ADMIN


def test_command_object_normalizes_names_and_options():
    spec = command_spec(
        {
            "names": ["set-rank-Color", "set-rank-color", "set-rank-Colour"],
            "description": "Sets a color.",
            "options": [
                {"name": "Target User", "description": "Target.", "type": "member", "required": True},
                {"name": "Color", "description": "Hex color.", "type": "text", "required": False},
            ],
        }
    )

    assert spec.names == ("set-rank-color", "set-rank-colour")
    assert [option.name for option in spec.options] == ["target_user", "color"]
    assert [option.type for option in spec.options] == ["user", "string"]


def test_command_object_parses_slash_enabled_flag():
    spec = command_spec(
        {
            "names": ["quiet"],
            "description": "Prefix only.",
            "level": "user",
            "slash": False,
        }
    )

    assert spec.slash_enabled is False
    assert not should_register_slash_command(spec)


def test_command_object_registers_prefix_alias_and_slash_alias():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default(), help_command=None)
    spec = command_spec({"names": ["help", "hilfe"], "description": "Shows help.", "level": "user"})

    async def handler(ctx, args):
        pass

    register_dual_command(bot, spec, handler)

    assert bot.get_command("help") is not None
    assert bot.get_command("hilfe") is not None
    assert bot.tree.get_command("help") is not None
    assert bot.tree.get_command("hilfe") is not None


def test_admin_and_kinger_commands_are_prefix_only_by_default():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default(), help_command=None)
    admin_spec = command_spec({"names": ["approve"], "description": "Approve.", "level": "admin"})
    kinger_spec = command_spec({"names": ["health"], "description": "Health.", "level": "kinger"})

    async def handler(ctx, args):
        pass

    register_dual_command(bot, admin_spec, handler)
    register_dual_command(bot, kinger_spec, handler)

    assert bot.get_command("approve") is not None
    assert bot.get_command("health") is not None
    assert bot.tree.get_command("approve") is None
    assert bot.tree.get_command("health") is None
    assert not should_register_slash_command(admin_spec)
    assert not should_register_slash_command(kinger_spec)


def test_user_command_can_disable_slash_generation():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default(), help_command=None)
    spec = command_spec({"names": ["quiet"], "description": "Quiet.", "level": "user", "slash": False})

    async def handler(ctx, args):
        pass

    register_dual_command(bot, spec, handler)

    assert bot.get_command("quiet") is not None
    assert bot.tree.get_command("quiet") is None


def test_slash_command_uses_declared_option_types_and_no_default_text_option():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default(), help_command=None)
    spec = command_spec(
        {
            "names": ["rank"],
            "description": "Shows rank.",
            "options": [
                {"name": "user", "description": "Target user.", "type": "user", "required": False},
                {"name": "debug", "description": "Show debug.", "type": "boolean", "required": False},
            ],
        }
    )
    plain_spec = command_spec({"names": ["plugins"], "description": "Lists plugins."})

    async def handler(ctx, args):
        pass

    register_dual_command(bot, spec, handler)
    register_dual_command(bot, plain_spec, handler)

    rank_params = {param.name: param for param in bot.tree.get_command("rank").parameters}
    assert rank_params["user"].type.name == "user"
    assert rank_params["debug"].type.name == "boolean"
    assert bot.tree.get_command("plugins").parameters == []


def test_kinger_role_ids_are_loaded_from_file(tmp_path):
    path = tmp_path / "command_permissions.json"
    path.write_text('{"kingerRoleIds": ["1523734381146148864"]}', encoding="utf-8")

    config = load_command_permission_config(Path(path))

    assert config.kinger_role_ids == {1523734381146148864}


def test_slash_sync_prefers_allowed_guild_ids():
    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids={30, 10}),
        guilds=[SimpleNamespace(id=20)],
    )

    assert slash_sync_guild_ids(bot) == [10, 30]


def test_slash_sync_uses_joined_guilds_without_allowlist():
    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids=set()),
        guilds=[SimpleNamespace(id=20), SimpleNamespace(id=10)],
    )

    assert slash_sync_guild_ids(bot) == [10, 20]


def test_slash_sync_with_guilds_clears_global_commands():
    class FakeTree:
        def __init__(self):
            self.commands = [SimpleNamespace(name="help")]
            self.copied_to = []
            self.cleared = []
            self.synced = []

        def get_commands(self, guild=None):
            return list(self.commands)

        def copy_global_to(self, guild):
            self.copied_to.append(guild.id)

        def clear_commands(self, guild=None):
            self.cleared.append(getattr(guild, "id", None))
            if guild is None:
                self.commands = []

        def add_command(self, command):
            self.commands.append(command)

        async def sync(self, guild=None):
            self.synced.append(getattr(guild, "id", None))
            return [SimpleNamespace(name="help")]

    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids={123}),
        guilds=[],
        tree=FakeTree(),
    )

    run(sync_application_commands(bot))

    assert bot.tree.copied_to == [123]
    assert bot.tree.cleared == [None]
    assert bot.tree.synced == [123, None]
    assert [command.name for command in bot.tree.commands] == ["help"]


def test_slash_sync_skips_unchanged_global_commands(tmp_path):
    class FakeTree:
        def __init__(self):
            self.commands = [SimpleNamespace(name="help", description="Help", parameters=[], extras={})]
            self.synced = []

        def get_commands(self, guild=None):
            return list(self.commands)

        async def sync(self, guild=None):
            self.synced.append(getattr(guild, "id", None))
            return list(self.commands)

    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids=set(), data_dir=tmp_path),
        guilds=[],
        tree=FakeTree(),
    )

    run(sync_application_commands(bot))
    run(sync_application_commands(bot))

    assert bot.tree.synced == [None]
    assert (tmp_path / "slash_sync_state.json").exists()


def test_slash_sync_runs_again_when_command_signature_changes(tmp_path):
    class FakeTree:
        def __init__(self):
            self.commands = [SimpleNamespace(name="help", description="Help", parameters=[], extras={})]
            self.synced = []

        def get_commands(self, guild=None):
            return list(self.commands)

        async def sync(self, guild=None):
            self.synced.append(getattr(guild, "id", None))
            return list(self.commands)

    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids=set(), data_dir=tmp_path),
        guilds=[],
        tree=FakeTree(),
    )

    run(sync_application_commands(bot))
    bot.tree.commands.append(SimpleNamespace(name="plugins", description="Plugins", parameters=[], extras={}))
    run(sync_application_commands(bot))

    assert bot.tree.synced == [None, None]


def test_slash_sync_cache_skips_repeated_guild_sync_and_global_clear(tmp_path):
    class FakeTree:
        def __init__(self):
            self.commands = [SimpleNamespace(name="help", description="Help", parameters=[], extras={})]
            self.copied_to = []
            self.cleared = []
            self.synced = []

        def get_commands(self, guild=None):
            return list(self.commands)

        def copy_global_to(self, guild):
            self.copied_to.append(guild.id)

        def clear_commands(self, guild=None):
            self.cleared.append(getattr(guild, "id", None))
            if guild is None:
                self.commands = []

        def add_command(self, command):
            self.commands.append(command)

        async def sync(self, guild=None):
            self.synced.append(getattr(guild, "id", None))
            return [SimpleNamespace(name="help")]

    bot = SimpleNamespace(
        settings=SimpleNamespace(allowed_guild_ids={123}, data_dir=tmp_path),
        guilds=[],
        tree=FakeTree(),
    )

    run(sync_application_commands(bot))
    run(sync_application_commands(bot))

    assert bot.tree.copied_to == [123]
    assert bot.tree.cleared == [None]
    assert bot.tree.synced == [123, None]


def test_command_access_levels():
    bot = FakeBot()
    kinger = FakeMember(roles=[FakeRole(1523734381146148864)])
    admin = FakeMember(administrator=True)
    user = FakeUser()

    assert run(has_command_access(bot, user, CommandLevel.USER))
    assert run(has_command_access(bot, admin, CommandLevel.ADMIN))
    assert not run(has_command_access(bot, user, CommandLevel.ADMIN))
    assert run(has_command_access(bot, kinger, CommandLevel.KINGER))
    assert not run(has_command_access(bot, admin, CommandLevel.KINGER))
