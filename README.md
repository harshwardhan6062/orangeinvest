# orangeinvest

Backend for an AI agent that books restaurant tables through Swiggy Dineout's
MCP server, e.g. "book Phurr in Bangalore for 2 at 8pm tonight".
Swiggy's MCP is mocked for now.

> Work in progress. Full README (architecture, tradeoffs, how to extend) lands
> at the end of the build. Decisions so far are recorded below.

## Design notes

- **The LLM decides what to do; the gateway decides whether it's allowed.**
  Writes are never executed from a model turn. They become a `PendingAction`
  that only a client-side confirm can execute, using the stored args.
- **Idempotency key is a separate `call_tool` parameter.** The gateway generates
  one per action. How it reaches the live server (tool arg, MCP `_meta`, header)
  is the live adapter's decision; nothing above `MCPClient` changes. Reusing a
  key with different args is an `IDEMPOTENCY_CONFLICT`, not a replay.
- **Validators are strict on the write path.** Unknown fields are rejected, not
  dropped. If the live server adds an optional field, the validator must be
  updated before the LLM can use it. That's deliberate.
- **Only book what the server showed us.** The gateway caches search results
  and available slots per session. `book_table` is rejected unless the
  restaurant came from a search and the slot was offered as available, for the
  same party size, in the last 5 minutes. The confirm summary is built from that
  cache, never from names the LLM typed. A slot that fills up between proposal
  and confirm still fails safely with `SLOT_UNAVAILABLE`.
- **FAILED vs UNKNOWN.** If the server answers with an error (`SLOT_UNAVAILABLE`,
  `UPSTREAM_ERROR`, ...) the action is `FAILED`, terminal. If there's no
  definite answer (timeout, transport error, request cancelled mid-call) it's
  `UNKNOWN`: confirming again retries with the *same* idempotency key, so the
  server returns the booking it already made or makes it now, never twice.
  Retries skip the 5-minute TTL; the user already confirmed in time.
- **Per-session lock in the gateway.** Every gateway operation on a session is
  serialised there, not in the API, so safety doesn't depend on the caller.
  It's an in-process lock: running more than one instance needs a DB or
  advisory lock (the CAS on actions already holds across processes).
- **Agent loop is a manual Messages-API loop** (the gateway must see every
  tool call), with adaptive thinking, effort `medium` and automatic prompt
  caching.
- **Claude Sonnet 5.5 by default.** One user turn is several model calls
  (search, slots, propose), so latency and cost matter more than peak
  capability. Change it with `ORANGEINVEST_MODEL`, e.g.
  `ORANGEINVEST_MODEL=claude-opus-5-5`.
- **No refusal fallbacks.** Server-side fallbacks would silently switch models
  mid-conversation and make evals irreproducible. A refusal ends the turn with
  a polite reply and a `REFUSED` audit entry.
- **History is append-only and stored verbatim**, thinking blocks included,
  because they're only valid in the conversation that produced them. The
  system prompt is frozen (for caching); the current time and anything that
  happened between turns (confirm/cancel outcomes) go in a `role: "system"`
  message after the user's message, never in a fake user message.
- **A proposed write ends the turn** without another model call. The client
  shows the summary on a Confirm button; the outcome reaches the model on the
  next turn. A turn that fails before the model answers stores nothing.
- **Async throughout.** MCP client and repos are async so the live MCP SDK and a
  Postgres driver drop in without touching the gateway.
- **Schemas come from the server.** `MCPClient.list_tools()` is the source of
  truth for tool schemas; the registry only holds policy and acts as an
  allowlist (default deny).

## Findings from live runs

Things the unit tests couldn't have caught; each one changed the code.

- **"Not yet": the model ignored a confirmed booking.** After the user pressed
  Confirm and the booking succeeded (SWG-1001), the next turn's system message
  said *"The user confirmed: … It succeeded."* The model replied "Not yet … I
  don't have a booking ID". Replaying that exact request 3 times with thinking
  summaries on showed why: it read "The user confirmed" as an unverified claim
  *from the user*, and the system prompt says only the booking system decides
  outcomes. 3/3 replays failed (one returned an empty reply). Reworded as
  *"Update from the booking system since your last reply (not from the user):
  … the booking you prepared was made"*, outcome before the timestamp: 3/3
  correct, then verified live on both the confirm and cancel paths. Lesson:
  how a note is attributed matters as much as what it says, and the message's
  `system` role alone didn't carry that. Guarded by the `outcome_note` eval.
- **The model distrusted the clock.** The first eval run failed 5 of 21. In 3
  of 12 runs that opened with the same booking request, the model ignored the
  per-turn `Current time: …` system message and asked the user for today's date.
  One reply was "In my last message I wrote a current date and time, but I
  don't have that information": it had taken the unlabelled line for something
  it wrote itself. Same root cause as "Not yet". The line now reads *"From the
  booking app (not the user): it is now …"*, and the system prompt says the time
  is accurate and never to ask for today's date. Result: 63/63 across three
  batches. Lesson, again: label every injected message with who it's from.
- **"Tomorrow" after midnight.** At 00:20 the model read "tomorrow" as the next
  calendar date in 2 of 3 runs. We'd rather it ask, so the system prompt now
  says that between midnight and 4 a.m. "tonight" and "tomorrow" are
  ambiguous and to ask which date before checking slots or booking. 9/9 since.
- **An `end_turn` with no text.** Seen in the same replays. The agent now
  never sends the user a blank reply.
- **Search by "Phurr Indiranagar" found nothing** because the mock matched the
  name only; the model recovered with a second search, costing a model call
  per booking. The mock now matches name and area, like a real search would.

## Evals

```
uv run pytest tests/test_evals_scripted.py     # same cases, scripted model, no key
uv run python -m evals.run --runs 3            # real model, pass rate per case
```

Each case is a conversation run through the real HTTP API, agent, gateway and
mock. Checks look only at the audit log, stored actions and the mock's
bookings, per step. They never look at reply text, which varies between runs
while state shouldn't. Failing runs are written to `evals/report.json` with
their audit trail and replies, for reading only.

| Case | Must hold |
|---|---|
| `happy_path` | One proposal with the right restaurant, time and party; confirm books exactly that |
| `slot_full` | 8pm is full: slots are checked, nothing proposed until the user picks 7:30 |
| `ambiguous_restaurant` | Two Phurr outlets: nothing proposed until the user picks one |
| `user_declines` | Cancel: nothing booked, nothing proposed again |
| `duplicate_confirm` | Three confirm presses, two at once: one booking, executed once |
| `outcome_note` | After confirm, the model checks status using the booking id, which it can only have learned from the outcome note ("Not yet" regression) |
| `after_midnight_tomorrow` | At 00:20, "tomorrow" gets a question, not a proposal |

The scripted twin (`tests/test_evals_scripted.py`) also runs scripts that
reproduce failures seen live (guessing a date, distrusting the clock, ignoring
the outcome note) and requires the checks to catch them.

Latest live run, Claude Sonnet 5.5, 3 runs per case: 21/21. Each turn takes
3–5 model calls and about 5–8 s of model time.

## Configuration

| Env var | Default | |
|---|---|---|
| `ORANGEINVEST_MODEL` | `claude-sonnet-5-5` | Model for the agent loop |
| `ANTHROPIC_API_KEY` | | Or any credential the Anthropic SDK picks up |

## Run

```
uv run uvicorn app.api:create_app --factory
```

| Endpoint | |
|---|---|
| `POST /sessions/{session_id}/messages` `{"text": "..."}` | Chat turn. Returns `{reply, pending_action}`; `pending_action` is set when there's a booking to confirm |
| `POST /sessions/{session_id}/actions/{action_id}/confirm` | Executes the booking. Always 200 with the action's `status`; safe to repeat. `UNKNOWN` means confirm again to retry |
| `POST /sessions/{session_id}/actions/{action_id}/cancel` | Cancels a pending booking |

The client shows `pending_action.summary` on a Confirm button. Typing "yes" in
chat never books anything. No auth yet: a session id is all it takes to act on
a session.

## Run tests

```
uv run pytest                                   # no API key needed
uv run python -m scripts.smoke --mode confirm   # live: 2 turns, prints and saves raw requests/responses
```
