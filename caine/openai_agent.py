from __future__ import annotations

import asyncio
import json
from textwrap import dedent

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field


class PluginDraft(BaseModel):
    name: str = Field(description="Short plugin name")
    description: str = Field(description="Human-readable plugin description")
    command_name: str = Field(description="Discord command name without prefix")
    code: str = Field(description="Complete Python plugin code without markdown fences")
    safety_notes: list[str] = Field(default_factory=list)


class PluginUpdateDraft(BaseModel):
    name: str = Field(description="Plugin name")
    description: str = Field(description="Short description of the update")
    code: str = Field(description="Complete updated Python plugin code without markdown fences")
    change_summary: str = Field(description="What changed in this version")
    safety_notes: list[str] = Field(default_factory=list)


class OpenAIAgent:
    def __init__(self, api_key: str, model: str, trusted_plugins: bool = False) -> None:
        self.model = model
        self.trusted_plugins = trusted_plugins
        self.client = OpenAI(api_key=api_key)

    async def answer(self, prompt: str, author_name: str) -> str:
        return await asyncio.to_thread(self._answer_sync, prompt, author_name)

    async def create_plugin(self, request: str, author_name: str) -> PluginDraft:
        return await asyncio.to_thread(self._create_plugin_sync, request, author_name)

    async def update_plugin(
        self,
        plugin_name: str,
        current_source: str,
        request: str,
        author_name: str,
    ) -> PluginUpdateDraft:
        return await asyncio.to_thread(
            self._update_plugin_sync,
            plugin_name,
            current_source,
            request,
            author_name,
        )

    async def health_check(self) -> str:
        return await asyncio.to_thread(self._health_check_sync)

    def _answer_sync(self, prompt: str, author_name: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions=dedent(
                """
                You are C.A.I.N.E., a helpful Discord bot.
                Answer in the same language as the user unless they ask otherwise.
                Keep Discord responses concise and practical.
                """
            ).strip(),
            input=f"{author_name}: {prompt}",
        )
        return getattr(response, "output_text", "").strip() or "Ich habe keine Antwort erhalten."

    def _create_plugin_sync(self, request: str, author_name: str) -> PluginDraft:
        instructions = self._plugin_generation_instructions()

        user_input = dedent(
            f"""
            Author: {author_name}
            Feature request: {request}
            """
        ).strip()

        parse = getattr(self.client.responses, "parse", None)
        if parse is not None:
            try:
                response = parse(
                    model=self.model,
                    instructions=instructions,
                    input=user_input,
                    text_format=PluginDraft,
                )
                draft = getattr(response, "output_parsed", None)
                if isinstance(draft, PluginDraft):
                    draft.code = _clean_code_block(draft.code)
                    return draft
            except TypeError:
                pass

        response = self.client.responses.create(
            model=self.model,
            instructions=(
                instructions
                + "\nReturn valid JSON with keys name, description, command_name, code, safety_notes."
            ),
            input=user_input,
        )
        payload = json.loads(getattr(response, "output_text", "{}"))
        draft = PluginDraft.model_validate(payload)
        draft.code = _clean_code_block(draft.code)
        return draft

    def _update_plugin_sync(
        self,
        plugin_name: str,
        current_source: str,
        request: str,
        author_name: str,
    ) -> PluginUpdateDraft:
        instructions = dedent(
            f"""
            You update an existing Python plugin for a Discord bot.

            Return a complete replacement Python file in the `code` field.
            Do not return a diff and do not use markdown fences.

            Requirements:
            - Preserve the existing PLUGIN name unless the user explicitly asks to rename it.
            - Preserve existing commands and behavior unless the requested change requires edits.
            - Keep async/sync behavior compatible with the current code.
            - If the plugin uses api.storage_get/set/delete, keep the await pattern.
            - If trusted plugin APIs are useful, you may use api.bot, api.manager,
              api.shared, @api.event(...), @api.on(...), and await api.emit(...).
            - The result must be valid Python and include setup_plugin(api).
            - Do not leak or print tokens, environment variables, or secrets.

            Trusted plugin mode is {self.trusted_plugins}.
            """
        ).strip()

        user_input = dedent(
            f"""
            Author: {author_name}
            Plugin id: {plugin_name}
            Requested change:
            {request}

            Current plugin code:
            ```python
            {current_source}
            ```
            """
        ).strip()

        parse = getattr(self.client.responses, "parse", None)
        if parse is not None:
            try:
                response = parse(
                    model=self.model,
                    instructions=instructions,
                    input=user_input,
                    text_format=PluginUpdateDraft,
                )
                draft = getattr(response, "output_parsed", None)
                if isinstance(draft, PluginUpdateDraft):
                    draft.code = _clean_code_block(draft.code)
                    return draft
            except TypeError:
                pass

        response = self.client.responses.create(
            model=self.model,
            instructions=(
                instructions
                + "\nReturn valid JSON with keys name, description, code, change_summary, safety_notes."
            ),
            input=user_input,
        )
        payload = json.loads(getattr(response, "output_text", "{}"))
        draft = PluginUpdateDraft.model_validate(payload)
        draft.code = _clean_code_block(draft.code)
        return draft

    def _plugin_generation_instructions(self) -> str:
        if self.trusted_plugins:
            return dedent(
                """
                You generate trusted Python plugins for a Discord bot.

                Plugin contract:
                - Return a complete Python file in the `code` field.
                - Do not wrap the code in markdown fences.
                - The file must define PLUGIN = {"name": "...", "description": "..."}.
                - The file must define async def setup_plugin(api): ...
                - Register commands with @api.command("command_name", description="...").
                - Command handlers must be async def handler(ctx, args): ...
                - Register Discord listeners with @api.event("message"),
                  @api.event("voice_state_update"), or any discord.py event name.
                - Use await api.reply(ctx, "...") or await api.send(ctx, "...") for replies.
                - Use await api.storage_get/set/delete for small persistent plugin state.
                - Plugins may access api.bot, api.manager, api.shared, api.data_dir,
                  and discord.py objects directly.
                - Plugins may communicate via @api.on("topic") and await api.emit("topic", ...).
                - Keep behavior focused and avoid leaking tokens or secrets.
                """
            ).strip()

        return dedent(
            """
            You generate small Python plugins for a Discord bot.

            Plugin contract:
            - Return a complete Python file in the `code` field.
            - Do not wrap the code in markdown fences.
            - The file must define PLUGIN = {"name": "...", "description": "..."}.
            - The file must define async def setup_plugin(api): ...
            - Register at least one command with @api.command("command_name", description="...").
            - Command handlers must be async def handler(ctx, args): ...
            - Send messages with await api.reply(ctx, "...") or await api.send(ctx, "...").
            - Use await api.storage_get/set/delete for tiny persistent state.
            - Use api.choice for random choices.

            Strict safety rules:
            - Do not import discord, openai, asyncio, aiohttp, requests, os, sys,
              subprocess, pathlib, socket, shutil, importlib, builtins, or typing.
            - Allowed imports only: random, datetime, math, re, statistics, html.
            - Do not use eval, exec, compile, open, input, __import__, globals,
              locals, getattr, setattr, vars, classes, while-loops, files,
              network, environment variables, subprocesses, or reflection.
            - Keep the plugin under 120 lines.
            - Prefer one focused command.
            """
        ).strip()

    def _health_check_sync(self) -> str:
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions="Reply with exactly OK.",
                input="Health check",
            )
        except OpenAIError as exc:
            return f"OpenAI Fehler: {exc.__class__.__name__}: {str(exc)[:500]}"
        text = getattr(response, "output_text", "").strip()
        return text or "OpenAI erreichbar, aber ohne Textantwort."


def _clean_code_block(value: str) -> str:
    code = value.strip()
    if not code.startswith("```"):
        return code + "\n"

    lines = code.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip() + "\n"
