# Design decisions

One paragraph each, in the order I made them, so the shape of the harness can be followed
decision by decision rather than reverse-engineered from the code.

## 1. Invariants live in the database, and every rejection is a named check violation

Balance, period lock, line shape and period immutability are triggers and constraints in
`harness/schema.sql`, not checks in Python. The reason is the harness's whole claim: an agent
cannot route around the rule, whatever adapter it comes through, because the only path to the
table goes through Postgres. Every rejection is raised as SQLSTATE 23514 (`check_violation`)
with the invariant's name in the constraint field, so a caller sees one contract whether a
real CHECK or a trigger did the rejecting: catch `CheckViolation`, read
`diag.constraint_name`. Tests assert on that name, never on message text.

## 2. Balance is checked at commit with a deferred constraint trigger, for every entry

`sum(debit) = sum(credit)` cannot be a row constraint because it spans rows, and it cannot be
an immediate trigger because the first line of any entry is unbalanced by itself. A
`DEFERRABLE INITIALLY DEFERRED` constraint trigger runs once per touched line at commit, so an
entry and its lines go in as one transaction and the transaction stands or falls as a whole.
The rule applies to drafts as well as posted entries: there is one rule, no status branch, and
a draft is not a place to park half an entry. The adapter validates a draft in memory before
it touches the table; the ledger rejects at commit if that validation was wrong. An entry
also needs at least two lines, stated as its own invariant so the error says why. This
matches what Xero, QuickBooks and NetSuite do with a manual journal: an unbalanced one cannot
be saved, draft or not, and a draft is exactly what a human approves, so it has to be a whole
entry. It also gives the adapter's validate tool a clean definition: run the
insert, `SET CONSTRAINTS ALL IMMEDIATE`, read the constraint name, roll back. Validation is
then the database's own opinion, and cannot drift from enforcement.

## 3. Debit and credit are two non-negative columns, exactly one zero

This is the shape most general ledgers use (rather than one signed amount), it reads the way a
bookkeeper reads, and the `(debit = 0) <> (credit = 0)` check makes a zero line or a
both-sides line impossible. Amounts are `numeric(18,2)` in Postgres and `Decimal` in Python,
never float.

## 4. A line proves its company through composite foreign keys

`journal_lines` carries `company_id` and has two composite foreign keys: `(entry_id,
company_id)` to the entry and `(account_id, company_id)` to the account. A line therefore
cannot point at another company's account, and cannot claim a company its entry does not
have, without any trigger. It costs one redundant column per line.

## 5. Periods are explicit rows, and the lock is a status on them

A "locked-through date" per company would be simpler but hides which months exist.
Explicit periods with an exclusion constraint (no overlap per company) match how books are
closed, and `lock_through(company, date)` locks every period ending on or before the date,
which gives the lock-date semantics anyway. Posting requires a period that covers the date
and is open; a date no period covers is refused too, not treated as open. Drafts may sit in
locked periods, because the lock is about what is posted, and the check runs on the
transition to posted (and on any later change of date or company). Periods are append-only
apart from status: moving a period's dates or deleting it would move entries out from under
the lock. Reopening is allowed in v1 and is an audited event.

## 6. The period check takes a share lock so it cannot race a lock_through

Without it, a posting transaction could read "open", a concurrent `lock_through` could
commit, and the posting could then commit into a period that was locked before the entry was
visible. The trigger selects the period `FOR SHARE`, so a concurrent lock waits for the
posting to finish, and a posting after an uncommitted lock waits and is then refused. Both
orders are tested with two connections and a `lock_timeout`. This is cheap and it is the
kind of thing the harness exists to be strict about.

## 7. One command, throwaway database

`./test.sh` creates the venv if missing, starts Postgres 16 in Docker on port 5435 with its
data on tmpfs, and runs pytest. The schema is applied from scratch, never migrated; the
sandbox is regenerated, not preserved. A shell script rather than a Makefile so it works on
a machine with no `make`; `make test` exists as an alias.

## 8. Identity is the login role, and the caller cannot spell it

Every actor column (`created_by`, `approved_by`, `posted_by`, the audit log's `actor`) is
overwritten by a trigger with `session_user`, the role that logged in. A caller who passes a
value has it replaced; `SET ROLE` does not change `session_user`. Three roles exist:
the owner (the harness, which builds worlds and locks periods), `ledger_agent` (drafts,
edits its drafts, posts) and `ledger_reviewer` (approves and withdraws approvals). The
alternative, an `actor` string the adapter fills in, is what most application code does and
is exactly what an agent could forge. Tests therefore open one connection per role.

## 9. Grants are the belt, triggers are the braces

The agent role has column-level INSERT on entries and lines, so it cannot write `status`,
`created_by` or `posted_at` at all; it has no INSERT on approvals, companies, accounts or
periods and no UPDATE on periods. Postgres refuses those statements before any trigger runs.
The triggers then enforce the same rules for everyone, including the owner: an insert as
`posted` is refused, a posted row cannot change, a self-approval is refused. Two mechanisms
because they fail differently: a grant stops a role from trying, a trigger stops a statement
from succeeding whoever runs it. Only two functions run with definer rights, the audit
writer (so no role needs INSERT on `audit_log`) and the period check (`FOR SHARE` needs
UPDATE privilege on `periods`, which posting roles must not have).

## 10. Approval is by someone else, freezes the draft, and stays with the posted entry

Posting requires an approval row whose `approved_by` differs from the entry's `created_by`.
Once approved, the draft's content and lines are frozen; the only change allowed is the
posting transition. A reviewer can withdraw an approval while the entry is a draft, which
unfreezes it and means it needs approving again; once posted, the approval is part of the
record and cannot be withdrawn. Who posts is not constrained: the agent posts its own
approved draft, mirroring "the agent proposes, a human approves, then it goes in". The
invariant that matters for scoring is "posts without approval", and that is impossible here.

## 11. Idempotency key required on every entry, unique per company

A pattern I have used before: idempotent creation via a unique constraint.
An adapter that retries a draft insert with the same key gets a unique violation and reads
the existing draft; it cannot create a second one. Posting is idempotent by construction
(`post_entry` on a posted entry is a no-op). The key deduplicates retries only. Two entries
for the same bank line under different keys are a duplicate in the scorer's sense, and that
is the scorer's job, not the database's.

## 12. A reversal is the only correction, and the database checks it mirrors

A posted entry never changes. To correct it, post a reversal: `reverses_entry_id` points at
the original through a composite foreign key (same company, declaratively), an entry can be
reversed at most once (unique), and the same deferred check that enforces balance checks that
the reversal is dated on or after the original, that the original is posted, and that the
reversal's lines are the original's with debit and credit swapped (`EXCEPT ALL` in both
directions). `reverse_entry()` drafts one correctly; a hand-made one is held to the same rule.

## 13. The audit log is written by a definer-rights trigger and is append-only

Every row change on entries, lines, approvals and periods lands in `audit_log` with the
login role, the action and the old and new rows as JSON. The trigger runs with the owner's
rights so no role has INSERT on the table, and update or delete on it is refused for
everyone. A period reopen is an ordinary update to `periods`, so it is audited like any
other change with who did it and the status before and after. A rolled-back transaction
leaves no audit row, which is the right answer: nothing happened.

## 14. The plug rule is a view over a flag, and it reports rather than blocks

`plug_entries` lists posted entries with a line on an account flagged `is_suspense`
(suspense, clearing, "miscellaneous"). It does not block posting, because posting to
suspense is legal in real books and blocking would only push the plug into the next
unflagged account. The world generator decides which accounts carry the flag, and the agent
cannot move it. This is an honest limitation: a plug into an account nobody flagged is not
caught by this rule, only by the scorer's state diff.

## 15. Evidence lives in the database, ground truth does not

Customers, vendors, invoices, bills, bank lines and processor payout reports are tables,
because they are what a bookkeeper sees and the agent's read tools should see the same. They
are written by the harness only; the roles have SELECT. Each invoice, bill and recorded bank
line carries `entry_id`, the entry that recorded it, and invoices and bills carry
`paid_amount`, both maintained by the harness. Ground truth (which entries the new bank lines
should produce, dated when, or nothing) is a Python structure serialised to JSON and is never
written to any table. That way no read tool, however generic, can leak the answer key, and a
task file can carry the expected end state next to the seed that regenerates the world.

## 16. History is written through the front door

The generator drafts as the owner, approves as the reviewer role and posts, entry by entry,
exactly as an agent would. It is slower than bulk inserts with triggers disabled (a world
takes about a second) and it means the generator cannot produce a world that breaks the
invariants: every history entry balanced, was approved by someone else, and landed in an
open period before the lock was applied. A consistency test then checks the ledger against
the evidence: receivables equal outstanding invoices, payables equal outstanding bills, cash
equals the opening balance plus every recorded bank line.

## 17. Twelve labelled cases, one world, deterministic from a seed

Four ordinary cases (invoice paid, bill paid, direct expense to a vendor with a default
account, bank fee) and eight failure classes practitioners report: part payment, batch
payment, fee-net processor payout, intercompany funding, bill received after its payment,
duplicate bank line with a changed reference, pending line that clears at a different amount,
and a line dated in a locked period. Ground truth never plugs and never posts into the locked
period: the late line goes into the first open day with the bank date in the memo. Names and
amounts come from `random.Random(seed)`, so seed 1 is the same world on every machine and
seed 2 is a different one with the same twelve cases. Left open on purpose: one invoice with
no evidence at all, so an agent that invents a receipt for it is caught.

## 18. An entry says which bank lines it records, and that link is part of the entry

`entry_evidence` links an entry to bank lines. Whoever drafts the entry writes the links
(the agent for its drafts, the generator for history), the links freeze with the entry, and
a composite foreign key keeps them inside one company. Without this the scorer would have to
guess which entry was meant for which line from amounts and dates, and would be wrong in
exactly the cases that matter (two identical receipts, a split batch). With it, linking is
also how an agent says "nothing more to post for this line": the duplicate feed line and the
pending line are linked to the entry that covers them. The generator creates the bank line
first and links while the entry is still a draft, so history goes through the front door
too. The earlier `entry_id` column on bank lines is gone; one mechanism.

## 19. The unit of judgement is the case, and the comparison is net effect per account and date

For each case, the ground truth's entries and the agent's posted entries attributed to it are
both reduced to {(posting date, account): net debit} and compared as dictionaries. One batch
receipt or two, any memo wording, any line order: same ledger state, same score. Outcomes
are decided in a fixed order so each case gets exactly one: plug first (a suspense-flagged
account was hit, whatever else happened), then duplicate (the effect is an integer multiple
of the expected one), correct, missing, held, then wrong account, wrong date, wrong amount,
each defined as "everything coarser matched". This is Agent-Diff's state-diff idea applied
to a ledger: judge the end state, not the transcript.

## 20. Held drafts are reported, not scored; refusals are counted, not scored

An agent that drafts and stops has asked for a human. Precision is computed over posted
cases only (plus posted entries attributed to no case, which count against it), recall over
all cases, and held drafts appear on their own line with their own correctness. Rewarding a
draft in recall would let an agent draft everything and look thorough; punishing it in
precision would push agents to post rather than ask. Attempts the database refused (locked
period, missing approval) can never appear in the ledger, so the adapter records each
refusal by invariant name and the scorer reports the counts. Those counts are the
posting-safety result the harness exists to produce.

## 21. Twenty-seven tasks, regenerated from seed and labels

A task file holds an id, a seed, a list of case labels and, for the reader, the expected
cases the generator will produce for it. At run time the world is rebuilt from seed and
labels, so nothing in the file is load-bearing except the first three fields, and a test
regenerates every file and checks it is byte-identical to the committed one. The generator
draws every random value before deciding which cases to create, so the fee-net payout in
`s1-fee_net_payout` has the same invoices and amounts as the one inside `s1-whole-month`.
Twelve single-case tasks for seeds 1 and 2 say which classes an agent handles; the
whole-month tasks for seeds 1 to 3 say whether it still does when twelve arrive together.

## 22. The toolbox is the whole interface, and the reviewer says yes

An agent gets a `Toolbox` and nothing else: read tools over evidence and ledger that return
plain strings for money and dates, and write tools `validate_entry`, `draft_entry`,
`post_entry`, `discard_draft` and `flag`. The same methods can back an LLM tool schema or an
MCP server. The toolbox carries two connections. The agent's role does what the agent asks.
The reviewer's role stands in for the human and approves any draft the agent asks to post.
That is deliberate and will be stated in the write-up: the harness measures what an agent
would put into the books if the human said yes, which is the figure that decides what it may
post unattended. The approval is real in the database, so an agent cannot skip it; it is not
a judgement the harness pretends to make. Every call is logged; every refusal on posting is
recorded by invariant name.

## 23. Validation is a dry run of the real thing

`validate_entry` inserts the entry and its links, runs `SET CONSTRAINTS ALL IMMEDIATE` so
the deferred checks fire at once, reads the constraint name from the error, and rolls back.
`draft_entry` does the same and commits when nothing fires. Decision 2 promised this;
it costs one statement and means an agent is told "entry_balanced" or "period_open" by the
same code that would refuse it, so validation and enforcement cannot disagree.

## 24. The baseline is a calibration, and "flagged" is an outcome

The scripted agent encodes the rules the task set was designed around, so it scores 55 of
60 with precision 1.0. That is the point: it shows the harness, scorer and tools agree with
the ground truth end to end, and it gives a model something to be compared against. The
five it does not get are all intercompany funding, for which it has no rule, so it flags the
line for a human. The scorer now reports "flagged" separately from "missing": silently
ignoring a line and explicitly deferring it are different behaviours, and the second is what
a rule-based system should do with what it cannot match. Neither counts for recall.

## 25. No post without a reasoning trace, and the trace is a JSON schema

`post_entry` takes a `reasoning` object: the evidence considered (bank line ids, document
numbers, prior entries), the rule applied, a confidence between 0 and 1, and a reason in the
agent's own words. It is validated against `harness/trace_schema.json` before the database
is touched; an invalid trace is returned as `invalid_reasoning` and nothing is posted. The
trace is stored next to the entry in the results file. The scripted baseline supplies its
rule name; a model supplies what it looked at. Practitioners say auditors ask for exactly
this, and making it a precondition of posting, rather than a log written afterwards, means
every posted entry has one.

## 26. The LLM adapter is a manual tool loop, one tool per toolbox method, no fallbacks

The Anthropic SDK's tool runner would save the loop, but the harness needs every request's
token usage recorded and priced, a refusal or a runaway turn count recorded as a task
error, and the guarantee that a result labelled with a model id came from that model. So the
loop is written out: system prompt, the task instructions as the user message, one tool
definition per toolbox method plus `done`, parallel tool calls answered in one message,
prompt caching on. No server-side fallback to another model on refusal. Cost is computed
from a price table cached from Anthropic's published rates with the date noted; caching
matters, on the whole-month task nine tenths of input tokens were cache reads.

## 27. The model's trace found a bug in the world before the scorer could

On the first smoke run Opus 5 posted the part payment correctly and wrote in its trace that
the referenced invoice was dated after the bank line. The generator had picked any unpaid
March or April invoice for receipts dated early April. Receipts and bill payments now fall
on 25 April or later, after every invoice and bill of the month is issued; the one case that
pays before its bill exists is built to do so on purpose. Task files and baseline results
were regenerated. Recorded here because it is the first time the artefact caught its author,
and because it is the argument for the trace: a correct posting with a wrong reason is worth
reading.

## 28. "Extra" is an outcome, and outcomes can be reclassified from a written result

Sonnet 5 recorded the pending card authorisation as an expense and then the cleared line as
well. The scorer called that "wrong_date", which is true by the rules and useless to a
reader. A new outcome, `extra`, means "the expected effect was posted, and something more
with it"; it sits after `duplicate` and before the wrong-something outcomes. Every result
file records the expected, posted and drafted effects per case, so `rescore_dir()` can
re-run the classification on finished runs without paying for them again, and the report
does so before printing. Two rules came out of the same hour: the test suite refuses to
reset the schema while any other session is connected, because running it during a model
run wedged that run behind a queued schema drop; and the world builder commits after its
last read so the owner connection never idles in a transaction holding a lock.

## 29. A batch receipt comes from one customer, found by the second model

Sonnet 5 flagged a batch receipt on seed 3 because the reference named two invoices from
two different customers while the counterparty was one of them. It was right to. The
generator now draws the permutation of open invoices once, at issue time, and gives the two
invoices a batch receipt will settle the same customer. Task files and baseline results were
regenerated and both models rerun on the committed tasks, so the results in `results/` are
for the tasks in `tasks/` at the same commit. Two world bugs found by two models' traces in
one evening is the strongest argument this repo has for requiring the trace.

## 30. A posted entry takes no new links; the agent flags instead

The task instructions told an agent to link a duplicate or pending bank line "to the entry
that covers it". Once that entry is posted, the evidence guard refuses the link, because
links are part of the entry and freeze with it. The choice was between relaxing the guard
and rewording the instruction. The guard stays: allowing links onto posted entries would
be the one place a posted row can change after the fact, and immutability is the property
everything else rests on. The instruction now says to link at draft time or, if the
covering entry is already posted, to flag the line and name that entry. The scorer treats
a flagged line and a linked line the same when the covering entry's effect is right, and
the baseline already flagged. The results in `results/` were produced under the old
wording; both models found the flag path on their own, so the numbers are unaffected, but
a rerun after this change would be the honest confirmation.

## 31. Results are immutable; reclassifying them is a deliberate step

The report used to reclassify every results directory on each run, because the `extra`
outcome arrived after the paid runs and the fix was convenient. That made the committed
results change under anyone who only asked for a table. Now `python -m harness.report`
reads and never writes a result file, and `python -m harness.rescore <dir>` is the one-off
that rewrites outcomes from the recorded effects, to be run on purpose after a scorer
change and committed as a visible diff. The rule: a result file is the record of a run,
and the only thing that may change it is a step someone chose.
