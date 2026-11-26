# orangeinvest

Backend for an AI agent that books restaurant tables through Swiggy Dineout's
MCP server: *"book Phurr in Bangalore for 2 at 8pm tonight"*. One flow, done
carefully: search → slots → confirm → book → status. Swiggy's MCP server is
mocked; the agent runs on Claude.

```
./run.sh                                         # checks uv + key, starts the API, opens the chat UI
uv run pytest                                    # 190 tests, no API key needed
uv run uvicorn app.api:create_app --factory      # API on :8000 (needs ANTHROPIC_API_KEY)
uv run python -m evals.run --runs 3              # live evals, pass rate per case
uv run python -m scripts.smoke                   # live 2-turn run, saves raw requests/responses
```

| Env var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | | Or any credential the Anthropic SDK picks up |
| `ORANGEINVEST_MODEL` | `claude-sonnet-5-5` | Model for the agent loop |

## Try it

Run `./run.sh` and a chat page opens. When the assistant prepares a booking,
it shows up as a card with **Confirm** and **Cancel** buttons and a countdown
to expiry. That button is the only way anything gets booked.

| Type or do | What you should see |
|---|---|
| `Book Phurr Indiranagar for 2 at 7:30pm tomorrow` | A booking card. Press Confirm, then ask *"what's my booking ID?"* |
| `Book Phurr Indiranagar for 2 at 8pm tomorrow` | 8pm and 8:30pm are full; it offers nearby times and proposes nothing until you pick one |
| `Book Phurr for 2 at 7:30pm tomorrow` | Two outlets: it asks which one |
| Press **Confirm again** on a booked card | The same booking ID comes back; it never books twice |
| Type `yes` instead of pressing Confirm | Nothing is booked |
| `Book Phurr Koramangala for 2 at 9pm tomorrow`, then Confirm | The mock's scripted failure: the booking fails and nothing is booked |

Between midnight and 4 a.m. it will first ask which date you mean by "tomorrow".

## Architecture

```
            chat turn                         Confirm / Cancel button
 client ─────────────────► API ◄───────────────────────────── client
                            │ run_turn                │ confirm / cancel
                            ▼                         │
            Claude ◄────► Agent loop                  │
                            │ every tool call         │
                            ▼                         ▼
       Registry ─────────► Gateway ◄──────────────────┘
       (policy)              │      │
                             ▼      ▼
                     MCPClient      Repos: sessions · pending actions · audit log
                  (MockDineout → Swiggy MCP)
```

The model decides what to do. The gateway decides whether that's allowed.

| Component | Role |
|---|---|
| [api.py](app/api.py) | FastAPI. One chat endpoint, plus client-only `confirm` and `cancel`. No logic of its own |
| [static/index.html](app/static/index.html) | The chat UI: one static page, no build step. Bookings appear as Confirm/Cancel cards |
| [agent.py](app/agent.py) | Generic tool-use loop. Knows nothing about restaurants; the system prompt and tools carry that |
| [llm.py](app/llm.py) | Claude on the Messages API: adaptive thinking, effort `medium`, prompt caching |
| [gateway.py](app/gateway.py) | Every tool call goes through it. Enforces the safety model below |
| [registry.py](app/registry.py) | Policy as data: risk tier, validator, summary builder and fact recorder per tool |
| [validators.py](app/validators.py) | Deterministic arg checks, run before anything reaches the server |
| [mcp_client.py](app/mcp_client.py) | The interface live Swiggy will implement, with typed errors |
| [mock_dineout.py](app/mock_dineout.py) | Deterministic stand-in: two Phurr outlets, full slots, scripted failures |
| [repos.py](app/repos.py) | Async state interfaces with in-memory implementations |
| [evals/](evals/) | Conversation evals: a live runner, plus a scripted twin in pytest |

## Safety model

- **Default deny.** The model only sees, and the gateway only runs, tools in the registry. The server owns the schemas, and the registry filters them.
- **Validate first.** Every tool's args are validated before any server call. Writes must also match what the server showed this session: the restaurant came from a search, and the slot was offered as available, for the same party size, in the last 5 minutes.
- **Writes become pending actions.** `book_table` never runs from a model turn. It's stored with validated args and a summary built from server data, and the turn ends.
- **Only the client can confirm.** Confirm and cancel are HTTP endpoints the model has no tool for. Typing "yes" in chat is just a message.
- **Confirm runs the stored snapshot.** It executes exactly what the summary showed, with no model call in between. Actions expire after 5 minutes.
- **Idempotency.** The gateway generates one key per action. A compare-and-set from PENDING to EXECUTING makes any number of confirm presses execute once.
- **FAILED vs UNKNOWN.** A definite error from the server means `FAILED`, and that's final. No definite answer (a timeout, a transport error, or the request being cancelled mid-call) means `UNKNOWN`. Confirming again then retries with the same key, so it can never book twice.
- **Per-session lock in the gateway, not the API,** so safety doesn't depend on the caller.
- **Everything is audited:** reads, denials, validation failures, proposals, confirms, outcomes and refusals.

## Key decisions and tradeoffs

- **Async throughout.** The MCP SDK, the Anthropic SDK, FastAPI and Postgres drivers are all async. Going async later would have meant rewriting the gateway.
- **Per-session cache of server facts.** Writes and confirm summaries use only what the server returned: restaurant names come from search results, never from text the model typed. The cost is a "look before you book" rule. If the model skips reading slots, the gateway rejects the booking and the model has to check first.
- **Strict validators.** Unknown fields are rejected, not dropped. If Swiggy adds an optional field, nobody can use it until a validator allows it. That's deliberate on the write path.
- **Sonnet 5.5 by default.** A booking takes 3–5 model calls, so latency and cost outweigh peak capability. It's one env var to change.
- **No refusal fallbacks.** Server-side fallbacks silently switch to another model, which makes evals irreproducible. A refusal ends the turn politely and writes a `REFUSED` audit entry.
- **History stored in Anthropic's message format, exactly as returned.** That's required: thinking blocks are only valid in the conversation that produced them, so history is append-only and stored unchanged. The cost is that stored conversations are tied to one provider.
- **Injected context goes in labelled `system` messages after the user's message.** That covers the clock and confirm outcomes. The system prompt itself stays frozen, so caching holds, and outcomes never appear as fake user text.

## Findings from live runs

These are things unit tests couldn't catch. Each one changed the code, and each is now covered by an eval.

1. **"Not yet."** After a successful confirm, the next turn's note said *"The user confirmed: … It succeeded."* The model replied "Not yet", and no booking ID. Replays with thinking summaries showed it had read "the user confirmed" as an unverified claim from the user. That happened in 3 of 3 replays. Reworded as *"Update from the booking system … (not from the user)"*, it was correct 3 of 3 times.
2. **Clock distrust.** In 3 of 12 runs, the model ignored the unlabelled `Current time:` line and asked the user for today's date. Once it said it had written that line itself. Labelled *"From the booking app (not the user): it is now …"*, it was correct 63 of 63 times.
3. **"Tomorrow" after midnight.** At 00:20 it booked the next calendar date in 2 of 3 runs. The prompt now says to ask which date between midnight and 4 a.m. It has asked 9 of 9 times since.

The common lesson: a `system` role alone doesn't tell the model who is speaking, so label every injected message with its source.

## Evals

Each case is a conversation run through the real API, agent, gateway and mock. Results below are for Claude Sonnet 5.5.

| Case | Must hold | First run | After fixes |
|---|---|---|---|
| `happy_path` | One proposal with the right args; confirm books exactly that | 3/3 | 9/9 |
| `slot_full` | 8pm is full: slots checked, nothing proposed until the user picks 7:30 | 2/3 | 9/9 |
| `ambiguous_restaurant` | Two outlets: nothing proposed until the user picks one | 3/3 | 9/9 |
| `user_declines` | Cancel: nothing booked, nothing proposed again | 2/3 | 9/9 |
| `duplicate_confirm` | Three confirm presses, two at once: one booking, executed once | 2/3 | 9/9 |
| `outcome_note` | After confirm, the model uses the booking ID from the note | 3/3 | 9/9 |
| `after_midnight_tomorrow` | At 00:20, "tomorrow" gets a question, not a proposal | 1/3 | 9/9 |

- **Checks never look at reply text.** They read only the audit log, the stored actions and the mock's bookings, step by step. Replies are kept in `evals/report.json` so people can read failing runs.
- **There's a scripted twin** in [test_evals_scripted.py](tests/test_evals_scripted.py) that runs in plain pytest. A well-behaved scripted model must pass every case. Scripts that reproduce failures seen live must fail the checks, which shows the checks actually catch them.
- **Limitation of `outcome_note`.** To check, from state alone, that the model believed the note, the user asks it to check the booking's status. The booking ID only appears in the note, so the check requires a `get_booking_status` call with that ID. A model that answered correctly from the note without calling the tool would fail this case. That hasn't happened yet: the tool was called in 9 of 9 runs.

## Known limitations

- **No auth.** Anyone with a session ID can chat in that session and confirm its actions.
- **In-memory state.** A restart loses everything, and the per-session lock only covers one process. The compare-and-set on actions is written so it maps to an `UPDATE … WHERE status = …` in Postgres.
- **Nothing recovers stuck actions.** A cancelled request is recorded as `UNKNOWN`, but if the process crashes mid-booking, the action stays `EXECUTING` forever.
- **How the idempotency key reaches Swiggy is undecided.** It could be a tool argument, MCP `_meta` or a header; the live adapter has to decide. If the live server doesn't honour the key, retrying an `UNKNOWN` action could book twice.
- **The mock is simple.** It ignores party size for availability, and its full and failing slots repeat every day.

## How to extend

**Add `cancel_booking`** (a write). The gateway and the agent loop don't change.
1. Server side: the tool and its schema. In the mock, add it to `SCHEMAS` and `call_tool`.
2. Validator: the `booking_id` must be one the server showed this session. Add a recorder on `get_booking_status` that stores booking IDs in the session's facts, the same "look before you write" pattern that `book_table` uses with slots.
3. A summary builder that uses those facts, e.g. "Cancel booking SWG-1001 at Phurr, Indiranagar, Sat 28 Nov 7:30 PM".
4. A registry entry: `ToolSpec("cancel_booking", RiskTier.WRITE, validator=..., summary=..., ...)`.
5. Reword `outcome_note` in `agent.py`. Its success text currently assumes the action was a new booking.
6. An eval case, plus a bad-behaviour script for the scripted twin.

**Add Swiggy Food.** Write an `MCPClient` that wraps both servers and routes each tool name to its server, prefixing names if they clash. The gateway takes one client and doesn't change. Add registry entries: reads, plus `place_order` as a WRITE with a summary that shows the items and the total. Payments stay out of scope. The system prompt is Dineout-specific and needs a section on food.

**Go live with Dineout.**
1. Implement `MCPClient` with the MCP SDK over streamable HTTP.
2. Map the server's errors to `MCPError` codes (`SLOT_UNAVAILABLE`, …). Leave timeouts and transport failures as plain exceptions, so the gateway records them as `UNKNOWN`.
3. Decide how the idempotency key is sent, and confirm the server honours it.
4. OAuth: per-user tokens held and refreshed by the adapter. Tokens never enter the model's context.
5. Swap the components in `default_components()` in `api.py`, add Postgres repos, then run `scripts.smoke` and the evals against staging.
