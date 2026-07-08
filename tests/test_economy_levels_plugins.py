import asyncio
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
APPROVED = ROOT / "plugins" / "approved"


def command_specs(path: Path) -> list[dict[str, object]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    specs: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == "command"):
                continue
            if not decorator.args or not isinstance(decorator.args[0], ast.Dict):
                continue
            names: list[str] = []
            level = "user"
            for key, value in zip(decorator.args[0].keys, decorator.args[0].values):
                if isinstance(key, ast.Constant) and key.value == "names":
                    if isinstance(value, ast.List):
                        names.extend(item.value for item in value.elts if isinstance(item, ast.Constant))
                if isinstance(key, ast.Constant) and key.value == "level":
                    if isinstance(value, ast.Constant):
                        level = str(value.value)
            if names:
                specs.append({"primary": names[0], "names": names, "level": level})
    return specs


def load_levels_module():
    path = APPROVED / "levels.py"
    spec = importlib.util.spec_from_file_location("test_levels_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_approved_plugin_files_are_renamed_to_economy_and_levels():
    assert (APPROVED / "economy.py").exists()
    assert (APPROVED / "levels.py").exists()
    assert not (APPROVED / "glitzerchip_manege.py").exists()
    assert not (APPROVED / "level_manege.py").exists()


def test_economy_exposes_english_primary_user_commands():
    user_primaries = [
        spec["primary"]
        for spec in command_specs(APPROVED / "economy.py")
        if spec["level"] == "user"
    ]

    assert user_primaries == [
        "coins",
        "shop",
        "buy",
        "inventory",
        "send",
        "richest",
    ]
    richest = next(spec for spec in command_specs(APPROVED / "economy.py") if spec["primary"] == "richest")
    assert richest["names"] == ["richest", "top"]


def test_economy_restores_admin_commands_and_python_api():
    specs = command_specs(APPROVED / "economy.py")
    admin_primaries = [spec["primary"] for spec in specs if spec["level"] == "admin"]
    kinger_primaries = [spec["primary"] for spec in specs if spec["level"] == "kinger"]
    source = (APPROVED / "economy.py").read_text(encoding="utf-8")

    assert admin_primaries == [
        "economy-admin",
        "coins-inspect",
        "coins-add",
        "coins-remove",
        "coins-set",
        "coins-reset",
        "shop-add",
        "shop-remove",
        "economy-config",
    ]
    assert kinger_primaries == ["economy-audit"]
    for helper in (
        '"set_balance": api_set_balance',
        '"transfer": api_transfer',
        '"get_inventory": api_get_inventory',
        '"remove_shop_item": api_remove_shop_item',
    ):
        assert helper in source


def test_approved_plugins_do_not_duplicate_command_or_alias_names():
    owners: dict[str, str] = {}
    duplicates: list[str] = []

    for plugin_path in sorted(APPROVED.glob("*.py")):
        for spec in command_specs(plugin_path):
            for name in spec["names"]:
                previous = owners.setdefault(str(name), plugin_path.name)
                if previous != plugin_path.name:
                    duplicates.append(f"{name}: {previous}, {plugin_path.name}")

    assert duplicates == []


def test_levels_uses_economy_integration_names():
    source = (APPROVED / "levels.py").read_text(encoding="utf-8")

    assert "economy.api" in source
    assert "economy.credit" in source
    assert "level-glitzerchips" not in source
    assert "glitzerchip_manege.credit" not in source
    assert "level_manege.api" not in source


def test_level_economy_reward_curve_is_increasing():
    levels = load_levels_module()

    rewards = [levels._economy_reward_for_level({}, level) for level in range(1, 31)]
    increments = [right - left for left, right in zip(rewards, rewards[1:])]

    assert rewards == sorted(rewards)
    assert all(amount > 0 for amount in rewards)
    assert increments[-1] > increments[0]
    assert max(rewards) > 500
    assert "kein cap" in levels._economy_reward_formula_text({})


def test_old_default_500_cap_is_removed_during_settings_migration():
    levels = load_levels_module()
    settings = {"glitzerchipRewardMaxPerLevel": 500, "economyRewardMaxPerLevel": 5000}

    levels._migrate_economy_settings(settings)

    assert settings.get("economyRewardMaxPerLevel", 0) == 0


def test_rank_card_responds_to_slash_interaction(monkeypatch):
    levels = load_levels_module()

    class FakeResponse:
        def __init__(self):
            self.done = False
            self.file = None

        def is_done(self):
            return self.done

        async def send_message(self, **kwargs):
            self.done = True
            self.file = kwargs.get("file")

    class FakeFollowup:
        def __init__(self):
            self.file = None

        async def send(self, **kwargs):
            self.file = kwargs.get("file")

    response = FakeResponse()
    followup = FakeFollowup()
    interaction = SimpleNamespace(response=response, followup=followup)
    ctx = SimpleNamespace(interaction=interaction)
    monkeypatch.setattr(
        levels,
        "discord",
        SimpleNamespace(File=lambda fp, filename: SimpleNamespace(fp=fp, filename=filename)),
    )

    asyncio.run(levels._send_rank_card(ctx, SimpleNamespace(reply=None), b"png"))

    assert response.file.filename == "rank.png"
    assert followup.file is None
