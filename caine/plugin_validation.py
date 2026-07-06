from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field


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

COMMAND_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


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
            command_name = _command_name_from_call(node)
            if command_name is not None:
                command_names.append(command_name)
                if not COMMAND_NAME_RE.match(command_name):
                    errors.append(f"command name '{command_name}' is invalid")

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


def _command_name_from_call(node: ast.Call) -> str | None:
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
        return first.value
    return ""


def _is_api_command_decorator(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _command_name_from_call(node) is not None
