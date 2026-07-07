from caine.plugin_validation import normalize_plugin_command_source, validate_plugin_source


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


def test_command_names_and_options_are_normalized():
    source = """
PLUGIN = {"name": "rank", "description": "Rank tools."}


async def setup_plugin(api):
    @api.command({
        "names": ["set-rank-Color", "set-rank-color", "set-rank-Colour"],
        "description": "Set color.",
        "level": "user",
        "options": [
            {"name": "Target User", "description": "Target.", "type": "member", "required": False}
        ]
    })
    async def color(ctx, args):
        await api.reply(ctx, "ok")
"""

    result = validate_plugin_source(source)
    normalized = normalize_plugin_command_source(source)

    assert result.ok
    assert result.command_names == ["set-rank-color", "set-rank-colour"]
    assert '"names": ["set-rank-color", "set-rank-colour"]' in normalized
    assert '"type": "user"' in normalized


def test_command_slash_flag_is_allowed_in_command_object():
    result = validate_plugin_source(
        """
PLUGIN = {"name": "quiet", "description": "Quiet tools."}


async def setup_plugin(api):
    @api.command({"names": ["quiet"], "description": "Prefix-only user command.", "level": "user", "slash": False})
    async def quiet(ctx, args):
        await api.reply(ctx, "quiet")
"""
    )

    assert result.ok
    assert result.command_names == ["quiet"]


def test_plugin_level_shared_api_and_emit_are_allowed():
    result = validate_plugin_source(
        """
PLUGIN = {"name": "demo_api", "description": "Demo API."}


async def setup_plugin(api):
    state = {"count": 0}

    def current_count():
        return state["count"]

    api.shared["demo_api.api"] = {"current_count": current_count}

    @api.command({"names": ["demo-api"], "description": "Use demo.", "level": "user"})
    async def demo_api(ctx, args):
        state["count"] = state["count"] + 1
        await api.emit("demo_api.used", {"count": state["count"]})
        await api.reply(ctx, "ok")
"""
    )

    assert result.ok
    assert result.command_names == ["demo-api"]


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
