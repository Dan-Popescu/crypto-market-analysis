# Crypto Market Analysis

Explain work to Dan in simple, concise French. Write code in English.

## Scope

This repository contains the independent BTC/ETH market analysis tool imported
from Crypto Observer v0.1.0. Ledgerly remains the source of portfolio transactions,
holdings and acquisition costs. See README.md for usage and docs/SPEC.md for scope.

Preserve source provenance, UTC timestamps, native quote currencies, data versions,
and the separation of live, imported and synthetic observations. Missing data
remains unknown. Keep experimental forecasts explicitly labelled as experimental.
Preserve the OBSERVER_* environment variables and observer.toml configuration
names until a deliberate compatibility migration is requested.

## Verification and pull requests

Install development dependencies with `uv sync --frozen --extra dev`.
Run `uv run --frozen python tools/check.py` in the working environment: either
the local PC or Codex cloud. This runs the existing offline tests and code checks.
Report the exact tested commit, commands, results and any unavailable checks in
the pull request. Verify the final version again after code or merge changes.

GitHub Actions is intentionally disabled for this repository. Keep checks inside
the working environment; adding or enabling remote workflows requires Dan's
explicit request. Tests use temporary data and local MCP transports; public
collection, scheduled jobs and live deployments require their own task scope.

## Data and publication

Commit source code, tests, configuration examples and synthetic fixtures.
Keep credentials, personal records, portfolio exports, databases, virtual
environments and generated reports outside version control. Existing real-market
reports from the original ZIP are local examples and are excluded by .gitignore.
Never execute orders, sign wallet operations or transfer funds.

## Delegation

Use gpt-6.1-sol with High reasoning for delegated work unless Dan explicitly
requests another model. Use at most Extra High when justified; never Max or Ultra.
Delegate independent bounded work only when it materially helps progress.
