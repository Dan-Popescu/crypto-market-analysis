# Using the data from an AI client

ChatGPT, Claude or another MCP client can explain the shared engine's bounded results. Collection, calculations and charts do not call a language model. Start collection separately when live data is needed; MCP lookup/calculation tools do not issue assessments, append outcomes, import files or start persistent watches.

## Local setup and reproducible protocol calls

`examples/mcp-config.json` shows the `mcpServers` STDIO format. Replace the absolute paths, run `uv sync --frozen` in the project and ensure the client can find `uv`. Add explicit `--mode demo --data-dir /absolute/path/to/data/demo-docs` arguments for synthetic work. First create the fixed fixture with the demo command in [ACCUMULATION.md](ACCUMULATION.md).

This local Python example uses the installed MCP SDK over STDIO. Run from the repository root after creating `data/demo-docs`; it performs no public network request. It sends the actual plan object, not a path, to MCP:

```bash
uv run --frozen python - <<'PY'
import asyncio
import json
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    plan = json.loads(Path("examples/accumulation-plan.json").read_text())
    common = {"asset": "BTC", "source": "demo", "quote": "EUR", "as_of": 1793145600}
    calls = [
        ("get_analysis_bundle", common),
        ("get_source_catalog", {}),
        ("get_market_context", {**common, "lookbacks": [7, 30, 365, 730]}),
        ("get_purchase_windows", {**common, "horizons": [1, 3, 7, 14], "thresholds": [0.02, 0.05]}),
        ("compare_purchase_plans", {"config": plan, "as_of": common["as_of"], "validation": "retrospective"}),
        ("evaluate_purchase_plans", {"config": plan, "as_of": common["as_of"], "validation": "known_at"}),
        ("get_vigilance", {**common, "horizon_hours": 4}),
    ]
    issued = Path("output/demo-issued.json")
    if issued.exists():
        calls.append(("get_assessment", {"assessment_id": json.loads(issued.read_text())["assessment_id"]}))
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "crypto_market_analysis", "serve", "--transport", "stdio",
        "--mode", "demo", "--data-dir", "data/demo-docs",
    ])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                assert not result.isError, name
                print(name, "error=" + str(result.isError))
                for content in result.content:
                    if content.type == "text":
                        print(content.text[:1000])

asyncio.run(main())
PY
```

`1793145600` is October 28, 2026 at 00:00 UTC, matching the CLI fixture. An insufficient strict result or degraded vigilance result can be valid output; inspect the reason and coverage. To render a native image, call `render_chart` with `{"kind":"context","asset":"BTC","source":"demo","quote":"EUR","as_of":1793145600}`. For `kind: "plans"`, also pass the complete `plan_config` object and `validation: "retrospective"`.

## Tool contract

All registered tools declare read-only behavior. Chart rendering uses temporary files to return an image; it does not publish a report. CLI `issue` and `observe` are the explicit journal writes. A CLI vigilance `--state-key` writes a local transition; `get_vigilance` does not.

| Tool | Purpose |
| --- | --- |
| `get_accumulation_brief` | One captured next-tranche review with explicit source/native EUR or USD quote, or a matching plan object. |
| `construct_purchase_plan` | Pure UTC recurrence constructor; returns normalized schema-1 JSON without saving it. |
| `preview_purchase_funding` | Price-free dated funding constraints and fees; no account balance or assumed fill. |
| `compare_cost_sensitivity` | One to three caller-declared cost cases on frozen policies and cashflows, without optimization. |
| `get_journal_summary` | Bounded issued-event cohorts with first definite outcomes and pending/missing/excluded denominators; no performance score. |
| `get_journal_scorecard` | Bounded descriptive event rates and qualified paired 90/180-day plan outcomes; distinct cohorts and overlap exclusions. |
| `replay_assessment` | Verify an existing original by ID and recompute its supported frozen calculations without appending outcomes. |
| `preview_shared_budget` | Price-free BTC/ETH commitments against one explicit dated native-quote envelope. |
| `calculate_purchase_costs` | Hypothetical amount-specific spot costs from explicit local books, fees and trading rules. |
| `calculate_venue_disagreement` | Fresh aligned same-quote dispersion with stale, missing and outlier sources visible. |
| `get_quote_context` | Explicit indicative FX and stablecoin basis with native input and transformation provenance. |
| `get_derivatives_context` | Native funding/OI 4h/24h comparisons with known-at endpoints, conventions and missingness. |
| `get_stored_derivatives` | Build native derivative inputs from bounded stored vintages and report consumed evidence. |
| `get_stored_quotes` | Read stored native reference/spot inputs, retaining date-only and unknown clocks. |
| `get_stored_venue_costs` | Use retained books with explicit fee, product and rule assumptions. |
| `get_stored_venue_disagreement` | Compare eligible retained native books with visible unknown/stale sources. |
| `qualify_sources` | Read-only independent native-market history, gap, freshness and resource qualification. |
| `get_analysis_bundle` | Establish as-of time, mode, reference market, observations, source coverage and limitations. |
| `get_market_context` | Descriptive context, complete UTC periods and explicit lookback coverage. |
| `get_purchase_windows` | Experimental 1/3/7/14-day endpoint/touch frequencies, reference rates, support and expiry. |
| `compare_purchase_plans` | Deterministic regular/split/deferred simulations, decimal accounting, reserve and 90/180-day outcomes. |
| `evaluate_purchase_plans` | Paired chronological evaluation, underperformance and descriptive dispersion; strict empty coverage stays empty. |
| `get_assessment` | Read a previously issued immutable assessment and appended outcomes by `assessment_id`. |
| `get_vigilance` | Read-only 4/8-hour observed fragility with reasons, contradictions, missing coverage and expiry. |
| `get_source_status` | Collection failures, configured venues, freshness and entity-specific unknowns. |
| `get_source_catalog` | Documented endpoint capabilities, rights limitations and dated French entity evidence; no live availability claim. |
| `get_request_schema` | On-demand nested request contracts, limits and synthetic executable examples; use `tool_name: "list"` for discovery. |
| `get_http_accounting` | Observed retained-run HTTP attempts, errors, latency and decoded body bytes; unknown instrumentation stays unknown. |
| `get_auto_repair_status` | Current configured-dataset repair checkpoints without planning, applying or recovery. |
| `get_analysis_dossier` | Explicit native market contexts and requested stored sections/calendar in one read transaction, with shared cutoff and watermark. |
| `get_installation_qualification` | Portable local native-market qualification report; this MCP surface forbids refresh. |
| `get_analysis_changes` | Compare one native selection at two known-at cutoffs; distinguish market events, analytical metric changes, revisions and collection issues. |
| `get_analysis_readiness` | Claim-level support, missing inputs and nonexecuting next actions from one snapshot; rights and entity verification remain independent. |
| `get_plan_robustness` | Fixed origins, declared costs and missing-hour stresses against regular purchases, with qualified/excluded denominators and descriptive stability. |
| `get_history_backfill_status` | Current bounded Coinbase/Bitstamp import job checkpoints; no planning, capture, retry or recovery. |
| `get_result_page` | Bounded frozen details by handle, selection/content hashes and JSON pointer; inspect expiry and fragment metadata. |
| `get_metric_series` | Discover actual source, native quote, unit and dimension identities before requesting history. |
| `get_dataset_status` | Configured dataset identity, captures, conflicts, attempts and repair state, without recovery or initialization. |
| `get_collection_history` | Paginated historical runs and current independent source/task scheduler clocks, with explicit mode classification. |
| `get_storage_status` | Current SQLite/WAL/archive/family usage and conservation/admission settings. |
| `get_upcoming_calendar` | Known-at upcoming sourced calendar records, retaining unknown publication clocks. |
| `get_forecast`, `get_evaluation` | Existing experimental 1–60-day historical frequencies and chronological evaluation. |
| `get_metric_history`, `explain_metric` | Bounded sourced observations and interpretation definitions. |
| `render_chart` | Native MCP image for existing charts or `context`, `windows` and `plans`. |

Arguments are bounded: up to 16 lookbacks, 16 purchase horizons, 12 decline thresholds and 24 evaluation origins. Plan/policy inputs must be finite JSON objects within documented limits. No arbitrary SQL, code execution or unrestricted file path interface is exposed. The endpoint/rights catalogue is available through CLI `sources` and MCP `get_source_catalog`. Assessment lookups summarize frozen evidence and return at most 20 appended outcomes; full evidence stays in the local journal.

Dictionary replies include `_mcp_result` with frozen selection/content hashes,
expiry and explicitly omitted paths. Use `get_result_page` for complete details;
an expired handle requires a fresh calculation. Replies fit 32 KiB including
the SDK's text and structured copies; chart images remain separate. CLI results
remain full. See [the operating guide](EFFICIENCY.md) for history cursors, cache
limits, schema discovery and conservation policy.

For a next-tranche review, call `get_accumulation_brief` with
`{"asset":"BTC","source":"demo","quote":"EUR","as_of":1793145600}` in the
synthetic server, optionally adding `plan_config` as an object. For sensitivity,
provide `config` and `cost_cases`, such as
`[{"name":"declared"},{"name":"higher-fee","fees":{"fixed":"2"}}]`.
The first cost case is an arithmetic reference, never a recommendation.
`get_journal_summary` accepts `limit` from 1 through 100, selects the most recent
originals and reports outcome truncation. Event counts are not independent sample
counts. New observable hourly endpoints remain separate from exact endpoints and
are not scored against unmatched historical forecasts. See [IMPROVEMENTS.md](IMPROVEMENTS.md).

The five new request-based calculations take `request` as a bounded JSON object
and optional integer `as_of`; use the corresponding JSON examples in
[offline workflows](OFFLINE_WORKFLOWS.md). The request's explicit data mode is
authoritative. `get_journal_scorecard` takes the same cutoff/limit as the journal
summary; `replay_assessment` takes `assessment_id`. No backup/restore operation
or filesystem-path input is exposed through MCP. Native USDT derivatives remain
separate from USD analyses.

An assistant request can be: “Compare regular, split and deferred BTC purchases using this synthetic EUR plan. Start with coverage and the same as-of time. Show holdings plus reserve at 90 and 180 days, underperformance and waiting costs. Distinguish observations, assumed fills and insufficient evidence. Explain contradictory context and never invent a calibrated probability.”

## Remote clients

Streamable HTTP uses `/mcp`; local HTTP binds to `127.0.0.1` by default. Remote use requires HTTPS, an external compatible OAuth provider and `observer:read` scope; see [OPERATIONS.md](OPERATIONS.md). Add that URL as a custom client connection only after configuring the service. The repository does not deploy a service or connect any account automatically.

Client documentation: [custom MCP in ChatGPT](https://developers.openai.com/api/docs/guides/custom-mcp-server); [Claude remote MCP connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp). Actual access depends on client/provider registration support and account permissions.

Version 0.5.0 stored tools take a bounded `request` and optional integer `as_of`.
Explicit sources, native quote and data mode determine each read. They do not
fetch data, repair gaps, issue assessments or expose paths. Capture and the local
workflow remain explicit CLI operations; see [local data connections](DATA_CONNECTIONS.md).
