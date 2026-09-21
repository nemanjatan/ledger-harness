"""The LLM adapter's loop, checked with a fake client: tool dispatch, results back in one
message, usage and cost accounting, done, refusal, turn cap. No network."""

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from harness.llm import TOOLS, LLMAgent, TaskError
from harness.tools import Toolbox
from harness.world import build_world


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)

    def to_dict(self):
        return self.__dict__


@dataclass
class Usage:
    input_tokens: int = 1000
    output_tokens: int = 100
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Response:
    content: list
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    stop_details: object = None


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})   # snapshot; the loop mutates it
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def tool_use(name, **input):
    return Block("tool_use", id=f"id-{name}", name=name, input=input)


@pytest.fixture
def setup(conn, agent, reviewer):
    world = build_world(conn, reviewer, seed=1, labels=["invoice_paid_exactly"])
    return world, Toolbox(agent, reviewer, world.company_id)


def test_tools_match_the_toolbox():
    names = {t["name"] for t in TOOLS} - {"done"}
    assert all(callable(getattr(Toolbox, n, None)) for n in names), names
    assert all(not n.startswith("_") for n in names)


def test_loop_dispatches_tools_and_accounts_for_usage(setup):
    world, tools = setup
    line_id = next(iter(world.new_bank_line_ids()))
    client = FakeClient([
        Response([tool_use("unrecorded_bank_lines"), tool_use("open_invoices")], "tool_use"),
        Response([tool_use("draft_entry", posting_date="2026-04-02", memo="m",
                           lines=[{"account": "1000", "debit": "5.00"}, {"account": "1100", "credit": "5.00"}],
                           bank_line_ids=[line_id])], "tool_use"),
        Response([tool_use("post_entry", entry_id=world.history_max_entry_id + 1,
                           reasoning={"evidence": ["x"], "rule": "r", "confidence": 0.5, "reason": "why"})], "tool_use",
                 Usage(input_tokens=2000, output_tokens=50, cache_read_input_tokens=500)),
        Response([Block("text", text="all done"), tool_use("done", summary="finished")], "tool_use"),
    ])
    llm = LLMAgent(model="claude-opus-5", client=client)
    llm.run(tools, "instructions")
    assert llm.summary == "finished"
    assert [c.tool for c in tools.calls] == ["unrecorded_bank_lines", "open_invoices", "draft_entry", "post_entry"]
    assert tools.traces[0]["posted"] is True
    # both parallel results went back in one user message
    second_request = client.messages.requests[1]
    assert [b["type"] for b in second_request["messages"][-1]["content"]] == ["tool_result", "tool_result"]
    assert second_request["system"] and second_request["tools"] is TOOLS
    assert second_request["messages"][0]["content"] == "instructions"
    assert llm.usage.requests == 4 and llm.usage.input_tokens == 5000 and llm.usage.cache_read_input_tokens == 500
    assert llm.usage.cost_usd("claude-opus-5") == Decimal("0.0340")   # (5000*5 + 350*25 + 500*0.5) per million


def test_bad_tool_call_is_reported_not_fatal(setup):
    world, tools = setup
    client = FakeClient([
        Response([tool_use("history_for")], "tool_use"),        # missing required argument
        Response([tool_use("no_such_tool")], "tool_use"),
        Response([tool_use("done", summary="s")], "tool_use"),
    ])
    LLMAgent(client=client).run(tools)
    results = [r["messages"][-1]["content"][0] for r in client.messages.requests[1:]]
    assert results[0]["is_error"] and "bad arguments" in results[0]["content"]
    assert results[1]["is_error"] and "unknown tool" in results[1]["content"]


def test_refusal_and_turn_cap_are_task_errors(setup):
    world, tools = setup
    with pytest.raises(TaskError, match="refusal"):
        LLMAgent(client=FakeClient([Response([], "refusal")])).run(tools)
    endless = FakeClient([Response([tool_use("list_accounts")], "tool_use")] * 3)
    with pytest.raises(TaskError, match="turn cap"):
        LLMAgent(client=endless, max_turns=3).run(tools)


def test_end_turn_without_done_still_finishes(setup):
    world, tools = setup
    client = FakeClient([Response([Block("text", text="nothing to do")], "end_turn")])
    llm = LLMAgent(client=client)
    llm.run(tools)
    assert llm.summary == "nothing to do"
