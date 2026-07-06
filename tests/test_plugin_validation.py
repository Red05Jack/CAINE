from caine.plugin_validation import validate_plugin_source


VALID_PLUGIN = """
PLUGIN = {"name": "dice", "description": "Rolls a die."}


async def setup_plugin(api):
    @api.command("dice", description="Roll a die.")
    async def dice(ctx, args):
        value = api.choice(["1", "2", "3", "4", "5", "6"])
        await api.reply(ctx, f"You rolled {value}.")
"""


def test_valid_plugin_passes():
    result = validate_plugin_source(VALID_PLUGIN)

    assert result.ok
    assert result.command_names == ["dice"]
    assert result.metadata["name"] == "dice"


def test_command_object_plugin_passes():
    result = validate_plugin_source(
        """
PLUGIN = {"name": "dice", "description": "Rolls a die."}


async def setup_plugin(api):
    @api.command({"names": ["dice"], "description": "Roll a die.", "level": "user"})
    async def dice(ctx, args):
        await api.reply(ctx, "rolled")
"""
    )

    assert result.ok
    assert result.command_names == ["dice"]


def test_banned_import_fails():
    result = validate_plugin_source(
        """
import os
PLUGIN = {"name": "bad", "description": "bad"}
async def setup_plugin(api):
    @api.command("bad", description="bad")
    async def bad(ctx, args):
        await api.reply(ctx, "bad")
"""
    )

    assert not result.ok
    assert "import 'os' is not allowed" in result.errors


def test_open_call_fails():
    result = validate_plugin_source(
        """
PLUGIN = {"name": "bad", "description": "bad"}
async def setup_plugin(api):
    @api.command("bad", description="bad")
    async def bad(ctx, args):
        open("secret.txt")
        await api.reply(ctx, "bad")
"""
    )

    assert not result.ok
    assert "name 'open' is not allowed" in result.errors


def test_missing_setup_fails():
    result = validate_plugin_source('PLUGIN = {"name": "bad", "description": "bad"}')

    assert not result.ok
    assert "missing async setup_plugin(api)" in result.errors
