# Design decisions

The choices I expect to be asked about, with the alternative I rejected and why.

---

## 1. A manual loop, not the SDK tool runner

The Anthropic SDK ships `client.beta.messages.tool_runner`, which drives the
request → execute → loop cycle for you. LangChain, LangGraph and friends offer
more. I wrote the loop by hand.

**Why.** The loop *is* the product here. Three requirements make a managed loop
unworkable:

- **Mid-loop suspension.** The run has to stop for human approval, release all
  resources, and resume minutes later with the answer delivered as the pending
  tool call's result. A runner that owns the loop owns the stack, and that is
  exactly the thing that has to be let go.
- **A gate between decision and execution.** The approval policy and the
  guardrails run *after* the model picks a tool and *before* the tool executes.
  That hook is the safety story.
- **Being able to explain and debug every line.** An agent framework is a large
  surface of someone else's control flow; when an agent misbehaves at 2am you are
  debugging their abstraction, not your problem.

**The cost.** ~200 lines I own and must maintain, and I had to handle `pause_turn`
and no-tool-call turns myself. Worth it.

**When I'd reverse it.** If the loop became commodity — no approval gate, no
suspension — the runner is less code.

---

## 2. Element refs, not CSS selectors

The agent acts by `click e4`, where `e4` was minted by the snapshot and written
into the DOM as `data-agentref`.

**The alternative** is letting the model emit selectors (`button.btn-primary`) or
coordinates. Both fail badly: selectors get hallucinated from HTML the model half
remembers and silently match the wrong node; coordinates break on any layout
change and need vision for something that is already text.

**Why refs win.** The model can only name things the snapshot just showed it. A
stale ref produces an explicit, recoverable error ("element e4 is not on the
current page — take a fresh snapshot") rather than a wrong action that looks like
it worked. Wrong actions that look successful are the worst failure mode in
browser automation, because every later step reasons on a false premise.

**The cost.** Refs are only valid for one snapshot, which the model has to be
told about, and a page that re-renders mid-step invalidates them. That is an
honest failure, and it is loud.

---

## 3. Every action returns the resulting page

`browser_click` returns the post-click snapshot. There is no separate "now look
at the page" step.

**Why.** Observation is not optional and should not depend on the model
remembering to do it. Making it the return value halves the step count, removes a
whole class of "acted twice without looking" bugs, and means an action and its
consequence are adjacent in the transcript — which is also what makes the
transcript readable to a human debugging it.

**The cost.** Snapshots on every action, including ones where nothing changed.
Bounded by the truncation limits.

---

## 4. Verification is a separate agent with read-only tools

`finish` submits claims; it does not end the run.

**The alternative** — asking the model "are you sure?" — is theatre. A model that
just made a mistake is the worst available judge of whether it made one; it has
the mistake in context as an assumption.

**Why this shape.**

- **Different surface.** The worker writes through a web form; the verifier reads
  the JSON API. A stale rendered page cannot fool it.
- **Its own browser.** Otherwise it inherits the worker's logged-in session and
  can mistake leftover page state for independent evidence.
- **Structurally read-only.** The toolbelt is built by *filtering on the
  `read_only` flag*, not by listing names, so a tool added next year cannot leak
  write access into verification without opting in. `http_request` additionally
  refuses non-`GET` during the verify phase.
- **Given the original goal, not just the claims.** An agent can make true claims
  about work that missed the point — recording the *wrong* invoice, correctly.

**The cost.** An extra model pass per run, and `inconclusive` is a real outcome:
the verifier sometimes cannot check a claim with read-only access. I treat that
as not-verified rather than optimistically passing it.

---

## 5. Approval is a gate in code, not a tool the model may call

The model has `request_approval`. The policy in `app/agent/policy.py` runs in the
executor before every tool call regardless.

**Why.** "Ask before doing something expensive" as a prompt instruction is a
*tendency*, not a guarantee. A gate in the executor is a guarantee. Approval
thresholds are business policy — they belong in code that a compliance reviewer
can read, not in a paragraph the model may or may not weight heavily today.

**The subtle part.** A form submit carries no amount in its arguments — just
`{"ref": "e8"}`. So the policy derives the value in play from the agent's
*committed facts*: it knows the invoice total because it remembered it. Approval
is based on what the agent knows it is doing.

That bit me during development, and the bug is instructive: `_largest_amount`
scanned all arguments, matched the `8` in the ref `e8`, and short-circuited past
the facts — silently disabling the threshold. Fixed by excluding non-monetary
argument names and taking the max of both sources.
(`tests/test_units.py::test_policy_ignores_refs_when_looking_for_money`.)

---

## 6. Transient failures are retried below the model

A 503 is retried in the executor with backoff. The model never sees it.
A rejected form value goes straight back, verbatim, and is **never** auto-retried.

**Why the asymmetry.** It is about information content. A 503 carries none —
making the model reason about it costs a step, pollutes the transcript, and
sometimes produces a *wrong* conclusion ("the credentials must be wrong"). A
validation error is the most useful thing that could have happened: it states the
correct format. Retrying it unchanged is guaranteed to fail again.

This is the `ErrorKind` taxonomy in `app/agent/schemas.py`, and I think it is the
highest-value reliability idea in the system.

---

## 7. Explicit memory, not transcript recall

Load-bearing values are committed with `remember` rather than trusted to survive
in context.

**Why.** Over 30 steps the transcript becomes tens of thousands of tokens of page
snapshots. Expecting reliable recall of one number from the middle of that is a
bet I do not want a payment amount riding on. Committed facts are re-rendered
into every later prompt at full fidelity.

It also gives three things free: the console can show what the agent "knows" as
it learns it, the verifier gets a clean set of claims, and the evidence bundle
has the provenance of every value.

**The cost.** It depends on the model choosing to call `remember`, so it is a
prompt-reliability dependency. A stricter design would auto-extract facts from
observations — more robust, much more machinery.

---

## 8. Append-only transcript

Facts are appended as new messages rather than spliced into history, and old
observations are never rewritten.

**Why.** Current Claude models bind reasoning blocks to the conversation that
produced them; editing earlier turns invalidates them. An append-only transcript
sidesteps that entirely. It is also simply easier to debug — `transcript.json` is
what the model saw, in order, with nothing retroactively changed.

**The cost.** Context grows monotonically. At 40 steps that is comfortable inside
a 1M window; a longer-horizon version needs server-side compaction, which is the
documented path rather than hand-editing history.

---

## 9. One process serves the agent, the world and the console

**Why.** `python -m app` gives a working demo with no orchestration — which
matters a lot for something that will be cloned and run by a reviewer. The agent
still talks to the simulated world over **real HTTP with a real browser**, so the
seam between runtime and world is the network boundary. Splitting them later is a
deployment change, not a rewrite.

**The cost.** Runs share one simulated world, so concurrent runs would interfere.
Fine for a prototype; called out in the README.

---

## 10. A scripted planner behind the Brain interface

`ScriptedBrain` implements `decide(messages) -> Decision` and replays a fixed
sequence for the invoice task.

**Why.** The interesting failure modes of an agent runtime are not in the model —
they are in the harness. Does a transient failure get retried silently? Does the
approval gate hold on *both* submits? Does a refuted verification actually feed
back? Does the evidence bundle land intact? Testing those against a live model
would be slow, expensive and non-deterministic. Testing them against a scripted
planner is none of those.

The key property: the orchestrator cannot tell the difference, so the tests
exercise the **real** loop — real browser, real sandbox, real executor, real
policy — with only the model swapped.

It is not blind replay. It parses refs out of live snapshots and picks the latest
invoice by reading dates off the real page, so the test still fails if the
snapshot layer, the sandbox or the executor break.

**The honest caveat.** It is scaffolding, not a fallback. It cannot handle a task
it was not written for, and nothing about it demonstrates autonomy. The README
says so where it is offered as a demo.

---

## 11. Secrets are redacted outbound, not inbound

The agent reads the portal password from the workspace and types it into a login
form. That is the task working correctly. What is not acceptable is that password
sitting in `run.json`, in `report.md`, in the console's event stream and in the
step timeline — all of which are kept, and some of which get shared.

**The boundary:** redaction is applied on the way *out* — to disk and to the
operator — and never on the way *in* to the model.

Redacting inbound would be security theatre that also breaks the task: the model
cannot sign in with `[redacted]`.

**Learned, not configured.** Whatever the agent reads that looks like a declared
credential (`Password: x`, `api_key = y`), plus anything it types into a field
the snapshot classified as `role=password`, gets registered for the run and
scrubbed from then on. That generalises to credentials this prototype has never
seen, rather than hard-coding the sandbox's password.

**Found by looking.** This was not designed in — I spotted the password in
plaintext in a console screenshot while capturing images for the README. It is a
good argument for screenshotting your own UI.

---

## 12. No build step for the console

Hand-written HTML/CSS/JS, served statically.

**Why.** A reviewer should be able to clone and run with `pip install` and
nothing else. A React toolchain would add `node_modules`, a build step and a
version-skew failure mode to a UI that is one page of DOM updates driven by an
event stream. The whole console is one readable file.

**The cost.** Manual DOM manipulation. At this size it is less code than the
framework would be.

---

## 13. Faults ship on by default

`flaky_login` and `strict_validation` are active out of the box.

**Why.** The default experience should demonstrate the thing that is hard. An
agent that completes a clean happy path proves very little; one that hits a 503,
retries, submits a badly formatted amount, reads the rejection and fixes itself
has demonstrated the behaviours the brief actually asks about. Making that the
*default* run rather than a special mode is a product decision as much as a
testing one.

---

## Things I got wrong along the way

Worth saying out loud, since all three were caught by tests rather than by
reading the code:

- **The approval gate silently did nothing.** Element refs (`e8`) contain digits,
  so the "is there money in play?" scan matched `8` and short-circuited past the
  agent's committed facts. The gate looked present and was inert. (Decision 5.)
- **One-shot faults did not re-arm on reset.** The counters lived in a module
  global rather than on the world object, so the second run in a process got an
  easier environment than the first — the kind of bug that makes a demo look
  better than the system is.
- **A CSS `display` rule beat the `hidden` attribute**, leaving the lightbox
  overlay permanently covering the console. Caught by screenshotting the UI
  rather than assuming it rendered.
