import asyncio
from types import SimpleNamespace

import pytest

from caine.bot import AttachmentInputError, request_text_from_message


class FakeAttachment:
    def __init__(self, filename, content, content_type=None, fail_cached=False):
        self.filename = filename
        self._content = content
        self.content_type = content_type
        self.size = len(content)
        self.fail_cached = fail_cached
        self.read_calls = []

    async def read(self, use_cached=False):
        self.read_calls.append(use_cached)
        if use_cached and self.fail_cached:
            raise AssertionError("cached attachment URL should not be used first")
        return self._content


def run(coro):
    return asyncio.run(coro)


def context_with(*attachments):
    return SimpleNamespace(message=SimpleNamespace(attachments=list(attachments)))


def test_file_only_attachment_becomes_request_text():
    ctx = context_with(FakeAttachment("plugin-idee.txt", b"Baue ein Level-System.", "text/plain"))

    result = run(request_text_from_message(ctx, ""))

    assert "Datei plugin-idee.txt:" in result
    assert "Baue ein Level-System." in result


def test_text_attachment_uses_normal_url_before_cached_proxy():
    attachment = FakeAttachment(
        "BOT_NACHBAU_SPEZIFIKATION.txt",
        b"Bot-Nachbau Inhalt.",
        "text/plain",
        fail_cached=True,
    )
    ctx = context_with(attachment)

    result = run(request_text_from_message(ctx, ""))

    assert "Bot-Nachbau Inhalt." in result
    assert attachment.read_calls == [False]


def test_typed_request_and_attachment_are_combined():
    ctx = context_with(FakeAttachment("details.md", b"Cooldown: 30 Sekunden.", "text/markdown"))

    result = run(request_text_from_message(ctx, "Bitte als Plugin bauen."))

    assert result.startswith("Bitte als Plugin bauen.")
    assert "Cooldown: 30 Sekunden." in result


def test_cp1252_text_attachment_is_supported():
    ctx = context_with(FakeAttachment("idee.txt", b"Pr\xfcfe Rollen fuer Nutzer."))

    result = run(request_text_from_message(ctx, ""))

    assert "Pr\xfcfe Rollen fuer Nutzer." in result


def test_binary_attachment_without_text_request_fails():
    ctx = context_with(FakeAttachment("bild.png", b"\x89PNG\r\n", "image/png"))

    with pytest.raises(AttachmentInputError):
        run(request_text_from_message(ctx, ""))
