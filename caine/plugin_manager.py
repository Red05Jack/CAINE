from __future__ import annotations

from difflib import SequenceMatcher
import importlib.util
import inspect
import json
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


@dataclass(frozen=True)
class PluginSource:
    plugin_id: str
    path: Path
    location: str
    replaces: str | None = None


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
        self.archive_dir = approved_dir.parent / "archive"
        self.data_dir = data_dir
        self.trusted_plugins = trusted_plugins
        self.loaded: dict[str, LoadedPlugin] = {}
        self._plugin_commands: dict[str, list[str]] = {}
        self._plugin_listeners: dict[str, list[tuple[str, Any]]] = {}
        self._plugin_subscriptions: dict[str, list[tuple[str, Any]]] = {}
        self._topic_handlers: dict[str, list[tuple[str, Any]]] = {}
        self._released_builtin_commands: dict[str, commands.Command] = {}
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

    def save_pending_revision(
        self,
        plugin_id: str,
        source: str,
        source_path: Path,
    ) -> tuple[str, Path, ValidationResult]:
        self.ensure_dirs()
        target_plugin_id = slugify(plugin_id)
        preferred_path = self.pending_dir / f"{target_plugin_id}.py"

        if _same_path(source_path, preferred_path):
            pending_id = target_plugin_id
            path = preferred_path
        elif preferred_path.exists():
            pending_id = self._unique_slug(f"{target_plugin_id}_update", self.pending_dir)
            path = self.pending_dir / f"{pending_id}.py"
        else:
            pending_id = target_plugin_id
            path = preferred_path

        path.write_text(source, encoding="utf-8")
        self._write_pending_meta(
            pending_id,
            {
                "action": "replace",
                "target_plugin_id": target_plugin_id,
                "target_filename": f"{target_plugin_id}.py",
                "source_path": str(source_path),
            },
        )
        return pending_id, path, self.validate_source(source)

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

    def pending_metadata(self, plugin_id: str) -> dict[str, Any]:
        return self._read_pending_meta(self.pending_path(plugin_id))

    def find_plugin_source(self, plugin_id: str) -> PluginSource | None:
        safe_id = slugify(plugin_id)
        pending_path = self.pending_dir / f"{safe_id}.py"
        if pending_path.exists():
            meta = self._read_pending_meta(pending_path)
            replaces = str(meta.get("target_plugin_id") or safe_id)
            return PluginSource(safe_id, pending_path, "pending", replaces)

        approved_path = self.approved_dir / f"{safe_id}.py"
        if approved_path.exists():
            return PluginSource(safe_id, approved_path, "approved", safe_id)

        return self._find_plugin_source_by_metadata(plugin_id)

    async def approve(self, plugin_id: str) -> LoadedPlugin:
        self.ensure_dirs()
        source_path = self.pending_path(plugin_id)
        if not source_path.exists():
            raise FileNotFoundError(f"pending plugin '{plugin_id}' not found")

        source = source_path.read_text(encoding="utf-8")
        validation = self.validate_source(source)
        if not validation.ok:
            raise PluginValidationError(validation.summary())
        meta = self._read_pending_meta(source_path)
        replace_plugin_id = self._replacement_plugin_id(meta)
        target = self.approved_dir / f"{replace_plugin_id or source_path.stem}.py"
        if replace_plugin_id is None and target.exists():
            replace_plugin_id = source_path.stem

        conflicts = self._command_conflicts(validation.command_names, replace_plugin_id)
        if conflicts:
            raise PluginValidationError(f"command already exists: {', '.join(conflicts)}")

        if replace_plugin_id is not None:
            return await self._approve_replacement(source_path, validation, replace_plugin_id)

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
        forced_plugin_name: str | None = None,
    ) -> LoadedPlugin:
        source = path.read_text(encoding="utf-8")
        validation = validation or self.validate_source(source)
        if not validation.ok:
            raise PluginValidationError(validation.summary())
        metadata = validation.metadata or extract_plugin_metadata(source)
        plugin_name = slugify(forced_plugin_name or metadata.get("name") or path.stem)

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
            self._restore_builtin_command_if_unused(command_name)
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
            if self._is_releasable_builtin_command(command.name):
                self._release_builtin_command(command.name)
            else:
                raise PluginValidationError(f"command '{command.name}' already exists")
        self.bot.add_command(command)
        self._plugin_commands.setdefault(plugin_name, []).append(command.name)

    async def _approve_replacement(
        self,
        source_path: Path,
        validation: ValidationResult,
        replace_plugin_id: str,
    ) -> LoadedPlugin:
        target = self.approved_dir / f"{replace_plugin_id}.py"
        archived_path = self._archive_approved_plugin(target)
        moved_pending = False

        try:
            shutil.move(str(source_path), str(target))
            moved_pending = True
            loaded = await self.load_plugin_file(target, validation, forced_plugin_name=replace_plugin_id)
        except Exception:
            self.unload_plugin(replace_plugin_id)
            if moved_pending and target.exists():
                if source_path.exists():
                    target.unlink(missing_ok=True)
                else:
                    shutil.move(str(target), str(source_path))
            if archived_path is not None and archived_path.exists():
                shutil.move(str(archived_path), str(target))
                try:
                    await self.load_plugin_file(target, forced_plugin_name=replace_plugin_id)
                except Exception as restore_exc:
                    log.warning("could not reload previous plugin %s: %s", replace_plugin_id, restore_exc)
            raise

        source_path.unlink(missing_ok=True)
        self._pending_meta_path(source_path.stem).unlink(missing_ok=True)
        return loaded

    def _command_conflicts(
        self,
        command_names: list[str],
        replace_plugin_id: str | None = None,
    ) -> list[str]:
        allowed_existing = set(self._plugin_commands.get(replace_plugin_id or "", []))
        return [
            name
            for name in command_names
            if name in self.bot.all_commands and name not in allowed_existing
            and not self._is_releasable_builtin_command(name)
        ]

    def _archive_approved_plugin(self, target: Path) -> Path | None:
        if not target.exists():
            return None

        self.archive_dir.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            archive_path = self.archive_dir / f"{target.stem}V{index}{target.suffix}"
            if not archive_path.exists():
                shutil.move(str(target), str(archive_path))
                return archive_path
            index += 1

    def _is_releasable_builtin_command(self, command_name: str) -> bool:
        if command_name != "help":
            return False
        command = self.bot.all_commands.get(command_name)
        if command is None:
            return False
        callback = getattr(command, "callback", None)
        return (
            getattr(command, "module", "") == "discord.ext.commands.help"
            and getattr(callback, "__qualname__", "") == "HelpCommand.command_callback"
        )

    def _release_builtin_command(self, command_name: str) -> None:
        removed = self.bot.remove_command(command_name)
        if removed is not None:
            self._released_builtin_commands.setdefault(command_name, removed)

    def _restore_builtin_command_if_unused(self, command_name: str) -> None:
        command = self._released_builtin_commands.get(command_name)
        if command is None or command_name in self.bot.all_commands:
            return
        self.bot.add_command(command)
        self._released_builtin_commands.pop(command_name, None)

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

    def _pending_meta_path(self, plugin_id: str) -> Path:
        return self.pending_dir / f"{slugify(plugin_id)}.meta.json"

    def _read_pending_meta(self, pending_path: Path) -> dict[str, Any]:
        meta_path = self._pending_meta_path(pending_path.stem)
        if not meta_path.exists():
            return {}
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_pending_meta(self, plugin_id: str, data: dict[str, Any]) -> None:
        with self._pending_meta_path(plugin_id).open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=True)

    def _replacement_plugin_id(self, meta: dict[str, Any]) -> str | None:
        if meta.get("action") != "replace":
            return None
        value = str(meta.get("target_plugin_id") or "").strip()
        return slugify(value) or None

    def _find_plugin_source_by_metadata(self, plugin_id: str) -> PluginSource | None:
        query = _compact_slug(slugify(plugin_id))
        if not query:
            return None

        best: tuple[float, PluginSource] | None = None
        for location, folder in (("pending", self.pending_dir), ("approved", self.approved_dir)):
            for path in sorted(folder.glob("*.py")):
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                validation = self.validate_source(source)
                metadata = validation.metadata or extract_plugin_metadata(source)
                meta = self._read_pending_meta(path) if location == "pending" else {}
                target_plugin_id = str(meta.get("target_plugin_id") or path.stem)

                candidates = [path.stem, slugify(metadata.get("name", ""))]
                candidates.extend(validation.command_names)
                score = max((_match_score(query, candidate) for candidate in candidates), default=0.0)
                if score < 0.72:
                    continue
                source_info = PluginSource(
                    plugin_id=path.stem,
                    path=path,
                    location=location,
                    replaces=slugify(target_plugin_id),
                )
                if best is None or score > best[0]:
                    best = (score, source_info)

        return best[1] if best else None

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


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _compact_slug(value: str) -> str:
    return slugify(value).replace("_", "")


def _match_score(query: str, candidate: str) -> float:
    compact = _compact_slug(candidate)
    if not compact:
        return 0.0
    if query == compact:
        return 1.0
    if query in compact or compact in query:
        return 0.9
    return SequenceMatcher(None, query, compact).ratio()
