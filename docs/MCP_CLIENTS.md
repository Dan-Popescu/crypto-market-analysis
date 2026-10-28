# Using the data from an AI client

The engine does not select a model or charge for LLM calls: ChatGPT, Claude or another MCP client writes the analysis from the tools. Collection must be started separately (`uv run crypto-market-analysis run`).

## Local client

`examples/mcp-config.json` contains a STDIO example for clients accepting the `mcpServers` format. Replace both absolute paths and check that `uv` is accessible to the client. First run `uv sync --frozen` in the project. The client then starts the server. Explicitly choose `--mode demo` to use synthetic data.

## ChatGPT on the web or Claude.ai

The Streamable HTTP server uses the `/mcp` path. Remote use requires an HTTPS domain and the OAuth provider described in [OPERATIONS.md](OPERATIONS.md), followed by adding that URL as a custom MCP connection in the client. Access depends on account permissions. The supplied examples do not expose a service publicly or connect an account automatically.

Client documentation: [custom MCP in ChatGPT](https://developers.openai.com/api/docs/guides/custom-mcp-server); [Claude remote MCP connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## Available tools

| Tool | Purpose |
| --- | --- |
| `get_analysis_bundle` | Timestamped evidence, reference market, observations, scenarios and limitations |
| `get_source_status` | Coverage, access errors and freshness |
| `get_forecast` | Experimental historical frequencies over 1–60 days |
| `get_evaluation` | Evaluation using information known at each date, or explicitly selected retrospective exploration |
| `get_metric_history` | Bounded series with units, currencies and provenance |
| `explain_metric` | Plain definitions and interpretation limits |
| `render_chart` | Charts returned as native MCP images |

Example request: “Use Crypto Market Analysis for the weekly BTC and ETH brief. Start with source coverage. Give the three most useful changes for the next 7, 21 and 60 days, contradictory observations and what would change the interpretation. Plot the weekly chart and explain technical terms. Separate observations from assumptions; invent no data or probabilities.”

CLI Markdown briefs are deterministic summaries. The JSON bundle provides more evidence for further exploration with an AI. V1 forecasts use historical prices and volatility; macro, network, options and leverage data enrich descriptive and conversational analysis, without being presented as validated predictive factors.
