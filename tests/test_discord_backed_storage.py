import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from caine.plugin_api import PluginAPI
from caine.storage import BACKUP_FILE_NAME, DiscordBackedPluginStorage, InMemoryPluginStorage


class FakeAttachment:
    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


class FakeMessage:
    def __init__(self, message_id, attachments):
        self.id = message_id
        self.attachments = attachments
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeChannel:
    name = "bot-db"
    id = 123

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.next_id = 1000

    def history(self, limit=100):
        async def iterator():
            for message in list(self.messages)[: limit or None]:
                yield message

        return iterator()

    async def send(self, content=None, file=None):
        assert file is not None
        file.fp.seek(0)
        data = file.fp.read()
        message = FakeMessage(self.next_id, [FakeAttachment(file.filename, data)])
        message.content = content
        self.next_id += 1
        self.messages.insert(0, message)
        return message


class FakeGuild:
    def __init__(self, channel=None):
        self.text_channels = [channel] if channel is not None else []
        self.created_channels = []

    async def create_text_channel(self, name, reason=None):
        channel = FakeChannel()
        channel.name = name
        self.text_channels.append(channel)
        self.created_channels.append((name, reason))
        return channel


class FakeBot:
    def __init__(self, guild):
        self.guilds = [guild]

    def get_channel(self, channel_id):
        for guild in self.guilds:
            for channel in guild.text_channels:
                if channel.id == channel_id:
                    return channel
        return None


def run(coro):
    return asyncio.run(coro)


def backup_message(payload, message_id=1):
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    return FakeMessage(message_id, [FakeAttachment(BACKUP_FILE_NAME, data)])


def test_discord_storage_loads_backup_and_writes_updated_snapshot(tmp_path):
    channel = FakeChannel(
        [
            backup_message(
                {
                    "version": 1,
                    "plugins": {"demo": {"count": 2}},
                }
            )
        ]
    )
    storage = DiscordBackedPluginStorage(FakeBot(FakeGuild(channel)), legacy_data_dir=tmp_path)

    run(storage.load())
    assert storage.last_loaded_source == "discord"
    assert run(storage.get("demo", "count")) == 2

    run(storage.set("demo", "count", 3))

    latest = channel.messages[0]
    written = json.loads(latest.attachments[0]._data.decode("utf-8"))
    assert written["plugins"]["demo"]["count"] == 3


def test_discord_storage_migrates_legacy_plugin_json_once(tmp_path):
    (tmp_path / "demo.json").write_text('{"legacy": true}', encoding="utf-8")
    channel = FakeChannel()
    storage = DiscordBackedPluginStorage(FakeBot(FakeGuild(channel)), legacy_data_dir=tmp_path)

    run(storage.load())

    assert storage.last_loaded_source == "legacy-local"
    assert run(storage.get("demo", "legacy")) is True
    written = json.loads(channel.messages[0].attachments[0]._data.decode("utf-8"))
    assert written["plugins"]["demo"]["legacy"] is True


def test_plugin_api_storage_uses_shared_memory_without_local_files(tmp_path):
    storage = InMemoryPluginStorage()
    api = PluginAPI(
        bot=SimpleNamespace(),
        plugin_name="demo",
        data_dir=Path(tmp_path),
        register_command=lambda plugin_name, command: None,
        register_event=lambda plugin_name, event_name, handler: None,
        subscribe=lambda plugin_name, topic, handler: None,
        emit=lambda plugin_name, topic, *args, **kwargs: [],
        manager=SimpleNamespace(),
        shared={},
        storage=storage,
    )

    run(api.storage_set("state", {"value": 42}))

    assert run(api.storage_get("state")) == {"value": 42}
    assert not (tmp_path / "demo.json").exists()
