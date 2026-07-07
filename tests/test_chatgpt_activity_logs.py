import json
from types import SimpleNamespace

from caine.openai_agent import (
    ChatGPTActivityLogger,
    OpenAIAgent,
    chatgpt_activity_log_path,
    read_chatgpt_activity_log,
    summarize_chatgpt_activity_entry,
)


class FakeResponses:
    def create(self, *, model, instructions, input):
        return SimpleNamespace(output_text="OK, ich helfe.")


def make_agent(log_path):
    agent = object.__new__(OpenAIAgent)
    agent.model = "test-model"
    agent.trusted_plugins = False
    agent.client = SimpleNamespace(responses=FakeResponses())
    agent.activity_logger = ChatGPTActivityLogger(log_path)
    return agent


def test_answer_writes_chatgpt_activity_log_and_redacts_secrets(tmp_path):
    path = chatgpt_activity_log_path(tmp_path)
    agent = make_agent(path)

    answer = agent._answer_sync("Hallo sk-1234567890abcdef", "Jakob")

    assert answer == "OK, ich helfe."
    entries = read_chatgpt_activity_log(path, limit=10)
    assert [entry["status"] for entry in entries] == ["request", "response"]
    assert [entry["event"] for entry in entries] == ["chatgpt.answer", "chatgpt.answer"]

    serialized = json.dumps(entries, ensure_ascii=False)
    assert "sk-1234567890abcdef" not in serialized
    assert "[REDACTED]" in serialized


def test_chatgpt_activity_summary_is_compact(tmp_path):
    path = chatgpt_activity_log_path(tmp_path)
    agent = make_agent(path)
    agent._answer_sync("Was kannst du?", "Jakob")

    entry = read_chatgpt_activity_log(path, limit=1)[0]
    summary = summarize_chatgpt_activity_entry(entry)

    assert "chatgpt.answer" in summary
    assert "test-model" in summary
