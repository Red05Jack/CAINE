from caine.openai_agent import CAINE_INSPIRED_PLUGIN_BOT_INSTRUCTIONS, OpenAIAgent


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


def test_plugin_update_instructions_keep_hierarchy_contract():
    instructions = make_agent(False)._plugin_update_instructions()

    assert "CAINE PLUGIN-HIERARCHIE" in instructions
    assert "S3 user commands" in instructions
    assert "S2 admin moderation/config commands" in instructions
    assert "S1 kinger" in instructions
    assert "Python-level API for other plugins" in instructions
