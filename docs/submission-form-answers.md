# Drafts for the CentrAlign submission form

### 1. In 3 to 5 sentences, what did you build?

I built an autonomous task worker that takes a natural language company request and operates a vendor portal and a payables system through a real browser. It first turns the request into an outcome contract, then uses an advisory plan and chooses one action at a time from live observations. The simulated environment deliberately injects intermittent 503 responses, session expiry, changing page layouts, and changing submit labels. After the worker declares completion, an independent verifier reads fresh state through JSON endpoints and can reject the claim and send the worker back to work.

### 2. Briefly explain your architecture.

The flow is Goal to Understand to Plan to Execute to Observe to Adapt to Verify to Complete. `agent/planner.py` contains `understand`, `make_plan`, and `next_action`. `understand` creates the outcome contract, `make_plan` creates an intended route, and `next_action` chooses one tool from the current history, facts, contract, and environment notes. The plan is advice only, and no plan step is executed by index.

`agent/loop.py` owns the step budget, retries, approval gate, stuck detection, replanning, and verification handoff. `agent/tools/base.py` defines the registry, while `agent/toolsets.py` injects the browser, file, and human tools. `agent/tools/browser.py` returns a semantic page digest rather than raw HTML. `agent/memory.py` keeps facts with provenance and recent observations.

`agent/verifier.py` receives the outcome contract, the worker's declared result, and a freshly read state snapshot. It does not receive the agent's step log, reasoning, or narration. Unsupported confirmations are downgraded mechanically, and truncated state cannot produce a confirmed verdict. The optional `--review-contract` flag prints the contract and asks for approval before execution.

### 3. What parts of your system are genuinely autonomous?

The model generates the contract from the request and chooses every production action one at a time from current observations. It is not given a fixed navigation sequence or field order. The controller retries observations marked as transient, detects repeated actions and no progress, intervenes after consecutive failures, and enforces the step budget. Mutating actions are never retried because a request timeout may have committed the write.

The browser tools support changing layouts through semantic names, and a submit label mismatch falls back to the form's submit control. Session expiry is represented by the login page, and the executor prompt tells the model to sign in again and resume. The preserved confirmed example at `runs/examples/invoice_entry-confirmed` proves an unscripted run recovered from two injected 503 responses inside one browser action and then completed with independent verification. That example does not prove a preserved session expiry recovery, so I do not claim that it does.

The verifier has authority over completion. A confident worker claim is not sufficient. A failed verification produces a specific replan brief, and the loop can attempt the task again without accepting the original claim.

### 4. What is currently hard coded or manually configured?

The repository has three task JSON files. They contain the requests, environment notes, sandbox credentials, and business policies such as using the tax inclusive amount from the source document. The mock company application, its fixture data, PDFs, failure injection, and two page layouts are also written for this submission.

The browser, file, and human tools are fixed at startup, although the controller receives them through an injected toolset. The default step budget is 28. The controller uses a failure streak threshold of 3, a repeat threshold of 3, a no progress threshold of 2, a maximum of 3 no action failures, and up to 2 retries for a retryable non mutating action. Provider and model selection are configured through command line flags and environment variables.

The verifier's state reader calls the application's JSON endpoints for payables and invoices. This is a deliberate separation from the browser path, but it assumes the environment exposes those endpoints. The policy that decides which of two amounts is authoritative is supplied in the environment notes. The agent is expected to apply that policy, not discover it from a general company knowledge base. Working memory is per run and is not persisted across runs.

### 5. What models, frameworks, APIs, libraries, AI coding tools, or existing projects did you use?

The recorded confirmed run used Groq with the `openai/gpt-oss-20b` model for both the worker and verifier. The code also supports Anthropic Claude through the Anthropic Messages API and OpenAI compatible Chat Completions providers through the `openai` package. The provider abstraction includes Anthropic, Groq, OpenAI, and a local OpenAI compatible server, but the preserved run evidence specifically verifies Groq with `openai/gpt-oss-20b`.

The runtime dependencies in `requirements.txt` are `anthropic`, `openai`, `playwright`, `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `pypdf`, and `reportlab`. I used Playwright with Chromium for browser control, FastAPI and Uvicorn for the mock application, Jinja2 for its templates, SQLite for persistence, ReportLab for generated PDFs, and pypdf for PDF extraction. I did not use LangChain, CrewAI, AutoGen, or another agent framework.

The project was built with Claude, GitHub Copilot, and Antigravity as AI coding tools. I disclose all three because the submission asks for the coding tools used, not only the runtime model. The tests and the preserved run evidence distinguish model judgment from the controller and browser behavior that were tested locally.

### 6. What is the biggest technical limitation of your current solution?

The biggest limitation is that the outcome contract is generated by a model. The verifier is strict about the contract it receives, but it cannot enforce a business requirement that the contract omitted. If the contract forgot that an amount must include tax, an incorrect amount could satisfy every criterion that was written. I found no preserved run showing that this happened in practice, so I describe it as a demonstrated design risk rather than an observed failure.

The hardening change makes this weakness visible. The full contract is printed immediately after `understand` returns, included in the trace and report, and can be reviewed through `--review-contract` before any task action runs. A reviewer can decline it and the run ends cleanly as blocked. The next stronger step would be to make important fields deterministic or allow the reviewer to amend the contract.

### 7. If you had another 2 weeks, what would you build or change next?

First, I would make critical contract fields reviewable and partially deterministic, especially amounts, dates, record identity, and duplicate constraints. This addresses the main correctness risk at the point where it begins.

Second, I would add a typed authentication required observation and a controller enforced re authentication path. The current model directed recovery is useful, but a controller guarantee would be more reliable.

Third, I would add a broader task suite, including a non finance environment written by someone else. This would pressure test whether the generic control plane works outside the application it was built with.

Fourth, I would add cross run company memory so policies and system procedures could be learned and reused rather than supplied in every task's environment notes.

Fifth, I would replace the convenient JSON endpoint state reader with a verifier integration that uses a real system of record interface without assuming that a special application endpoint exists.

### 8. Declaration check

The code and evidence now support the run, sandbox, disclosure, and debugging claims, but the author must personally confirm that they have read every file and can debug the system live. The demo video or live demo requirement is still not satisfied, so any declaration that includes that requirement cannot be checked until a real link is added.
