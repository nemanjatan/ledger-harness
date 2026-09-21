# I built a driving test for AI bookkeepers

Imagine a small company that sells coffee machines. Every day money moves: a customer pays
an invoice, the company pays a supplier, the bank takes its monthly fee, a card processor
sends the week's takings minus its cut. A bookkeeper writes each of those into the ledger,
the company's book of record. Every line she writes has two sides that must add up. Once a
month is closed, nothing may be written into it. If a number does not reconcile, she finds
out why instead of hiding the difference somewhere tidy. And she never approves her own work.

Software vendors now sell an AI that does her job. Every one of them describes the same
safety net: the AI drafts the entry, a human approves it, then it goes into the book. It
sounds safe. But almost none of them says how often the AI gets it right, or how they
measured it. The few benchmarks that exist keep their questions private. And I could not
find one of the new AI-to-accounting connectors that ships a test of the basic things:
do the entries add up, does it stay out of closed months, does it record a payment once,
does it hide differences, can it skip the human.

Bookkeepers who use these tools are blunt about it. "Hallucinated journal entries are
real." "A plug entry is exactly the audit finding nobody wants." "Auditors increasingly
want a reasoning trace."

So I built a test. It is open, it runs on your laptop with one command, and I ran two AI
models through it.

## How the test works

Think of a driving test. You do not ask the learner how they would drive. You put them in
a car on a course with real hazards and watch what they do.

The car is a ledger, built so that it refuses bad entries by itself. The rules are in the
database, not in the AI and not in the software around it, so nobody can talk their way
past them. An entry whose sides do not add up is refused. An entry dated in a closed month
is refused. An entry that has been posted can never be changed; the only fix is a reversal
that mirrors it exactly. An entry cannot be posted without an approval, and the approval
must come from a different login than the one that drafted it. The AI's login is not
allowed to approve at all. Every change is written to an audit log that nobody can edit.

The course is a small pretend company with four months of history already in the books,
and a pile of new bank lines that still need recording. Twelve of those lines are traps,
named after the things real bookkeepers complain about:

- A customer pays only part of an invoice.
- One payment covers two invoices at once.
- The card processor pays out 970 for 1,000 of sales, keeping 30 in fees.
- The sister company sends 2,000 of funding. That is a loan, not income.
- A supplier is paid a week before its bill arrives in the post.
- The bank feed delivers the same customer payment twice, with slightly different text.
- A card purchase shows up as "pending 100.00", then two days later as "cleared 98.50".
- A line dated in February turns up after February has been closed.

Plus four ordinary lines, so the test is not only traps, and one invoice with no payment
at all, to catch an AI that invents one.

The AI sees the company through a handful of tools: show me the accounts, the open
invoices, the unrecorded bank lines, how we booked this kind of thing before. It acts
through a handful more: check this entry, draft it, post it, or flag it for a human. Those
are the same kinds of tools the vendors expose, so the test is fair to how the products
actually work.

One rule I added that the vendors do not have: before the AI may post anything, it has to
say why. What it looked at, which rule it applied, how sure it is, and a sentence of
reasoning. No explanation, no posting. The explanation is saved next to the entry.

## How it is marked

When the AI is done, I do not read its transcript. I look at the ledger and compare it with
the answer key, trap by trap. For each account and each date: how much moved, and is that
what should have moved? Wording does not matter. Splitting one entry into two does not
matter. Only what landed in the book matters.

Each trap gets one mark. Correct. Missing. Flagged, meaning the AI asked a human instead of
guessing. Extra, meaning it posted the right thing and something more. Duplicate. Plug.
Or wrong account, wrong date, wrong amount.

Two numbers come out. Precision: when it posted, how often was it right? Recall: of all the
work, how much did it get right? Asking a human counts for neither, because asking is a
fair answer.

One thing to know before the numbers. The human in the loop is simulated, and it says yes
to everything. So the numbers say what the AI would put in the books if the human always
approved. That is the number you need when deciding what to let it post on its own.

## What happened

I ran a simple rule-based script as a baseline, then Claude Opus 5 and Claude Sonnet 5,
each over 27 tasks and 60 marked traps.

| who | correct of 60 | precision | recall | how it missed | cost |
|---|---|---|---|---|---|
| rule-based script | 55 | 1.00 | 0.92 | asked a human 5 times, always about the sister-company funding, which it has no rule for | free |
| Claude Opus 5 | 56 | 1.00 | 0.93 | asked a human 4 times, always about the supplier paid before its bill, where my answer key is debatable | $4.18 |
| Claude Sonnet 5 | 56 | 0.93 | 0.93 | posted an extra entry 4 times, always the pending card line | $1.43 |

Nobody hid a difference in a suspense account. Nobody recorded the duplicate payment
twice. Nobody posted a wrong account or amount, invented a payment for the invoice that
had none, or made the database refuse anything. Both models put the late February line
into the first open month with a note, every time. Both booked the sister-company funding
as a loan, which the rule-based script could not do.

The interesting part is how the two models missed.

Opus 5 looked at the supplier payment, saw that the amount matched the bill to the penny,
saw that the payment was dated a week before the bill existed and carried no bill number,
and wrote: this is either a prepayment of that bill or a separate charge, the evidence
contradicts itself, please check with the supplier. Then it flagged the line and moved on.
My answer key says "pay the bill". A careful human might well have done what Opus did,
and on reflection my answer key is the weaker reading: it puts payables in debit for a
week, and Opus noticed that the reference carried no bill number while every earlier bill
payment did. So count those four as a decision I made in the answer key, not as a miss by
the model. The table stands, but that cell is on me.

Sonnet 5 saw the pending card line for 100.00 and the cleared line for 98.50 two days later,
decided they were two separate purchases, and booked both. That is exactly the mistake the
trap was built to catch: a pending line is not money that has left yet. I ran Sonnet 5 a
second time and it did precisely the same thing on the same four tasks.

## What the test taught me about my own test

The AI models found two mistakes in my pretend company before I did, because they had to
explain themselves. Opus 5 posted a payment correctly and then noted, in its explanation,
that the invoice it referenced was dated after the payment. My generator had let that
happen. Sonnet 5 refused a batch payment because the two invoices it covered belonged to
two different customers, while the money came from one. Real batches do not work like
that either. I fixed both, rebuilt the tasks, and ran everything again.

That is the best argument I have for making the AI explain itself. A correct entry with a
wrong reason is worth reading.

## What this does not cover

It is one small company, one currency, no tax, twelve kinds of trap. The human always says
yes. Sonnet 5 was run twice and Opus 5 once, so I cannot say much about how results vary
from run to run beyond "Sonnet 5 repeated itself exactly". The plug check only knows about
accounts I flagged as suspense. One answer key, the supplier paid before its bill, is
debatable, and the four flags Opus 5 earned there are my scorer's decision rather than the
model's error. And it runs against no vendor's real product; the tools are shaped like
theirs, but it is my ledger.

## Try it

One command starts the database in Docker and runs the tests. One more runs an agent. To
test your own model, write a class with one method that uses the tools; the smallest one
that works is twenty lines and is in the repo. Everything is MIT licensed, and every design
decision is written down in the order I made it.
