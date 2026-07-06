from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord.ext import commands

from caine.plugin_api import PluginAPI
from caine.plugin_validation import (
    PluginValidationError,
    ValidationResult,
    extract_plugin_metadata,
    validate_plugin_source,
)


log = logging.getLogger(__name__)
SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class LoadedPlugin:
    name: str
    path: Path
    description: str
    commands: list[str] = field(default_factory=list)


class PluginManager:
    def __init__(
        self,
        bot: commands.Bot,
        pending_dir: Path,
        approved_dir: Path,
        data_dir: Path,
        trusted_plugins: bool = False,
    ) -> None:
        self.bot = bot
        self.pending_dir = pending_dir
        self.approved_dir = approved_dir
        self.data_dir = data_dir
        self.trusted_plugins = trusted_plugins
        self.loaded: dict[str, LoadedPlugin] = {}
        self._plugin_commands: dict[str, list[str]] = {}
        self._plugin_listeners: dict[str, list[tuple[str, Any]]] = {}
        self._plugin_subscriptions: dict[str, list[tuple[str, Any]]] = {}
        self._topic_handlers: dict[str, list[tuple[str, Any]]] = {}
        self.shared: dict[str, Any] = {}

    def ensure_dirs(self) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.approved_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def save_pending(self, suggested_name: str, source: str) -> tuple[str, Path, ValidationResult]:
        self.ensure_dirs()
        slug = self._unique_slug(suggested_name, self.pending_dir)
        path = self.pending_dir / f"{slug}.py"
        path.write_text(source, encoding="utf-8")
        return slug, path, self.validate_source(source)

    def list_pending(self) -> list[tuple[str, Path, ValidationResult]]:
        self.ensure_dirs()
        result: list[tuple[str, Path, ValidationResult]] = []
        for path in sorted(self.pending_dir.glob("*.py")):
            result.append((path.stem, path, self.validate_source(path.read_text(encoding="utf-8"))))
        return result

    def validate_source(self, source: str) -> ValidationResult:
        strict = validate_plugin_source(source)
        if not self.trusted_plugins:
            return strict

        syntax_errors = [error for error in strict.errors if error.startswith("syntax error:")]
        if syntax_errors:
            return ValidationResult(
                ok=False,
                errors=syntax_errors,
                warnings=strict.warnings,
                command_names=strict.command_names,
                metadata=strict.metadata,
            )

        if "setup_plugin" not in source:
            return ValidationResult(
                ok=False,
                errors=["missing setup_plugin(api)"],
                warnings=strict.warnings,
                command_names=strict.command_names,
                metadata=strict.metadata,
            )

        warnings = list(strict.warnings)
        if strict.errors:
            warnings.append("trusted plugin mode: strict validator errors ignored")
        return ValidationResult(
            ok=True,
            errors=[],
            warnings=warnings,
            command_names=strict.command_names,
            metadata=strict.metadata or extract_plugin_metadata(source),
        )

    def pending_path(self, plugin_id: str) -> Path:
        safe_id = slugify(plugin_id)
        return self.pending_dir / f"{safe_id}.py"

    async def approve(self, plugin_id: str) -> LoadedPlugin:
        self.ensure_dirs()
        source_path = self.pending_path(plugin_id)
        if not source_path.exists():
            raise FileNotFoundError(f"pending plugin '{plugin_id}' not found")

        source = source_path.read_text(encoding="utf-8")
        validation = self.validate_source(source)
        if not validation.ok:
            raise PluginValidationError(validation.summary())
        conflicts = [name for name in validation.command_names if name in self.bot.all_commands]
        if conflicts:
            raise PluginValidationError(f"command already exists: {', '.join(conflicts)}")

        target = self.approved_dir / f"{source_path.stem}.py"
        if target.exists():
            target = self.approved_dir / f"{self._unique_slug(source_path.stem, self.approved_dir)}.py"

        shutil.move(str(source_path), str(target))
        try:
            return await self.load_plugin_file(target, validation)
        except Exception:
            if not source_path.exists():
                shutil.move(str(target), str(source_path))
            raise

    def reject(self, plugin_id: str) -> bool:
        path = self.pending_path(plugin_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    async def load_all_approved(self) -> list[LoadedPlugin]:
        self.ensure_dirs()
        loaded: list[LoadedPlugin] = []
        for path in sorted(self.approved_dir.glob("*.py")):
            try:
                loaded.append(await self.load_plugin_file(path))
            except Exception as exc:
                log.warning("skipping plugin %s: %s", path.name, exc)
        return loaded

    async def reload_all_approved(self) -> list[LoadedPlugin]:
        for plugin_name in list(self.loaded):
            self.unload_plugin(plugin_name)
        return await self.load_all_approved()

    async def load_plugin_file(
        self,
        path: Path,
        validation: ValidationResult | None = None,
    ) -> LoadedPlugin:
        source = path.read_text(encoding="utf-8")
        validation = validation or self.validate_source(source)
        if not validation.ok:
            raise PluginValidationError(validation.summary())
        metadata = validation.metadata or extract_plugin_metadata(source)
        plugin_name = slugify(metadata.get("name") or path.stem)

        self.unload_plugin(plugin_name)
        module_name = f"caine_runtime_plugins.{plugin_name}_{abs(hash(path))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginValidationError(f"could not import {path.name}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setup_plugin = getattr(module, "setup_plugin", None)
        if setup_plugin is None:
            raise PluginValidationError("plugin has no setup_plugin")

        loaded = LoadedPlugin(
            name=plugin_name,
            path=path,
            description=metadata.get("description", ""),
        )
        self.loaded[plugin_name] = loaded
        self._plugin_commands[plugin_name] = []

        api = PluginAPI(
            bot=self.bot,
            plugin_name=plugin_name,
            data_dir=self.data_dir,
            register_command=self._register_command,
            register_event=self._register_event,
            subscribe=self._subscribe,
            emit=self._emit,
            manager=self,
            shared=self.shared,
        )
        try:
            result = setup_plugin(api)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.unload_plugin(plugin_name)
            raise

        loaded.commands = list(self._plugin_commands.get(plugin_name, []))
        return loaded

    def unload_plugin(self, plugin_name: str) -> None:
        for command_name in self._plugin_commands.pop(plugin_name, []):
            self.bot.remove_command(command_name)
        for event_name, handler in self._plugin_listeners.pop(plugin_name, []):
            self.bot.remove_listener(handler, event_name)
        for topic, handler in self._plugin_subscriptions.pop(plugin_name, []):
            handlers = self._topic_handlers.get(topic, [])
            self._topic_handlers[topic] = [
                item for item in handlers if not (item[0] == plugin_name and item[1] is handler)
            ]
            if not self._topic_handlers[topic]:
                self._topic_handlers.pop(topic, None)
        self.loaded.pop(plugin_name, None)

    def _register_command(self, plugin_name: str, command: commands.Command) -> None:
        if command.name in self.bot.all_commands:
            raise PluginValidationError(f"command '{command.name}' already exists")
        self.bot.add_command(command)
        self._plugin_commands.setdefault(plugin_name, []).append(command.name)

    def _register_event(self, plugin_name: str, event_name: str, handler: Any) -> None:
        self.bot.add_listener(handler, event_name)
        self._plugin_listeners.setdefault(plugin_name, []).append((event_name, handler))

    def _subscribe(self, plugin_name: str, topic: str, handler: Any) -> None:
        clean_topic = str(topic).strip()
        if not clean_topic:
            raise PluginValidationError("plugin bus topic must not be empty")
        self._topic_handlers.setdefault(clean_topic, []).append((plugin_name, handler))
        self._plugin_subscriptions.setdefault(plugin_name, []).append((clean_topic, handler))

    async def _emit(self, sender_name: str, topic: str, *args: Any, **kwargs: Any) -> list[Any]:
        results: list[Any] = []
        for plugin_name, handler in list(self._topic_handlers.get(str(topic).strip(), [])):
            if plugin_name == sender_name:
                continue
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            results.append(result)
        return results

    def _unique_slug(self, suggested_name: str, folder: Path) -> str:
        base = slugify(suggested_name) or "generated_plugin"
        candidate = base
        index = 2
        while (folder / f"{candidate}.py").exists():
            candidate = f"{base}_{index}"
            index += 1
        return candidate


def slugify(value: str) -> str:
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    value = SLUG_RE.sub("_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if value and value[0].isdigit():
        value = f"plugin_{value}"
    return value[:64]
