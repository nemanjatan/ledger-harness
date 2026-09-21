"""LLM adapter: a Claude model drives the toolbox through the Messages API tool-use loop.

The model sees exactly what the scripted baseline sees, one tool per toolbox method, plus a
`done` tool to end the turn. It gets the task instructions from harness.tasks and nothing
about the ground truth. The loop is manual so that every request's token usage is recorded
and priced, and so that a refusal or a runaway turn count is recorded as a task error rather
than handled behind the harness's back. No fallback to another model: a result labelled with
a model id is that model's alone.

Prices are Anthropic first-party rates cached from the claude-api skill on 2026-06-24, in USD
per million tokens. Cache writes cost 1.25x input; cache reads 0.1x input, except where the
table says otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from harness.tools import TRACE_SCHEMA, Toolbox

DEFAULT_MODEL = "claude-opus-5"
MAX_TURNS = 80

# input, output, cache write, cache read; USD per 1M tokens
PRICES = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00"), Decimal("6.25"), Decimal("0.50")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00"), Decimal("2.50"), Decimal("0.20")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00"), Decimal("1.25"), Decimal("0.10")),
    "claude-fable-5-1": (Decimal("10.00"), Decimal("50.00"), Decimal("12.50"), Decimal("0.25")),
}

SYSTEM = """\
You are a careful bookkeeper working in a sandbox ledger through tools. Read before you write:
look at the unrecorded bank lines, the open invoices and bills, the vendors, the payout reports
and the history of similar lines before deciding anything. Every entry you post must be one a
reviewer could defend from the evidence. When you are unsure, flag the line for a human rather
than guess, and never make a difference disappear into a suspense or miscellaneous account.
Post entries with post_entry, which needs a reasoning trace. When there is nothing more to do,
call done with a short summary."""

LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "account": {"type": "string", "description": "Account code, e.g. 1000."},
        "debit": {"type": "string", "description": "Decimal amount as a string, e.g. '123.45'. Omit or '0' for a credit line."},
        "credit": {"type": "string", "description": "Decimal amount as a string. Omit or '0' for a debit line."},
        "description": {"type": "string"},
    },
    "required": ["account"],
    "additionalProperties": False,
}

ENTRY_PROPERTIES = {
    "posting_date": {"type": "string", "description": "ISO date, e.g. 2026-04-02."},
    "memo": {"type": "string"},
    "lines": {"type": "array", "minItems": 2, "items": LINE_SCHEMA},
    "bank_line_ids": {"type": "array", "items": {"type": "integer"},
                      "description": "The bank line or lines this entry records. Include a duplicate or pending line here when this entry covers it; if you only notice one after posting, flag it instead, since a posted entry cannot take new links."},
    "idempotency_key": {"type": "string", "description": "Optional. Unique per company; reuse it to retry safely."},
}


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties or {}, "required": list(required),
                         "additionalProperties": False},
    }


TOOLS = [
    tool("list_accounts", "The chart of accounts: code, name, kind."),
    tool("list_periods", "Accounting periods with their status. Posting into a locked period is refused."),
    tool("unrecorded_bank_lines", "Bank lines not yet linked to any entry. Positive amount is money in, negative is money out. Status is pending or cleared."),
    tool("open_invoices", "Sales invoices with an outstanding balance: number, customer, dates, amount, outstanding."),
    tool("open_bills", "Purchase bills with an outstanding balance: number, vendor, dates, amount, outstanding."),
    tool("list_vendors", "Vendors and their default expense account code, if any."),
    tool("payout_reports", "Card processor payout reports: gross collected, fees kept, net paid, and which invoices."),
    tool("history_for", "Recorded bank lines whose reference or counterparty contains the text, with the entry that recorded each. Use it to see how something was booked before.",
         {"reference_like": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["reference_like"]),
    tool("get_entry", "One journal entry with its lines, status and linked bank lines.",
         {"entry_id": {"type": "integer"}}, ["entry_id"]),
    tool("validate_entry", "Dry run: the ledger's verdict on an entry (balance, line shape, accounts, links) without keeping it. Returns ok or the name of the invariant it breaks.",
         ENTRY_PROPERTIES, ["posting_date", "memo", "lines"]),
    tool("draft_entry", "Create a draft entry with its lines and the bank lines it records. Refused outright if it breaks an invariant. Returns the entry id.",
         ENTRY_PROPERTIES, ["posting_date", "memo", "lines", "bank_line_ids"]),
    tool("post_entry", "Ask for a draft to be approved and posted. Requires a reasoning trace. The ledger may refuse (for example period_open when the date is in a locked period); the refusal is returned and the draft stays a draft.",
         {"entry_id": {"type": "integer"}, "reasoning": TRACE_SCHEMA}, ["entry_id", "reasoning"]),
    tool("discard_draft", "Delete a draft you no longer want.", {"entry_id": {"type": "integer"}}, ["entry_id"]),
    tool("flag", "Leave a bank line, or a draft, for a human, with the reason. Use it for anything you will not record.",
         {"bank_line_ids": {"type": "array", "items": {"type": "integer"}}, "reason": {"type": "string"},
          "entry_id": {"type": "integer"}}, ["bank_line_ids", "reason"]),
    tool("done", "Finish. Call this when every unrecorded line has been recorded, covered or flagged.",
         {"summary": {"type": "string"}}, ["summary"]),
]


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage) -> None:
        self.requests += 1
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> Decimal | None:
        if model not in PRICES:
            return None
        p_in, p_out, p_write, p_read = PRICES[model]
        million = Decimal(1_000_000)
        return ((self.input_tokens * p_in + self.output_tokens * p_out
                 + self.cache_creation_input_tokens * p_write + self.cache_read_input_tokens * p_read) / million
                ).quantize(Decimal("0.0001"))

    def as_dict(self, model: str) -> dict:
        cost = self.cost_usd(model)
        return {"requests": self.requests, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_creation_input_tokens": self.cache_creation_input_tokens,
                "cache_read_input_tokens": self.cache_read_input_tokens,
                "cost_usd": None if cost is None else str(cost)}


class TaskError(Exception):
    """The run ended for a reason that is a result in itself (refusal, turn cap)."""


@dataclass
class LLMAgent:
    model: str = DEFAULT_MODEL
    effort: str = "high"
    client: object = None                       # anthropic.Anthropic, or a fake in tests
    max_turns: int = MAX_TURNS
    usage: Usage = field(default_factory=Usage)
    transcript: list = field(default_factory=list)
    summary: str = ""

    @property
    def name(self) -> str:
        return f"{self.model}-{self.effort}"

    def __post_init__(self):
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic()

    def run(self, tools: Toolbox, instructions: str = "") -> None:
        self.usage = Usage()
        self.transcript = []
        self.summary = ""
        messages = [{"role": "user", "content": instructions or "Record the unrecorded bank lines."}]
        for _ in range(self.max_turns):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
            )
            self.usage.add(response.usage)
            self.transcript.append([b.to_dict() if hasattr(b, "to_dict") else dict(b) for b in response.content])
            if response.stop_reason == "refusal":
                raise TaskError(f"refusal: {getattr(response, 'stop_details', None)}")
            if response.stop_reason == "max_tokens":
                raise TaskError("max_tokens reached in one response")
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                self.summary = " ".join(b.text for b in response.content if b.type == "text")
                return
            messages.append({"role": "assistant", "content": response.content})
            results = []
            finished = False
            for use in tool_uses:
                if use.name == "done":
                    self.summary = str(use.input.get("summary", ""))
                    finished = True
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": "ok"})
                    continue
                results.append(self.dispatch(tools, use))
            messages.append({"role": "user", "content": results})
            if finished:
                return
        raise TaskError(f"turn cap of {self.max_turns} reached")

    @staticmethod
    def dispatch(tools: Toolbox, use) -> dict:
        method = getattr(tools, use.name, None)
        if method is None or use.name.startswith("_"):
            return {"type": "tool_result", "tool_use_id": use.id, "content": f"unknown tool {use.name}", "is_error": True}
        try:
            result = method(**dict(use.input))
        except TypeError as exc:
            return {"type": "tool_result", "tool_use_id": use.id, "content": f"bad arguments: {exc}", "is_error": True}
        except Exception as exc:  # noqa: BLE001 - the model should see the failure and carry on
            tools.agent.rollback()
            tools.reviewer.rollback()
            return {"type": "tool_result", "tool_use_id": use.id, "content": f"tool failed: {type(exc).__name__}: {exc}", "is_error": True}
        return {"type": "tool_result", "tool_use_id": use.id, "content": json.dumps(result, default=str)}
