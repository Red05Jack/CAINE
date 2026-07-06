from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from discord.ext import commands

from caine.command_system import build_prefix_command, command_spec


PluginHandler = Callable[[commands.Context, str], Awaitable[None]]
EventHandler = Callable[..., Any]


class PluginAPI:
    def __init__(
        self,
        bot: commands.Bot,
        plugin_name: str,
        data_dir: Path,
        register_command: Callable[[str, commands.Command], None],
        register_event: Callable[[str, str, EventHandler], None],
        subscribe: Callable[[str, str, EventHandler], None],
        emit: Callable[[str, str, Any], Awaitable[list[Any]]],
        manager: Any,
        shared: dict[str, Any],
    ) -> None:
        self.bot = bot
        self.manager = manager
        self.shared = shared
        self.plugin_name = plugin_name
        self.data_dir = data_dir

        self._bot = bot
        self._plugin_name = plugin_name
        self._data_dir = data_dir
        self._register_command = register_command
        self._register_event = register_event
        self._subscribe = subscribe
        self._emit = emit
        self._state_path = data_dir / f"{plugin_name}.json"

    def command(
        self,
        name: str | dict[str, Any],
        description: str = "",
        aliases: tuple[str, ...] | list[str] = (),
        level: str = "user",
    ) -> Callable[[PluginHandler], PluginHandler]:
        def decorator(handler: PluginHandler) -> PluginHandler:
            spec = command_spec(name, description=description, aliases=aliases, level=level)

            async def callback(ctx: commands.Context, args: str = "") -> None:
                await handler(ctx, args)

            command = build_prefix_command(
                self._bot,
                spec,
                callback,
                plugin_name=self._plugin_name,
            )
            self._register_command(self._plugin_name, command)
            return handler

        return decorator

    def event(self, name: str) -> Callable[[EventHandler], EventHandler]:
        listener_name = name if name.startswith("on_") else f"on_{name}"

        def decorator(handler: EventHandler) -> EventHandler:
            self._register_event(self._plugin_name, listener_name, handler)
            return handler

        return decorator

    def on(self, topic: str) -> Callable[[EventHandler], EventHandler]:
        def decorator(handler: EventHandler) -> EventHandler:
            self._subscribe(self._plugin_name, topic, handler)
            return handler

        return decorator

    async def emit(self, topic: str, *args: Any, **kwargs: Any) -> list[Any]:
        return await self._emit(self._plugin_name, topic, *args, **kwargs)

    async def reply(self, ctx: commands.Context, content: str) -> None:
        await ctx.reply(content[:1900], mention_author=False)

    async def send(self, ctx: commands.Context, content: str) -> None:
        await ctx.send(content[:1900])

    def choice(self, values: list[Any] | tuple[Any, ...]) -> Any:
        if not values:
            return None
        return random.choice(list(values))

    async def storage_get(self, key: str, default: Any = None) -> Any:
        state = self._read_state()
        return state.get(key, default)

    async def storage_set(self, key: str, value: Any) -> None:
        state = self._read_state()
        state[key] = value
        self._write_state(state)

    async def storage_delete(self, key: str) -> None:
        state = self._read_state()
        state.pop(key, None)
        self._write_state(state)

    def _read_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            with self._state_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=True)
