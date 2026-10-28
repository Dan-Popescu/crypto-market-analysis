# Prototype import — October 28, 2026

## Origin and scope

Source: `crypto-observer-v0.1.0.zip`, supplied by Dan.
The project, Python package and command are renamed to Crypto Market
Analysis, `crypto_market_analysis` and `crypto-market-analysis`.
Documentation references, tests, MCP configuration, Docker files and the
dependency lockfile follow this rename. The configuration names `OBSERVER_*`,
`observer.toml`, `observer.sqlite` and the `observer:read` scope remain unchanged.

This pass prepares the repository for local and cloud development.
The prototype's market calculations and forecasts remain unchanged.
Existing experimental features retain their limitations.

## Import checks

Checks independently executed on October 28, 2026 on Windows with Python 3.12.3,
in an isolated environment containing the locked dependencies:

- `uv run --frozen python tools/check.py`: passed.
- `uv lock --check --offline`: lockfile consistent.
- Ruff over `src`, `tests` and `tools`: no errors.
- Complete suite: **71 tests passed** in 20.79 seconds.
- Local STDIO and HTTP MCP protocols, and native image responses: included in the tests.
- `uv build --wheel --offline`: renamed distribution built.

The first MCP test attempt in the restricted environment did not finish and
was interrupted. The complete suite then passed with the required access to
local processes and connections. No MCP code change was necessary.

No public collection, AI account connection, Ledgerly integration or Docker
installation was performed during this import. This pass therefore does not
validate their real-world operation.

## Repository contents

The repository retains code, tests, documentation and synthetic examples.
The seven real-market reports and charts from the ZIP, the supplied wheel,
databases and Python environments are excluded from version control.
The original author's validation report remains separate in [VALIDATION.md](VALIDATION.md).

The repository was initially private. On October 28, 2026, Dan then chose a
public repository to facilitate pipeline trials and GitHub checks when useful.
GitHub Actions remains disabled at this stage; this visibility change adds no
workflow. A shared verification command is provided for PCs and Codex cloud;
its results must be recorded before every merge.

## Previously observed limitation

The freshness marker for each liquidity measurement uses the general market
cadence and can display a 30-minute-old measurement as fresh even though
liquidity collection is scheduled every five minutes. This was reproduced
while inspecting the prototype; a separate task must correct it. The import
does not change this calculation.
