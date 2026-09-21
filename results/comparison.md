| agent | cases | correct | precision | recall | flagged | held | extra | wrong | dup | plug | refusals | errors | cost USD | time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 60 | 55 | 1.000 | 0.917 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | 0m00s |
| claude-opus-5-high | 60 | 56 | 1.000 | 0.933 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 4.1808 | 21m42s |
| claude-sonnet-5-high | 60 | 56 | 0.933 | 0.933 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 1.4292 | 15m43s |
| claude-sonnet-5-high-repeat | 60 | 56 | 0.933 | 0.933 | 0 | 0 | 4 | 0 | 0 | 0 | 0 | 0 | 1.3972 | 15m16s |

Correct per case label (out of the times it appeared):

| case | baseline | claude-opus-5-high | claude-sonnet-5-high | claude-sonnet-5-high-repeat |
|---|---|---|---|---|
| bank_fee | 5/5 | 5/5 | 5/5 | 5/5 |
| batch_payment | 5/5 | 5/5 | 5/5 | 5/5 |
| bill_after_bank_line | 5/5 | 1/5 | 5/5 | 5/5 |
| bill_paid_exactly | 5/5 | 5/5 | 5/5 | 5/5 |
| direct_expense | 5/5 | 5/5 | 5/5 | 5/5 |
| duplicate_bank_line | 5/5 | 5/5 | 5/5 | 5/5 |
| fee_net_payout | 5/5 | 5/5 | 5/5 | 5/5 |
| intercompany_transfer | 0/5 | 5/5 | 5/5 | 5/5 |
| invoice_paid_exactly | 5/5 | 5/5 | 5/5 | 5/5 |
| late_line_in_locked_period | 5/5 | 5/5 | 5/5 | 5/5 |
| part_payment | 5/5 | 5/5 | 5/5 | 5/5 |
| pending_then_cleared_differently | 5/5 | 5/5 | 1/5 | 1/5 |
