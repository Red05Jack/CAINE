from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field

from caine.command_system import normalize_command_name, normalize_option_name, normalize_option_type


ALLOWED_IMPORTS = {
    "datetime",
    "html",
    "math",
    "random",
    "re",
    "statistics",
}

BANNED_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "vars",
}

BANNED_ATTRS = {
    "__base__",
    "__bases__",
    "__builtins__",
    "__class__",
    "__closure__",
    "__code__",
    "__dict__",
    "__func__",
    "__globals__",
    "__loader__",
    "__module__",
    "__mro__",
    "__self__",
    "__spec__",
    "__subclasses__",
    "attachments",
    "bot",
    "channel",
    "client",
    "guild",
    "message",
    "mentions",
    "voice_client",
}

BANNED_NODE_TYPES = (
    ast.AsyncWith,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Lambda,
    ast.Nonlocal,
    ast.Raise,
    ast.Try,
    ast.While,
    ast.With,
)

COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
COMMAND_LEVELS = {"kinger", "admin", "user", "s1", "s2", "s3"}
COMMAND_OPTION_TYPES = {"string", "integer", "number", "boolean", "user", "channel", "role", "attachment"}


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    command_names: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return "valid"
        return "; ".join(self.errors)


class PluginValidationError(ValueError):
    pass


def validate_plugin_source(source: str) -> ValidationResult:
    source = normalize_plugin_command_source(source)
    errors: list[str] = []
    warnings: list[str] = []
    command_names: list[str] = []
    metadata: dict[str, str] = {}

    if len(source) > 24_000:
        errors.append("plugin is too large")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(False, [f"syntax error: {exc.msg}"], warnings)

    top_level_ok = (ast.Assign, ast.AsyncFunctionDef, ast.Expr, ast.Import, ast.ImportFrom)

    for node in tree.body:
        if not isinstance(node, top_level_ok):
            errors.append(f"top-level {type(node).__name__} is not allowed")
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            errors.append("top-level expressions are not allowed")
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name != "setup_plugin":
                errors.append("only setup_plugin may be defined at top level")
            if node.decorator_list:
                errors.append("top-level decorators are not allowed")

    if not any(isinstance(node, ast.AsyncFunctionDef) and node.name == "setup_plugin" for node in tree.body):
        errors.append("missing async setup_plugin(api)")

    metadata = extract_plugin_metadata(tree)
    if not metadata.get("name"):
        errors.append("PLUGIN metadata must contain a name")
    if not metadata.get("description"):
        warnings.append("PLUGIN metadata should contain a description")

    for node in ast.walk(tree):
        if isinstance(node, BANNED_NODE_TYPES):
            errors.append(f"{type(node).__name__} is not allowed")

        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split(".", 1)[0]
                if root_name not in ALLOWED_IMPORTS:
                    errors.append(f"import '{alias.name}' is not allowed")

        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".", 1)[0]
            if module not in ALLOWED_IMPORTS:
                errors.append(f"from-import '{node.module}' is not allowed")

        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            errors.append(f"name '{node.id}' is not allowed")

        if isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS:
            errors.append(f"attribute '{node.attr}' is not allowed")

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not _is_api_command_decorator(decorator):
                    errors.append("only api.command decorators are allowed")

        if isinstance(node, ast.Call):
            node_command_names = _command_names_from_call(node)
            if node_command_names is not None:
                for command_name in node_command_names:
                    command_names.append(command_name)
                    if not COMMAND_NAME_RE.match(command_name):
                        errors.append(f"command name '{command_name}' is invalid")

                command_level = _command_level_from_call(node)
                if command_level is not None and command_level not in COMMAND_LEVELS:
                    errors.append(f"command level '{command_level}' is invalid")
                errors.extend(_command_option_errors_from_call(node))

    if not command_names:
        errors.append("plugin must register at least one api.command(...)")

    unique_errors = list(dict.fromkeys(errors))
    unique_warnings = list(dict.fromkeys(warnings))
    unique_commands = list(dict.fromkeys(command_names))
    return ValidationResult(
        ok=not unique_errors,
        errors=unique_errors,
        warnings=unique_warnings,
        command_names=unique_commands,
        metadata=metadata,
    )


def validate_plugin_or_raise(source: str) -> ValidationResult:
    result = validate_plugin_source(source)
    if not result.ok:
        raise PluginValidationError(result.summary())
    return result


def normalize_plugin_command_source(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    line_starts = _line_start_offsets(source)
    replacements: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_api_command_decorator(node):
            replacements.extend(_normalization_replacements_for_call(node, line_starts))

    for start, end, text in sorted(replacements, key=lambda item: item[0], reverse=True):
        source = source[:start] + text + source[end:]
    return source


def extract_plugin_metadata(source_or_tree: str | ast.Module) -> dict[str, str]:
    tree = ast.parse(source_or_tree) if isinstance(source_or_tree, str) else source_or_tree
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "PLUGIN":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return {}
                if not isinstance(value, dict):
                    return {}
                return {str(key): str(item) for key, item in value.items()}
    return {}


def _command_names_from_call(node: ast.Call) -> list[str] | None:
    func = node.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "command"
        and isinstance(func.value, ast.Name)
        and func.value.id == "api"
    ):
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return _normalize_command_names([first.value])
    if isinstance(first, ast.Dict):
        return _command_names_from_dict(first)
    return [""]


def _command_level_from_call(node: ast.Call) -> str | None:
    for keyword in node.keywords:
        if keyword.arg == "level" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value).strip().lower()

    if not node.args or not isinstance(node.args[0], ast.Dict):
        return None

    for key, value in zip(node.args[0].keys, node.args[0].values):
        if isinstance(key, ast.Constant) and key.value == "level" and isinstance(value, ast.Constant):
            return str(value.value).strip().lower()
    return None


def _command_names_from_dict(node: ast.Dict) -> list[str]:
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant):
            continue
        if key.value == "name" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            return _normalize_command_names([value.value])
        if key.value == "names":
            names = _literal_string_sequence(value)
            return _normalize_command_names(names) or [""]
    return [""]


def _normalize_command_names(names: list[str]) -> list[str]:
    normalized = [normalize_command_name(name) for name in names]
    return list(dict.fromkeys(name for name in normalized if name))


def _command_option_errors_from_call(node: ast.Call) -> list[str]:
    option_node = _command_options_node_from_call(node)
    if option_node is None:
        return []

    records = _literal_option_dicts(option_node)
    if records is None:
        return ["command options must be a literal list of objects"]

    errors: list[str] = []
    seen_names: set[str] = set()
    for record in records:
        name = normalize_option_name(str(record.get("name", "")))
        if not name:
            errors.append("command option name is invalid")
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        option_type = normalize_option_type(str(record.get("type", "string")))
        if option_type not in COMMAND_OPTION_TYPES:
            errors.append(f"command option '{name}' type is invalid")
    return errors


def _command_options_node_from_call(node: ast.Call) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == "options":
            return keyword.value

    if not node.args or not isinstance(node.args[0], ast.Dict):
        return None

    for key, value in zip(node.args[0].keys, node.args[0].values):
        if isinstance(key, ast.Constant) and key.value == "options":
            return value
    return None


def _literal_string_sequence(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        result = []
        for item in node.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                result.append(item.value)
            else:
                return []
        return result
    return []


def _literal_option_dicts(node: ast.AST) -> list[dict[str, object]] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None

    records: list[dict[str, object]] = []
    for item in node.elts:
        if not isinstance(item, ast.Dict):
            return None
        record: dict[str, object] = {}
        for key, value in zip(item.keys, item.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            key_name = key.value
            if key_name in {"name", "description", "type"} and isinstance(value, ast.Constant):
                record[key_name] = str(value.value)
            elif key_name == "required" and isinstance(value, ast.Constant):
                record[key_name] = bool(value.value)
        records.append(record)
    return records


def _is_api_command_decorator(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _command_names_from_call(node) is not None


def _normalization_replacements_for_call(
    node: ast.Call,
    line_starts: list[int],
) -> list[tuple[int, int, str]]:
    if not node.args:
        return []

    replacements: list[tuple[int, int, str]] = []
    first = node.args[0]

    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        names = _normalize_command_names([first.value])
        if names and names[0] != first.value:
            replacements.append(_node_replacement(first, line_starts, json.dumps(names[0])))
        return replacements

    if not isinstance(first, ast.Dict):
        return replacements

    for key, value in zip(first.keys, first.values):
        if not isinstance(key, ast.Constant):
            continue
        if key.value == "name" and isinstance(value, ast.Constant) and isinstance(value.value, str):
            names = _normalize_command_names([value.value])
            if names and names[0] != value.value:
                replacements.append(_node_replacement(value, line_starts, json.dumps(names[0])))
        elif key.value == "names":
            names = _literal_string_sequence(value)
            normalized_names = _normalize_command_names(names)
            if normalized_names and normalized_names != names:
                replacements.append(_node_replacement(value, line_starts, json.dumps(normalized_names)))
        elif key.value == "options":
            records = _literal_option_dicts(value)
            if records is None:
                continue
            normalized_records = _normalize_option_records(records)
            if normalized_records != records:
                replacements.append(_node_replacement(value, line_starts, json.dumps(normalized_records)))
    return replacements


def _normalize_option_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in records:
        name = normalize_option_name(str(record.get("name", "")))
        if not name or name in seen:
            continue
        seen.add(name)
        description = str(record.get("description", "") or name).strip()[:100]
        option_type = normalize_option_type(str(record.get("type", "string")))
        normalized.append(
            {
                "name": name,
                "description": description,
                "type": option_type,
                "required": bool(record.get("required", False)),
            }
        )
    return sorted(normalized, key=lambda item: not bool(item["required"]))


def _node_replacement(node: ast.AST, line_starts: list[int], text: str) -> tuple[int, int, str]:
    start = line_starts[node.lineno - 1] + node.col_offset
    end = line_starts[node.end_lineno - 1] + node.end_col_offset
    return start, end, text


def _line_start_offsets(source: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", source):
        starts.append(match.end())
    return starts
