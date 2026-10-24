# Drafts for the CentrAlign submission form

Paste-ready. Trim to taste — the form gives no word limits, but short and
specific beats long and hedged, and every claim here is one the repo backs up.

---

### In 3–5 sentences, what did you build?

> An autonomous task worker that takes a plain-language company request and
> completes it by operating two real internal web systems through a browser —
> a vendor invoice portal behind a login, and an internal payables system. It
> first turns the request into an explicit contract of what "done" means, then
> chooses one action at a time from what it actually observes, reading invoice
> PDFs when the web view withholds the authoritative figure, re-authenticating
> when its session expires, and asking for human approval before any write.
> When it believes it is finished, an independent verifier re-reads the
> company's data through a different path and can reject the claim and send it
> back to work. I also wrote the environment it runs in, including deliberate
> failure injection — intermittent 503s, a mid-task logout, and two different
> DOM layouts — because reliability can only be claimed, not shown, against an
> environment that always behaves.

### Briefly explain your architecture

> Goal → Understand → Plan → Execute → Observe → Adapt → Verify → Complete,
> with each stage a distinct component.
>
> `understand` turns the request into an **outcome contract**: checkable
> assertions about end state, required evidence, whether a write is involved,
> and which system holds the truth. `plan` produces an intended route that is
> advice only — no step of it is ever executed directly. The execute loop calls
> `next_action`, which picks exactly one tool per turn from the live observation
> history; the model is never told which step it is on. Tools are registered
> with JSON schemas in a registry, so the controller dispatches whatever the
> model picks and `loop.py` contains no mention of browsers, PDFs or payables —
> that is what lets three structurally different tasks run through the same code
> unchanged.
>
> The controller, not the model, owns reliability: retry eligibility is declared
> by the tool rather than guessed, three consecutive failures trigger an
> intervention the model didn't ask for, the step budget is enforced from
> outside, and every tool marked `mutating` is routed through a human approval
> gate the agent cannot bypass. Memory is split into facts with provenance and
> an episodic log that keeps recent steps verbatim and compacts older ones.
>
> Verification is separate on purpose. The verifier sees only the contract, the
> claim, and state read freshly from the data endpoints — never the agent's step
> log or narration — so there is nothing to talk it into. Its verdict is
> binding; a rejection becomes the brief for the next re-plan.

### What parts of your system are genuinely autonomous?

> Deciding what "done" means — the task files contain no steps, criteria, URLs
> or field names. Every action, chosen one at a time from live observations;
> there is no scripted sequence anywhere in the codebase. Working out that the
> portal's list view shows only a pre-tax subtotal and that the total payable
> has to come from the invoice PDF — no task mentions the PDF. Recovery:
> re-authenticating after a session expiry, retrying through 503s, re-reading
> when the layout differs, and taking a different route after a rejected
> verification. In the correction task, which record is wrong and what the right
> value is. And when to stop — either to ask a human, or to report a blocker
> rather than claim a false success.

### What is currently hard coded or manually configured?

> The environment is mine: AcmeOps was written for this submission, so its seed
> data, layout variants and failure modes are my choices, though the agent has
> no privileged knowledge of it. The tool set is fixed at startup — the agent
> composes tools freely but cannot write new ones, and there is no code
> generation. The task file supplies sandbox portal credentials and one policy
> line (a payable records the tax-inclusive total), which is the context a new
> employee would be given; it does not supply the record to fix, the right
> value, or any navigation path. The verifier reads state through two JSON
> endpoints I wrote — in a real deployment that would be a database or system
> API; what matters is that it is a different path from the UI the agent drove.
> Human answers are scripted in unattended mode so runs are reproducible, and
> both scripted and interactive approvals are recorded in the trace. There is no
> cross-run memory: working memory lives for one run and is discarded.
> Verification is model-judged against mechanically-read state.

### What models, frameworks, APIs, libraries, AI coding tools, or existing projects did you use?

> The model is a replaceable part: `agent/llm.py` puts Anthropic's Messages API
> and the OpenAI-compatible Chat Completions API behind one interface, so the
> same loop runs on **Groq** (open-weight models) or **Claude** with
> `--provider`. Tool schemas are declared once and converted at the boundary,
> and structured replies get a repair round because open-weight models fence and
> preface their JSON far more often than frontier ones. Three prompted roles
> (understand / plan / act) plus a separate verifier call, over native tool
> calling. No agent framework: no LangChain, LlamaIndex, CrewAI or similar — the
> control loop is the thing being evaluated, so importing one would have meant
> submitting someone else's answer. Playwright with Chromium for browser
> automation. FastAPI, Uvicorn, Jinja2 and SQLite for the environment, ReportLab
> to generate the invoice PDFs, pypdf to read them. No pre-built templates or
> starter projects.
>
> AI coding tools: built with Claude's help in an agentic coding session —
> architecture, code, environment and documentation. I have read every file, and
> the four test suites (98 checks, none of which need an API key) were written to
> check my own claims rather than to pass. One of them runs the whole pipeline —
> real browser, real login, real PDF extraction, real write, real approval gate,
> real state read — with only the model's choice of action stubbed, so I can say
> precisely which parts are proven and which depend on model judgement. I can
> walk through, debug and extend any part of it.

### What is the biggest technical limitation of your current solution?

> Verification is only as good as the contract, and the contract is written by a
> model from a one-line request. If `understand` omits a criterion — say it
> never states that the amount must be tax-inclusive — a wrong amount can
> satisfy every criterion that *was* written and be confirmed. The verifier is
> strict about the contract it is given; it cannot know what the contract forgot
> to ask. Making the contract reviewable and amendable by a human before work
> starts is the fix, and it is not built.
>
> Separately worth stating: the controller's guarantees — approval, budgets,
> retry eligibility, binding verification — hold whatever model is behind them,
> and the offline suite proves that with the model stubbed out. What varies with
> model strength is how often the agent reaches a verified success inside the
> step budget. The `--provider` switch exists so that is measurable rather than
> assumed.
>
> Smaller limits: the semantic page digest covers the HTML this environment uses
> and would need extending for canvas-rendered or heavily client-side UIs; one
> run handles one task, with no concurrency; and token cost grows with step
> count because each turn re-sends the contract and recent history.

### If you had another 2 weeks, what would you build/change next?

> 1. **Persistent company memory** — facts, procedures and per-system quirks
>    surviving across runs, so the second invoice task is faster and more
>    certain than the first. This is the piece that turns a task worker into
>    something resembling an AI employee.
> 2. **A reviewable outcome contract** — show the human the success criteria
>    before work begins, let them amend, treat the amended version as binding.
>    Fixes the main limitation at its root.
> 3. **An eval harness** — every task across many chaos seeds, reporting
>    verified-success rate, steps used and approvals requested. Today I can say
>    it recovers; with this I could say how often.
> 4. **Deterministic verification where possible** — check amounts and dates in
>    code against the data store, reserving the model for judgements that
>    genuinely need language.
> 5. **A second environment I didn't write**, to test generalisation honestly.
> 6. **Richer computer use** — a desktop app in the loop, beyond browser-only.

---

## Other form fields

- **Role:** AI Engineering Intern
- **When can you start?** Mid-December 2026, remote. Final semester (Jan–May
  2027) is online, with only brief week-long trips to India for exams in March
  and May.
- **Current status:** Student
- **Degree / graduation year:** B.Tech, IIIT Allahabad, 2027

## Final declaration — read before ticking

All five boxes are truthful **provided** you have:

1. run the system end to end yourself at least once,
2. read every file in `agent/` and `acmeops/`,
3. disclosed the AI coding tools (the answer above does),
4. kept everything in the sandbox (it is),
5. satisfied yourself you could debug or extend it live — they will ask.

Point 1 is the one still outstanding.
