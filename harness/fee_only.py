"""The smallest agent that works: books the monthly bank fee, flags everything else.

It exists so the README's "adding an agent" example is real code that runs:
    .venv/bin/python -m harness.run fee-only
"""


class FeeOnlyAgent:
    name = "fee-only"
    model = "scripted"

    def run(self, tools, instructions=""):
        for line in tools.unrecorded_bank_lines():
            if line["reference"] == "MONTHLY ACCOUNT FEE":
                amount = line["amount"].lstrip("-")
                draft = tools.draft_entry(
                    line["booked_on"], "Monthly account fee",
                    [{"account": "6200", "debit": amount}, {"account": "1000", "credit": amount}],
                    [line["id"]],
                )
                tools.post_entry(draft["entry_id"], {
                    "evidence": [f"bank line {line['id']}"],
                    "rule": "monthly fee goes to bank charges",
                    "confidence": 1.0,
                    "reason": "Same reference as the fee booked in earlier months.",
                })
            else:
                tools.flag([line["id"]], "not a bank fee; leaving it for a human")
