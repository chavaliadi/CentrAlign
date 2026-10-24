# Autonomous AI Task Worker

A prototype for CentrAlign AI's **AI Engineering Intern** problem statement.

It takes a natural-language company request, works out what "done" would mean,
operates two real web systems through a browser to get there, recovers when the
environment misbehaves, asks a human before it changes anything, and then
**refuses to accept its own word that it succeeded** — an independent verifier
re-reads the company's data and can send the agent back to work.

The deliberate choice behind the whole submission: *a narrow system that
genuinely works beats a broad one that simulates it.* So the scope is one
domain — accounts payable at a fictional company — and nothing in the path from
request to verified outcome is mocked.

---

## The demo in one command

```bash
git clone <this repo> && cd centralign-task-worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

export GROQ_API_KEY=gsk_...                # or ANTHROPIC_API_KEY / OPENAI_API_KEY

python run.py --all --auto-approve --start-env
```

The provider is inferred from whichever key is set, so there is nothing else to
configure. To be explicit, or to compare:

```bash
python run.py --list-models                        # what this provider offers
python run.py --all --provider groq --auto-approve --start-env
python run.py --all --provider anthropic --model claude-sonnet-5-5 --auto-approve --start-env
```

That boots the mock company environment, then runs **three different tasks
against the same unchanged agent**. Each produces an evidence folder under
`runs/`.

To watch the approval gate work, drop `--auto-approve` — the run pauses at the
terminal before every write. To watch the browser, add `--headed`.
To review the generated definition of done before execution, add
`--review-contract`; declining it ends the run as blocked without executing a
tool.

```bash
python run.py --task invoice_entry --start-env            # one task, approvals prompted
python run.py --all --chaos off --auto-approve --start-env # clean baseline, no failure injection
```

### Tests, which need no API key

```bash
python -m uvicorn acmeops.app:app --port 8099 &      # environment for the live suites
python -m tests.test_tools                # 34 checks: browser tools against the live app
python -m tests.test_loop_offline         # 28 checks: the control loop, model stubbed out
python -m tests.test_llm_provider         # 36 checks: provider abstraction, no network
python -m tests.test_paraphrase_offline   # 23 checks: outcome contracts across request wordings
python -m tests.test_end_to_end_scripted  # 18 checks: whole pipeline, model stubbed out
```

139 checks, none of which need an API key.

### What this proves, precisely

`test_end_to_end_scripted` runs the real controller against the real AcmeOps
over a real Chromium — real login, real PDF download and extraction, a real
write through the form, the real approval gate, the real state read that
verification uses — with **only the model's choice of next action** replaced by
a script. It asserts the write landed at the PDF's total rather than the list
view's subtotal, that no duplicate row appeared, that both form submissions
passed through the approval gate, and that the evidence folder contains the
report, trace, screenshots and the retained invoice.

So the honest claim is: every moving part except the model's judgement is
proven end to end. `test_loop_offline` then covers the judgement-adjacent
behaviour that is the *controller's* responsibility rather than the model's —
retrying transients, catching a wrong value at verification and re-planning,
refusing a denied write, flagging a duplicate, ending a hopeless run — also
with the model stubbed out. What remains untested without a key is only how
well a given model decides, which is exactly what the `--provider` switch
exists to let you measure.

---

## The three tasks

All three run on the same agent, same tools, same prompts. Nothing is switched
on per task; the task files contain only the request and notes about the
environment.

| Task | Request | What it exercises |
| --- | --- | --- |
| `invoice_entry` | "Find the latest invoice from Northwind Traders, get the amount and the due date, enter it into our payables system, and tell me once it's done." | Multi-system work, document reading, an approved write, verification of a created record |
| `outstanding_report` | "How much do we owe Globex Corporation across all their unpaid invoices? Give me the figure and which invoices it covers." | Read-only aggregation over several documents. No write, so no approval — and verification has to check an *answer* rather than a state change |
| `correct_due_date` | "Someone says the due date on one of our Northwind payables doesn't match the vendor's invoice. Check our payables against the vendor portal and fix whatever is wrong." | The request names neither the record nor the right value. The agent has to find the discrepancy itself, then correct in place without duplicating |

Generalisation is the criterion most prototypes fail, so it is the one made
cheapest to check here: `python run.py --all` and read the summary table.

---

## The environment is real, and hostile on purpose

`acmeops/` is a FastAPI + SQLite company environment: real HTTP, real HTML,
real session cookies, real PDFs, real persisted writes.

* **`/portal`** — the vendor invoice portal. Behind a login. Lists invoices,
  each with a detail page and a downloadable PDF.
* **`/payables`** — the internal payables system. The system of record, with
  forms to create and correct records, and an audit log.

Two traps are built in, because an environment that always behaves cannot
demonstrate reliability:

**1. The obvious number is the wrong one.** The portal's list view shows the
**pre-tax subtotal**. The total payable appears only on the invoice PDF. An
agent that scrapes the table gets a plausible, wrong figure — and verification
catches it. (`tests/test_tools.py` asserts the two values genuinely differ.)

Each task resets the world to the state its own contract is written against,
declared as `env_profile` in the task file. The correction task needs a
pre-existing payable with a wrong due date to find; the data-entry task must
start with none, or verification ends up judging a mistake the agent did not
make. (It did exactly that in the first real run — the fix is `/admin/reset`
taking a profile.)

**2. The environment fails.** `acmeops/chaos.py` injects, from a seeded RNG so
any failure is reproducible:

* intermittent `503`s on reads (~18%),
* one mid-task session expiry, which bounces the agent to a login page,
* **two different DOM layouts** — columns reordered, labels reworded, form
  fields in a different order,
* occasional multi-second responses.

The layout shift is why the agent is never given raw HTML or asked to invent
CSS selectors. `agent/tools/browser.py` hands it a *semantic digest* — headings,
tables as rows with keys, form fields with their labels and `name` attributes,
links with their text — and it acts on semantic handles ("fill the field named
`due_date`"). The markup changes; the handles still resolve.

---

## Architecture

```
          Goal (one sentence from a colleague)
            │
   ┌────────▼────────┐   planner.understand()
   │   UNDERSTAND    │   → an explicit OUTCOME CONTRACT:
   └────────┬────────┘     success criteria, required evidence,
            │              requires_write, systems of record, unknowns
   ┌────────▼────────┐   planner.make_plan()
   │      PLAN       │   → an intended route. Advice, never executed as a script.
   └────────┬────────┘
            │
   ┌────────▼─────────────────────────────────────────┐
   │  EXECUTE / OBSERVE / ADAPT        (loop.py)      │
   │                                                  │
   │  planner.next_action() picks ONE tool from the   │
   │  live observation history — never "do step 3"    │
   │        │                                         │
   │        ├── mutating? ──► APPROVAL GATE ──► human │
   │        ▼                                         │
   │  tool runs → Observation(ok, summary, retryable) │
   │        │                                         │
   │        ├── retryable? ──► backoff retry          │
   │        ├── 3 failures? ──► controller injects a  │
   │        │                   change-of-approach    │
   │        ▼                                         │
   │  memory: facts (with provenance) + episodes      │
   │          (recent verbatim, older compacted)      │
   └────────┬─────────────────────────────────────────┘
            │ agent calls finish_task()
   ┌────────▼────────┐   verifier.verify()
   │     VERIFY      │   Sees ONLY the contract, the claim, and state
   │  (independent)  │   re-read from the JSON data endpoints.
   └────────┬────────┘   No step log. No narration. Verdict is binding.
            │
    confirmed? ──no──► re-plan against the verifier's objection, up to 2x
            │ yes
   ┌────────▼────────┐
   │    COMPLETE     │  evidence folder: report.md, trace.json,
   └─────────────────┘  screenshots/, downloads/
```

`agent/loop.py` contains no mention of browsers, invoices, PDFs or payables. It
knows about contracts, tools, observations, budgets, approval and verification.
Everything task-specific lives in the tool modules and the task files — which is
exactly why the same controller runs all three tasks unchanged.

| File | Responsibility |
| --- | --- |
| `agent/loop.py` | The controller. Budget, retries, stuck-detection, approval gate, re-plan decisions |
| `agent/llm.py` | Provider abstraction: Anthropic and OpenAI-compatible (Groq, OpenAI, local) behind one interface |
| `agent/planner.py` | Three separate model roles: `understand`, `make_plan`, `next_action` |
| `agent/verifier.py` | Independent verification against freshly-read state |
| `agent/memory.py` | Working memory (facts with provenance) + episodic memory with compaction |
| `agent/tools/base.py` | Tool protocol and registry. Adding a capability touches no other file |
| `agent/tools/browser.py` | Playwright + the semantic page digest |
| `agent/tools/files.py` | PDF and text extraction |
| `agent/tools/human.py` | `ask_human`, `remember`, `finish_task` |
| `agent/evidence.py` | Trace, report, screenshots, downloads |
| `acmeops/` | The company environment, including the chaos layer |

### Four design decisions I would defend

**1. "Done" is fixed in writing before any work starts.** `understand()` turns
the loose request into a contract of checkable end-state assertions. Everything
downstream is judged against that contract rather than the wording of the
request — which is what makes verification possible at all.

**2. The verifier cannot be talked into a yes.** It never sees the agent's work.
It gets the contract, the claim, and state read through a *different path* — the
JSON endpoints, while the agent was driving the UI. So a flash message, or a
form that looked submitted but wasn't, does not reach it. Its verdict is
binding and its objection is fed back as the next re-plan's brief.

**3. The model is a replaceable part.** `agent/llm.py` puts Anthropic and the
OpenAI-compatible API (Groq, OpenAI, a local server) behind one interface:
tool schemas are declared once and converted at the boundary, and structured
replies get a repair round because open-weight models fence and preface their
JSON far more often than frontier ones. A loop whose reliability depends on one
vendor's quirks is not a loop worth submitting — and the problem statement
explicitly does not prescribe a model. Switching is `--provider groq`.

**4. Noticing failure is the controller's job, not the model's.** Retry
eligibility is declared by the tool (`retryable`), not guessed by the model. A
run of three failures triggers an intervention the model didn't ask for. The
step budget is enforced from outside. Asking a confused model to notice its own
confusion is not a reliability strategy.

---

## What is genuinely autonomous

* **Deciding what "done" means.** Nothing in the task files lists steps,
  success criteria, URLs to visit or fields to fill.
* **Every production action.** One tool per turn, chosen from live
  observations. There is no scripted sequence in the production control path
  — `--all` runs three structurally different tasks through the same code path.
* **Finding the authoritative value.** No task says "read the PDF". The agent
  has to notice that the web view withholds the total and go to the document.
* **Recovery.** Re-authenticating after a session expiry, retrying through
  503s, re-reading when a layout differs, choosing a different route after a
  rejected verification.
* **Which record is wrong** in the correction task, and what the right value is.
* **When to stop and ask**, and when to stop and report a blocker.

## What is hard-coded or manually configured

Stated plainly, because the form asks and because a reviewer will find it:

* **The environment is mine.** AcmeOps was written for this submission. Its
  seed data, its two layout variants and its failure modes are all chosen by
  me. The agent has no privileged knowledge of it, but it is not a stranger's
  software.
* **The tool set is fixed at startup.** The agent composes tools freely but
  cannot write a new one. There is no code generation.
* **Credentials and a policy note are supplied in the task file** — the sandbox
  portal login, and the line that a payable records the tax-inclusive total.
  Both are the kind of context a new employee would be told. The agent is not
  given the record to fix, the right value, or any navigation path.
* **The verifier reads state through endpoints I wrote** (`/api/payables`,
  `/api/invoices`). In a real deployment this would be a database or system API;
  the separation of path from the agent's UI route is the part that matters, and
  that holds.
* **Human answers are scripted in `--auto-approve` mode** so unattended runs
  and the recorded demo are reproducible. Interactive mode is a real console
  prompt. Both are recorded in the trace.
* **No cross-run memory.** Working memory lives for one run and is then
  discarded. Persistent company memory is the next thing to build, not
  something this prototype has.
* **Verification is model-judged**, against mechanically-read state. The state
  is ground truth; the comparison is a model call with a narrow brief.

## Biggest technical limitation

**Verification is only as good as the contract, and the contract is written by
a model from a one-line request.** If `understand()` omits a criterion — say it
never states that the amount must be tax-inclusive — then a wrong amount can
satisfy every criterion that *was* written and be confirmed. The verifier is
rigorous about the contract it is given; it cannot know what the contract forgot
to ask. Making the contract itself reviewable, and letting a human amend it
before work starts, is the obvious next move and is not built.

**Model sensitivity, measured rather than assumed.** The controller's
guarantees — approval, budgets, retry eligibility, binding verification — hold
whatever model is behind it, and the offline suite proves that with the model
stubbed out entirely. What *does* vary with model strength is how often the
agent reaches a verified success before the step budget: weaker models burn
steps re-reading pages and are likelier to need a re-plan. The `--provider`
switch exists so that is a measurement rather than a guess, and the eval
harness below is what would turn it into a number.

Secondary limits: the semantic digest covers the HTML this environment uses
(tables, definition lists, labelled fields) and would need extending for
canvas-rendered or heavily client-side UIs; there is no concurrency, so one run
is one task; and token cost scales with step count because every turn re-sends
the contract and recent history.

## With another two weeks

1. **Persistent company memory.** Facts, procedures and per-system quirks
   written to a store that survives runs, so the second invoice task is faster
   and more certain than the first. This is the piece that turns a task worker
   into something resembling an AI employee.
2. **A reviewable contract.** Show the human the success criteria before work
   starts, let them amend, and treat the amended version as binding. Fixes the
   limitation above at its root.
3. **An eval harness.** Run each task across many chaos seeds *and both
   providers*, reporting verified-success rate, steps used and approvals
   requested. Right now I can say the system recovers; with this I could say how
   often, and how much of the remaining gap is the loop versus the model.
4. **Deterministic verification where it is possible.** Amounts and dates
   should be checked by code against the data store, with the model reserved
   for judgements that genuinely need language.
5. **A second environment the agent has never seen**, written by someone else,
   to test generalisation honestly rather than against my own app.
6. **Richer computer use** — a desktop app in the loop, to move beyond
   browser-only automation.

## Assumptions

* The browser is an acceptable way to reach these systems; no private APIs were
  assumed to exist.
* The invoice PDF outranks the web summary wherever they disagree. This is
  stated in the environment notes, as a policy would be.
* "Latest invoice" means most recently issued, not nearest due date. The
  contract makes the interpretation explicit, so a reviewer can disagree with it
  visibly rather than silently.
* One human approval per write is proportionate for accounts payable.
* Everything runs against a sandbox. No real company data, credentials or
  third-party systems are touched — `ap.clerk@acme.example` / `Vendor#2026`
  exist only in `acmeops/app.py`.

## Models, frameworks and tools used

* **Provider-agnostic model access** — three prompted roles (understand / plan /
  act) plus a separate verifier call, over native tool calling. Runs against
  **Groq** (open-weight models, via the OpenAI-compatible API) or **Anthropic
  Claude**, selected with `--provider`; `openai` and a `local` server are wired
  too. No agent framework: no LangChain, LlamaIndex, CrewAI or similar. The
  control loop is the submission, so importing one would have meant submitting
  someone else's answer.
* **Playwright** (Chromium) for browser automation.
* **FastAPI + Uvicorn + Jinja2 + SQLite** for the environment,
  **ReportLab** to generate invoice PDFs, **pypdf** to read them.
* **AI coding tools:** written with Claude's help in an agentic coding session —
  architecture, code, environment and this document. I have read every file,
  the test suites were written to check my own claims rather than to pass, and
  I can walk through, debug and extend any part of it.
