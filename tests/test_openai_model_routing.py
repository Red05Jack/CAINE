from types import SimpleNamespace

from caine.config import load_settings
from caine.openai_agent import CommandRoute, OpenAIAgent, PluginDraft, PluginUpdateDraft


class RecordingResponses:
    def __init__(self):
        self.create_models = []
        self.parse_models = []

    def create(self, *, model, instructions, input):
        self.create_models.append(model)
        return SimpleNamespace(output_text="OK")

    def parse(self, *, model, instructions, input, text_format):
        self.parse_models.append((model, text_format))
        if text_format is CommandRoute:
            return SimpleNamespace(output_parsed=CommandRoute(command_name="help", args="", confidence=0.9))
        if text_format is PluginDraft:
            return SimpleNamespace(
                output_parsed=PluginDraft(
                    name="demo",
                    description="Demo plugin",
                    command_name="demo",
                    code="PLUGIN = {'name': 'demo', 'description': 'Demo'}\n",
                )
            )
        if text_format is PluginUpdateDraft:
            return SimpleNamespace(
                output_parsed=PluginUpdateDraft(
                    name="demo",
                    description="Demo update",
                    code="PLUGIN = {'name': 'demo', 'description': 'Demo'}\n",
                    change_summary="Updated demo plugin",
                )
            )
        raise AssertionError(f"unexpected text_format {text_format}")


def make_agent():
    responses = RecordingResponses()
    agent = object.__new__(OpenAIAgent)
    agent.text_model = "cheap-model"
    agent.code_model = "code-model"
    agent.model = "code-model"
    agent.trusted_plugins = False
    agent.client = SimpleNamespace(responses=responses)
    return agent, responses


def test_config_splits_text_and_code_models(monkeypatch):
    monkeypatch.setenv("OPENAI_TEXT_MODEL", "cheap-model")
    monkeypatch.setenv("OPENAI_CODE_MODEL", "code-model")
    monkeypatch.setenv("OPENAI_MODEL", "legacy-model")

    settings = load_settings()

    assert settings.openai_text_model == "cheap-model"
    assert settings.openai_code_model == "code-model"
    assert settings.openai_model == "code-model"


def test_text_processing_uses_text_model():
    agent, responses = make_agent()

    agent._answer_sync("Was kannst du?", "Jakob")
    agent._health_check_sync()
    agent._select_command_for_message_sync(
        "hey caine was kann ich machen?",
        "was kann ich machen?",
        "Jakob",
        [{"name": "help", "aliases": [], "description": "Shows help"}],
        "!",
    )

    assert responses.create_models == ["cheap-model", "cheap-model"]
    assert responses.parse_models == [("cheap-model", CommandRoute)]


def test_code_generation_uses_code_model():
    agent, responses = make_agent()

    agent._create_plugin_sync("Mach ein Demo-Plugin", "Jakob")
    agent._update_plugin_sync("demo", "PLUGIN = {}\n", "Mach es besser", "Jakob")

    assert responses.create_models == []
    assert responses.parse_models == [("code-model", PluginDraft), ("code-model", PluginUpdateDraft)]
