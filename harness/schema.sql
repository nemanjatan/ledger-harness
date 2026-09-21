-- Sandbox ledger schema.
--
-- Applied fresh by harness.db.reset_schema(); there are no migrations. Every invariant the
-- harness scores is enforced here, in the database, so that no adapter or agent can route
-- around it. Every rejection is raised as a check_violation (SQLSTATE 23514) carrying the
-- name of the invariant in the constraint field, so callers see one uniform contract:
--
--   line_is_debit_xor_credit     a line carries a debit or a credit, never both, never neither
--   entry_has_lines              an entry has at least two lines
--   entry_balanced               sum(debit) = sum(credit) for every entry, at every commit
--   entry_starts_as_draft        an entry is inserted as a draft; only post_entry() posts it
--   entry_approved               posting needs an approval row
--   approver_not_creator         the approver is not the actor who created the draft
--   approval_before_posting      approvals attach to drafts, not to posted entries
--   approval_immutable           an approval row never changes
--   approved_entry_immutable     an approved draft's content is frozen until it is posted
--   posted_entry_immutable       a posted entry, its lines and its approval never change
--   posting_date_in_period       a posted entry's date falls inside a defined period
--   period_open                  that period is not locked
--   period_dates_fixed           a period's company and dates never change once created
--   period_never_deleted         periods are never deleted
--   reversal_of_posted_entry     a reversal points at a posted entry
--   reversal_not_before_original a reversal is dated on or after the entry it reverses
--   reversal_mirrors_original    a reversal's lines are the original's with sides swapped
--   evidence_link_immutable      an evidence link is added or removed, never edited
--   audit_log_append_only        audit rows are never updated or deleted
--
-- Identity: the actor recorded on drafts, approvals, postings and audit rows is session_user,
-- the role that logged in. It cannot be supplied by the caller and SET ROLE does not change
-- it. Three roles exist: the owner (the harness), ledger_agent (drafts and posts, cannot
-- approve), ledger_reviewer (approves, cannot draft or post). Grants are the belt; the
-- triggers are the braces and apply to the owner too.

create extension if not exists btree_gist;

create table companies (
    id      serial primary key,
    name    text not null unique
);

create table accounts (
    id          serial primary key,
    company_id  integer not null references companies (id),
    code        text not null,
    name        text not null,
    kind        text not null check (kind in ('asset', 'liability', 'equity', 'revenue', 'expense')),
    is_suspense boolean not null default false,     -- suspense, clearing, "miscellaneous": plug targets
    unique (company_id, code),
    unique (id, company_id)     -- lets journal_lines prove the account is the entry's company's
);

-- Accounting periods. A company's periods never overlap. Posting needs an open period that
-- covers the posting date; lock_through() closes every period ending on or before a date.
create table periods (
    id          serial primary key,
    company_id  integer not null references companies (id),
    starts_on   date not null,
    ends_on     date not null,
    status      text not null default 'open' check (status in ('open', 'locked')),
    check (ends_on >= starts_on),
    exclude using gist (company_id with =, daterange(starts_on, ends_on, '[]') with &&)
);

create table journal_entries (
    id                  bigserial primary key,
    company_id          integer not null references companies (id),
    posting_date        date not null,
    memo                text not null default '',
    idempotency_key     text not null,
    status              text not null default 'draft' check (status in ('draft', 'posted')),
    created_by          text not null default session_user,
    created_at          timestamptz not null default now(),
    posted_by           text,
    posted_at           timestamptz,
    reverses_entry_id   bigint,
    check ((status = 'posted') = (posted_at is not null)),
    check ((status = 'posted') = (posted_by is not null)),
    check (reverses_entry_id <> id),
    unique (company_id, idempotency_key),   -- retrying a draft insert cannot create a second draft
    unique (reverses_entry_id),             -- an entry is reversed at most once
    unique (id, company_id),
    foreign key (reverses_entry_id, company_id) references journal_entries (id, company_id)
);

-- Debit and credit are separate non-negative columns; exactly one of them is zero.
create table journal_lines (
    id          bigserial primary key,
    entry_id    bigint not null,
    company_id  integer not null,
    account_id  integer not null,
    debit       numeric(18, 2) not null default 0 check (debit >= 0),
    credit      numeric(18, 2) not null default 0 check (credit >= 0),
    description text not null default '',
    constraint line_is_debit_xor_credit check ((debit = 0) <> (credit = 0)),
    foreign key (entry_id, company_id) references journal_entries (id, company_id),
    foreign key (account_id, company_id) references accounts (id, company_id)
);
create index on journal_lines (entry_id);

-- One approval per entry, by someone other than the drafter. Deleting an approval withdraws
-- it, which is allowed while the entry is a draft.
create table approvals (
    id          bigserial primary key,
    entry_id    bigint not null unique references journal_entries (id),
    approved_by text not null default session_user,
    approved_at timestamptz not null default now(),
    note        text not null default ''
);

-- Append-only record of every row change on the tables above, written by a definer-rights
-- trigger so no role needs, or has, insert rights on it.
create table audit_log (
    id          bigserial primary key,
    at          timestamptz not null default now(),
    actor       text not null,
    table_name  text not null,
    row_id      bigint not null,
    action      text not null check (action in ('INSERT', 'UPDATE', 'DELETE')),
    old_row     jsonb,
    new_row     jsonb
);
create index on audit_log (table_name, row_id);

-- A posted entry that touches a suspense-flagged account. The database does not block these:
-- posting to suspense is legal in real books. The scorer reads this view.
create view plug_entries as
select e.id as entry_id,
       e.company_id,
       e.posting_date,
       e.posted_by,
       sum(l.debit + l.credit) as suspense_amount
  from journal_entries e
  join journal_lines l on l.entry_id = e.id
  join accounts a on a.id = l.account_id
 where e.status = 'posted'
   and a.is_suspense
 group by e.id;

---------------------------------------------------------------------------------------------
-- Evidence. What a bookkeeper (or agent) sees besides the ledger: who the company deals with,
-- what it invoiced, what it was billed, what the bank says. Written by the harness only. The
-- entry_id links are maintained by the harness and say "this document is recorded by that
-- entry"; ground truth for unrecorded evidence lives outside the database on purpose.
---------------------------------------------------------------------------------------------
create table customers (
    id          serial primary key,
    company_id  integer not null references companies (id),
    name        text not null,
    unique (company_id, name)
);

create table vendors (
    id                  serial primary key,
    company_id          integer not null references companies (id),
    name                text not null,
    expense_account_id  integer references accounts (id),   -- default account, as in most GLs
    unique (company_id, name)
);

create table invoices (
    id          serial primary key,
    company_id  integer not null references companies (id),
    customer_id integer not null references customers (id),
    number      text not null,
    issued_on   date not null,
    due_on      date not null,
    amount      numeric(18, 2) not null check (amount > 0),
    paid_amount numeric(18, 2) not null default 0 check (paid_amount >= 0 and paid_amount <= amount),
    entry_id    bigint references journal_entries (id),
    unique (company_id, number)
);

create table bills (
    id          serial primary key,
    company_id  integer not null references companies (id),
    vendor_id   integer not null references vendors (id),
    number      text not null,
    received_on date not null,
    due_on      date not null,
    amount      numeric(18, 2) not null check (amount > 0),
    paid_amount numeric(18, 2) not null default 0 check (paid_amount >= 0 and paid_amount <= amount),
    entry_id    bigint references journal_entries (id),
    unique (company_id, number)
);

-- Signed amount: positive is money in, negative is money out.
create table bank_lines (
    id           serial primary key,
    company_id   integer not null references companies (id),
    booked_on    date not null,
    amount       numeric(18, 2) not null check (amount <> 0),
    reference    text not null default '',
    counterparty text not null default '',
    status       text not null default 'cleared' check (status in ('pending', 'cleared')),
    unique (id, company_id)
);

-- A card processor's payout report: which invoices it collected, what it kept, what it paid.
create table processor_payouts (
    id          serial primary key,
    company_id  integer not null references companies (id),
    processor   text not null,
    paid_on     date not null,
    gross       numeric(18, 2) not null check (gross > 0),
    fees        numeric(18, 2) not null check (fees >= 0),
    net         numeric(18, 2) not null,
    invoice_ids integer[] not null,
    check (net = gross - fees)
);

-- "This entry records these bank lines." Written by whoever drafts the entry (the agent for
-- its drafts, the harness for history), frozen with the entry. The scorer groups an agent's
-- entries by the bank lines they claim. Linking a line to an entry that covers it is also how
-- an agent says "this line is a duplicate of that one, nothing more to post".
create table entry_evidence (
    entry_id     bigint not null,
    company_id   integer not null,
    bank_line_id integer not null,
    primary key (entry_id, bank_line_id),
    foreign key (entry_id, company_id) references journal_entries (id, company_id),
    foreign key (bank_line_id, company_id) references bank_lines (id, company_id)
);
create index on entry_evidence (bank_line_id);

---------------------------------------------------------------------------------------------
-- Invariant: every entry balances, has at least two lines, and if it is a reversal it mirrors
-- a posted original. Checked once per touched row at commit time (deferred constraint
-- trigger), so an entry and its lines are inserted in one transaction and the transaction as
-- a whole is accepted or rejected. Drafts included: one rule, no status branch.
---------------------------------------------------------------------------------------------
create function assert_entry_valid(p_entry_id bigint) returns void
language plpgsql as $$
declare
    e            journal_entries%rowtype;
    original     journal_entries%rowtype;
    n_lines      integer;
    total_debit  numeric;
    total_credit numeric;
    n_mismatched integer;
begin
    select * into e from journal_entries where id = p_entry_id;
    if not found then
        return;     -- the entry was deleted in this transaction along with its lines
    end if;
    select count(*), coalesce(sum(debit), 0), coalesce(sum(credit), 0)
      into n_lines, total_debit, total_credit
      from journal_lines
     where entry_id = p_entry_id;
    if n_lines < 2 then
        raise exception 'journal entry % has % line(s); an entry needs at least two',
            p_entry_id, n_lines
            using errcode = 'check_violation', constraint = 'entry_has_lines';
    end if;
    if total_debit <> total_credit then
        raise exception 'journal entry % does not balance: debits % credits %',
            p_entry_id, total_debit, total_credit
            using errcode = 'check_violation', constraint = 'entry_balanced';
    end if;
    if e.reverses_entry_id is null then
        return;
    end if;
    select * into original from journal_entries where id = e.reverses_entry_id;
    if original.status <> 'posted' then
        raise exception 'journal entry % reverses entry %, which is not posted',
            p_entry_id, e.reverses_entry_id
            using errcode = 'check_violation', constraint = 'reversal_of_posted_entry';
    end if;
    if e.posting_date < original.posting_date then
        raise exception 'journal entry % is dated % but reverses entry % dated %',
            p_entry_id, e.posting_date, original.id, original.posting_date
            using errcode = 'check_violation', constraint = 'reversal_not_before_original';
    end if;
    select count(*) into n_mismatched from (
        (select account_id, debit, credit from journal_lines where entry_id = original.id
         except all
         select account_id, credit, debit from journal_lines where entry_id = p_entry_id)
        union all
        (select account_id, credit, debit from journal_lines where entry_id = p_entry_id
         except all
         select account_id, debit, credit from journal_lines where entry_id = original.id)
    ) d;
    if n_mismatched > 0 then
        raise exception 'journal entry % does not mirror the lines of entry %',
            p_entry_id, original.id
            using errcode = 'check_violation', constraint = 'reversal_mirrors_original';
    end if;
end $$;

create function trg_lines_valid() returns trigger
language plpgsql as $$
begin
    if tg_op in ('INSERT', 'UPDATE') then
        perform assert_entry_valid(new.entry_id);
    end if;
    if tg_op in ('DELETE', 'UPDATE') then
        perform assert_entry_valid(old.entry_id);
    end if;
    return null;
end $$;

create function trg_entry_valid() returns trigger
language plpgsql as $$
begin
    perform assert_entry_valid(new.id);
    return null;
end $$;

create constraint trigger entry_valid_on_lines
    after insert or update or delete on journal_lines
    deferrable initially deferred
    for each row execute function trg_lines_valid();

create constraint trigger entry_valid_on_entry
    after insert or update on journal_entries
    deferrable initially deferred
    for each row execute function trg_entry_valid();

---------------------------------------------------------------------------------------------
-- Invariant: a posted entry's date falls in an open period. The period row is locked FOR
-- SHARE so a concurrent lock_through() waits for this posting to commit or roll back, instead
-- of both succeeding with the entry landing in a period that is locked by the time it is
-- visible. Definer rights because row locking needs UPDATE privilege, which the posting roles
-- do not and should not have on periods.
---------------------------------------------------------------------------------------------
create function assert_period_open(p_company_id integer, p_date date) returns void
language plpgsql security definer set search_path = public as $$
declare
    p periods%rowtype;
begin
    select * into p
      from periods
     where company_id = p_company_id
       and p_date between starts_on and ends_on
       for share;
    if not found then
        raise exception 'no accounting period covers % for company %', p_date, p_company_id
            using errcode = 'check_violation', constraint = 'posting_date_in_period';
    end if;
    if p.status = 'locked' then
        raise exception 'period % to % is locked; cannot post on %', p.starts_on, p.ends_on, p_date
            using errcode = 'check_violation', constraint = 'period_open';
    end if;
end $$;

---------------------------------------------------------------------------------------------
-- Guard on journal_entries: drafts only on insert; identity forced from session_user; posted
-- rows frozen; approved drafts frozen except for the posting transition itself; posting
-- needs an approval and an open period.
---------------------------------------------------------------------------------------------
create function trg_entries_guard() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if old.status = 'posted' then
            raise exception 'journal entry % is posted and cannot be deleted', old.id
                using errcode = 'check_violation', constraint = 'posted_entry_immutable';
        end if;
        return old;
    end if;

    if tg_op = 'INSERT' then
        if new.status <> 'draft' then
            raise exception 'journal entries are inserted as drafts and posted with post_entry()'
                using errcode = 'check_violation', constraint = 'entry_starts_as_draft';
        end if;
        new.created_by := session_user;
        new.created_at := now();
        new.posted_by := null;
        new.posted_at := null;
        return new;
    end if;

    -- UPDATE
    if old.status = 'posted' then
        raise exception 'journal entry % is posted and cannot change', old.id
            using errcode = 'check_violation', constraint = 'posted_entry_immutable';
    end if;
    new.created_by := old.created_by;
    new.created_at := old.created_at;
    if (new.company_id, new.posting_date, new.memo, new.idempotency_key, new.reverses_entry_id)
       is distinct from
       (old.company_id, old.posting_date, old.memo, old.idempotency_key, old.reverses_entry_id)
       and exists (select 1 from approvals where entry_id = old.id) then
        raise exception 'journal entry % is approved; withdraw the approval to change it', old.id
            using errcode = 'check_violation', constraint = 'approved_entry_immutable';
    end if;
    if new.status = 'posted' then
        if not exists (select 1 from approvals where entry_id = old.id) then
            raise exception 'journal entry % has no approval and cannot be posted', old.id
                using errcode = 'check_violation', constraint = 'entry_approved';
        end if;
        perform assert_period_open(new.company_id, new.posting_date);
        new.posted_by := session_user;
        new.posted_at := now();
    else
        new.posted_by := null;
        new.posted_at := null;
    end if;
    return new;
end $$;

create trigger entries_guard
    before insert or update or delete on journal_entries
    for each row execute function trg_entries_guard();

---------------------------------------------------------------------------------------------
-- Guard on journal_lines: no change to the lines of a posted or approved entry.
---------------------------------------------------------------------------------------------
create function assert_entry_mutable(p_entry_id bigint) returns void
language plpgsql as $$
declare
    e_status text;
begin
    select status into e_status from journal_entries where id = p_entry_id;
    if e_status = 'posted' then
        raise exception 'journal entry % is posted; its lines cannot change', p_entry_id
            using errcode = 'check_violation', constraint = 'posted_entry_immutable';
    end if;
    if exists (select 1 from approvals where entry_id = p_entry_id) then
        raise exception 'journal entry % is approved; withdraw the approval to change its lines', p_entry_id
            using errcode = 'check_violation', constraint = 'approved_entry_immutable';
    end if;
end $$;

create function trg_lines_guard() returns trigger
language plpgsql as $$
begin
    if tg_op in ('INSERT', 'UPDATE') then
        perform assert_entry_mutable(new.entry_id);
    end if;
    if tg_op in ('DELETE', 'UPDATE') then
        perform assert_entry_mutable(old.entry_id);
    end if;
    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end $$;

create trigger lines_guard
    before insert or update or delete on journal_lines
    for each row execute function trg_lines_guard();

---------------------------------------------------------------------------------------------
-- Guard on approvals: identity forced; drafts only; not by the drafter; never edited; kept
-- once the entry is posted.
---------------------------------------------------------------------------------------------
create function trg_approvals_guard() returns trigger
language plpgsql as $$
declare
    e journal_entries%rowtype;
begin
    if tg_op = 'UPDATE' then
        raise exception 'approval % cannot change; withdraw it and approve again', old.id
            using errcode = 'check_violation', constraint = 'approval_immutable';
    end if;
    if tg_op = 'DELETE' then
        select * into e from journal_entries where id = old.entry_id;
        if e.status = 'posted' then
            raise exception 'journal entry % is posted; its approval is part of the record', e.id
                using errcode = 'check_violation', constraint = 'posted_entry_immutable';
        end if;
        return old;
    end if;
    select * into e from journal_entries where id = new.entry_id;
    if found and e.status <> 'draft' then
        raise exception 'journal entry % is already posted and cannot be approved', e.id
            using errcode = 'check_violation', constraint = 'approval_before_posting';
    end if;
    new.approved_by := session_user;
    new.approved_at := now();
    if found and e.created_by = new.approved_by then
        raise exception '% drafted journal entry % and cannot approve it', new.approved_by, e.id
            using errcode = 'check_violation', constraint = 'approver_not_creator';
    end if;
    return new;
end $$;

create trigger approvals_guard
    before insert or update or delete on approvals
    for each row execute function trg_approvals_guard();

---------------------------------------------------------------------------------------------
-- Guard on entry_evidence: links are part of the entry and freeze with it.
---------------------------------------------------------------------------------------------
create function trg_evidence_guard() returns trigger
language plpgsql as $$
begin
    if tg_op = 'UPDATE' then
        raise exception 'evidence links are added or removed, never edited'
            using errcode = 'check_violation', constraint = 'evidence_link_immutable';
    end if;
    if tg_op = 'INSERT' then
        perform assert_entry_mutable(new.entry_id);
        return new;
    end if;
    perform assert_entry_mutable(old.entry_id);
    return old;
end $$;

create trigger evidence_guard
    before insert or update or delete on entry_evidence
    for each row execute function trg_evidence_guard();

---------------------------------------------------------------------------------------------
-- Periods are append-only apart from their status. Moving a period's dates or deleting it
-- would silently move posted entries out from under the lock. Reopening is allowed and lands
-- in the audit log like any other change.
---------------------------------------------------------------------------------------------
create function trg_periods_append_only() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'period % is never deleted', old.id
            using errcode = 'check_violation', constraint = 'period_never_deleted';
    end if;
    if (old.company_id, old.starts_on, old.ends_on)
       is distinct from (new.company_id, new.starts_on, new.ends_on) then
        raise exception 'period % dates cannot change', old.id
            using errcode = 'check_violation', constraint = 'period_dates_fixed';
    end if;
    return new;
end $$;

create trigger periods_append_only
    before update or delete on periods
    for each row execute function trg_periods_append_only();

---------------------------------------------------------------------------------------------
-- Audit log. Definer rights so the writing role never needs insert on audit_log.
---------------------------------------------------------------------------------------------
create function trg_audit() returns trigger
language plpgsql security definer set search_path = public as $$
begin
    insert into audit_log (actor, table_name, row_id, action, old_row, new_row)
    values (session_user, tg_table_name,
            coalesce((to_jsonb(new) ->> 'id')::bigint, (to_jsonb(old) ->> 'id')::bigint),
            tg_op, to_jsonb(old), to_jsonb(new));
    return null;
end $$;

create function trg_audit_evidence() returns trigger
language plpgsql security definer set search_path = public as $$
begin
    insert into audit_log (actor, table_name, row_id, action, old_row, new_row)
    values (session_user, tg_table_name, coalesce(new.entry_id, old.entry_id), tg_op, to_jsonb(old), to_jsonb(new));
    return null;
end $$;

create trigger audit after insert or update or delete on journal_entries for each row execute function trg_audit();
create trigger audit after insert or update or delete on journal_lines   for each row execute function trg_audit();
create trigger audit after insert or update or delete on approvals       for each row execute function trg_audit();
create trigger audit after insert or update or delete on periods         for each row execute function trg_audit();
create trigger audit after insert or delete on entry_evidence for each row execute function trg_audit_evidence();

create function trg_audit_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_log row % is never changed', old.id
        using errcode = 'check_violation', constraint = 'audit_log_append_only';
end $$;

create trigger audit_log_append_only
    before update or delete on audit_log
    for each row execute function trg_audit_append_only();

---------------------------------------------------------------------------------------------
-- Write path helpers. Plain invoker-rights functions: they hold no privilege of their own,
-- the caller's grants and the triggers above decide.
---------------------------------------------------------------------------------------------

-- Post a draft. Idempotent: posting an already posted entry is a no-op.
create function post_entry(p_entry_id bigint) returns void
language plpgsql as $$
begin
    if not exists (select 1 from journal_entries where id = p_entry_id) then
        raise exception 'journal entry % does not exist', p_entry_id using errcode = 'no_data_found';
    end if;
    update journal_entries set status = 'posted' where id = p_entry_id and status = 'draft';
end $$;

-- Draft a reversal of a posted entry: same lines, sides swapped. Returns the draft's id.
create function reverse_entry(p_entry_id bigint, p_posting_date date, p_idempotency_key text, p_memo text default '')
returns bigint
language plpgsql as $$
declare
    original journal_entries%rowtype;
    new_id   bigint;
begin
    select * into original from journal_entries where id = p_entry_id;
    if not found then
        raise exception 'journal entry % does not exist', p_entry_id using errcode = 'no_data_found';
    end if;
    insert into journal_entries (company_id, posting_date, memo, idempotency_key, reverses_entry_id)
    values (original.company_id, p_posting_date, p_memo, p_idempotency_key, original.id)
    returning id into new_id;
    insert into journal_lines (entry_id, company_id, account_id, debit, credit, description)
    select new_id, company_id, account_id, credit, debit, description
      from journal_lines where entry_id = original.id;
    return new_id;
end $$;

-- Lock every period of the company that ends on or before the date. A period that straddles
-- the date stays open. Returns the number of periods newly locked.
create function lock_through(p_company_id integer, p_through date) returns integer
language sql as $$
    with locked as (
        update periods
           set status = 'locked'
         where company_id = p_company_id
           and ends_on <= p_through
           and status = 'open'
        returning 1
    )
    select count(*)::integer from locked;
$$;

---------------------------------------------------------------------------------------------
-- Roles and grants. Login roles with throwaway passwords: this is a sandbox on tmpfs.
-- Column-level insert grants mean the agent cannot supply status, actor or timestamp columns.
---------------------------------------------------------------------------------------------
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ledger_agent') then
        create role ledger_agent login password 'ledger_agent';
    end if;
    if not exists (select 1 from pg_roles where rolname = 'ledger_reviewer') then
        create role ledger_reviewer login password 'ledger_reviewer';
    end if;
end $$;

grant usage on schema public to ledger_agent, ledger_reviewer;
grant select on all tables in schema public to ledger_agent, ledger_reviewer;
grant usage on all sequences in schema public to ledger_agent, ledger_reviewer;

grant insert (company_id, posting_date, memo, idempotency_key, reverses_entry_id) on journal_entries to ledger_agent;
grant update (posting_date, memo, status) on journal_entries to ledger_agent;
grant delete on journal_entries to ledger_agent;
grant insert (entry_id, company_id, account_id, debit, credit, description) on journal_lines to ledger_agent;
grant update (account_id, debit, credit, description) on journal_lines to ledger_agent;
grant delete on journal_lines to ledger_agent;

grant insert, delete on entry_evidence to ledger_agent;

grant insert (entry_id, note) on approvals to ledger_reviewer;
grant delete on approvals to ledger_reviewer;
