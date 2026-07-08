from types import SimpleNamespace

from caine.openai_agent import CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS, CommandRoute, OpenAIAgent


def make_agent(trusted_plugins=False):
    agent = object.__new__(OpenAIAgent)
    agent.trusted_plugins = trusted_plugins
    return agent


def test_untrusted_plugin_generation_instructions_require_hierarchy_and_shared_api():
    instructions = make_agent(False)._plugin_generation_instructions()

    assert "CAINE PLUGIN-HIERARCHIE" in instructions
    assert "S3 / Commands / Nutzer" in instructions
    assert "S2 / Admin commands / Moderation" in instructions
    assert "S1 / Kinger / Master" in instructions
    assert '"examples": ["!command example"]' in instructions
    assert "Every command must include 1-3 examples" in instructions
    assert '"routing": {' in instructions
    assert "Every command must include routing metadata" in instructions
    assert 'api.shared["plugin_id.api"]' in instructions
    assert "Do not use @api.on(...) in untrusted plugins" in instructions


def test_plugin_concept_instructions_describe_command_and_python_api_layers():
    assert "Commands / Nutzer" in CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS
    assert "Admin commands / Moderation" in CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS
    assert "Kinger / Master" in CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS
    assert "Python-Schnittstelle fuer andere Plugins" in CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS


def test_trusted_plugin_generation_instructions_allow_bus_integrations():
    instructions = make_agent(True)._plugin_generation_instructions()

    assert "CAINE PLUGIN-HIERARCHIE" in instructions
    assert "Build the command surface in the hierarchy" in instructions
    assert 'api.shared["plugin_id.api"]' in instructions
    assert '@api.on("topic")' in instructions
    assert "await api.emit" in instructions
    assert '"slash": false' in instructions
    assert "Every command must include 1-3 examples" in instructions
    assert "Every command must include routing metadata" in instructions
    assert "Admin and kinger commands must stay prefix-only" in instructions


def test_plugin_update_instructions_keep_hierarchy_contract():
    instructions = make_agent(False)._plugin_update_instructions()

    assert "CAINE PLUGIN-HIERARCHIE" in instructions
    assert "S3 user commands" in instructions
    assert "S2 admin moderation/config commands" in instructions
    assert "S1 kinger" in instructions
    assert "Python-level API for other plugins" in instructions
    assert '"examples": ["!command example"]' in instructions
    assert "routing metadata" in instructions
    assert "Slash commands are currently S3/user-only" in instructions
    assert "Return only a minimal list of exact source replacements" in instructions
    assert "Do not return a complete replacement file" in instructions


def test_command_router_prompt_includes_replied_caine_message_context():
    class FakeResponses:
        def __init__(self):
            self.input = ""
            self.instructions = ""

        def parse(self, *, model, instructions, input, text_format):
            self.input = input
            self.instructions = instructions
            return SimpleNamespace(output_parsed=CommandRoute(command_name="ask", args="mach das", confidence=0.9))

    responses = FakeResponses()
    agent = object.__new__(OpenAIAgent)
    agent.model = "test-model"
    agent.client = SimpleNamespace(responses=responses)

    route = agent._select_command_for_message_sync(
        "mach das bitte",
        "mach das bitte",
        "Jakob",
        [{"name": "ask", "aliases": [], "description": "Ask CAINE"}],
        "!",
        "CAINE: Nutze `!help level_manege` fuer Details.",
    )

    assert route.command_name == "ask"
    assert "Replied-to CAINE message: CAINE: Nutze `!help level_manege` fuer Details." in responses.input
    assert "replied-to message as context" in responses.instructions
    assert "already filtered by the user's" in responses.instructions
    assert "including admin or kinger commands" in responses.instructions
    assert "routing.priority" in responses.instructions
    assert "routing.avoid_when" in responses.instructions
    assert "Prefer \"ask\" for broad or vague help/capability questions" in responses.instructions
    assert "Use \"help\" only when the user explicitly asks for a command list" in responses.instructions
    assert "Never route destructive commands" not in responses.instructions
