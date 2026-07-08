from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from discord.ext import commands

from caine.command_system import build_prefix_command, command_spec
from caine.storage import InMemoryPluginStorage


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
        storage: Any | None = None,
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
        self._storage = storage or InMemoryPluginStorage()

    def command(
        self,
        name: str | dict[str, Any],
        description: str = "",
        aliases: tuple[str, ...] | list[str] = (),
        level: str = "user",
        options: tuple[Any, ...] | list[Any] = (),
        examples: tuple[str, ...] | list[str] | str = (),
        slash: bool | None = None,
        routing_priority: int | str = 50,
        routing_when: str = "",
        routing_not_when: str = "",
    ) -> Callable[[PluginHandler], PluginHandler]:
        def decorator(handler: PluginHandler) -> PluginHandler:
            spec = command_spec(
                name,
                description=description,
                aliases=aliases,
                level=level,
                options=options,
                examples=examples,
                slash_enabled=slash,
                routing_priority=routing_priority,
                routing_when=routing_when,
                routing_not_when=routing_not_when,
            )

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
        return await self._storage.get(self._plugin_name, str(key), default)

    async def storage_set(self, key: str, value: Any) -> None:
        await self._storage.set(self._plugin_name, str(key), value)

    async def storage_delete(self, key: str) -> None:
        await self._storage.delete(self._plugin_name, str(key))
