from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PLACEHOLDER_VALUES = {
    "your_discord_bot_token_here",
    "your_openai_api_key_here",
}


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _csv_int(value: str | None) -> set[int]:
    result: set[int] = set()
    for item in _csv(value):
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _secret(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = value.strip()
    if cleaned.lower() in PLACEHOLDER_VALUES:
        return ""
    return cleaned


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openai_text_model: str
    openai_router_model: str
    openai_code_model: str
    command_prefix: str
    allowed_guild_ids: set[int]
    plugin_autoload: bool
    trusted_plugins: bool
    project_root: Path
    pending_plugins_dir: Path
    approved_plugins_dir: Path
    data_dir: Path
    command_permissions_path: Path

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def openai_model(self) -> str:
        return self.openai_code_model


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")
    pending = root / "plugins" / "pending"
    approved = root / "plugins" / "approved"
    data = root / "data"
    command_permissions = root / "command_permissions.json"

    return Settings(
        discord_token=_secret(os.getenv("DISCORD_TOKEN")),
        openai_api_key=_secret(os.getenv("OPENAI_API_KEY")),
        openai_text_model=os.getenv("OPENAI_TEXT_MODEL", os.getenv("OPENAI_CHEAP_MODEL", "gpt-5-nano")),
        openai_router_model=os.getenv("OPENAI_ROUTER_MODEL", "gpt-5-mini"),
        openai_code_model=os.getenv("OPENAI_CODE_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.5")),
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        allowed_guild_ids=_csv_int(os.getenv("CAINE_ALLOWED_GUILD_IDS")),
        plugin_autoload=_bool(os.getenv("CAINE_PLUGIN_AUTOLOAD"), True),
        trusted_plugins=_bool(os.getenv("CAINE_TRUSTED_PLUGINS"), False),
        project_root=root,
        pending_plugins_dir=pending,
        approved_plugins_dir=approved,
        data_dir=data,
        command_permissions_path=command_permissions,
    )
