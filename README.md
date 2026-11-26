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
- **Async throughout.** MCP client and repos are async so the live MCP SDK and a
  Postgres driver drop in without touching the gateway.
- **Schemas come from the server.** `MCPClient.list_tools()` is the source of
  truth for tool schemas; the registry only holds policy and acts as an
  allowlist (default deny).

## Run tests

```
uv run pytest
```
