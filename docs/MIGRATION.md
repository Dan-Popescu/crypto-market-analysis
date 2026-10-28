# English repository and output migration

The implementation brief adopted on October 28, 2026 makes English canonical
for first-party repository content and software output. This supersedes earlier
French-output requirements. It applies to documentation, examples, code names,
comments, prompts, CLI and MCP text, reports, charts, public schema values, new
commit messages and pull request text. Existing Git history is preserved.

Raw external evidence, provider identifiers, instrument symbols, official names
and source URLs retain their original content. An English explanation must keep
externally supplied text distinguishable from application-authored prose.

## Public contract inventory

| Surface | Migration contract |
| --- | --- |
| CLI command and Python package | Keep `crypto-market-analysis` and `crypto_market_analysis`. |
| Configuration and data locations | Keep `OBSERVER_*`, `observer.toml` and `observer.sqlite`; no automatic configuration or database rename. |
| MCP authentication | Keep the `observer:read` scope and existing OAuth setting names. |
| Existing CLI/MCP operations | Keep technical operation names; translate authored help, descriptions, errors and responses. |
| Analysis bundle | Version the canonical English-output bundle as schema `2.0`; consumers must check the schema version. |
| `daily_trend` and `weekly_trend.direction` | Canonical output values are `up`, `down` and `flat`, replacing `hausse`, `baisse` and `stable`, respectively. |
| Forecast event keys | Keep `red`, `up`, `flat`, `down_barrier` and `up_barrier`. `red` names an endpoint comparison, not a calendar-candle color. |
| Legacy unknown-unit value | Accept `inconnu` only as a legacy input alias; authored English output uses `unknown`. |
| Synthetic JSON examples | Translate authored strings deterministically while preserving keys, numbers, timestamps and synthetic provenance. |
| Validation evidence | Preserve the original author's reported results in `VALIDATION.md` separately from independently executed import checks in `IMPORT.md`. Translation is not a new test run. |

This document defines the coordinated migration contract. Runtime conformance
must be verified by the implementation checks; documentation translation alone
does not validate the application. The migration introduces no paid data source,
public deployment, account connection or new analytical feature.

## Existing local data and clients

Keep local databases and settings in place. Consumers that previously compared
French direction values must update those comparisons for schema `2.0`.
Retain the distinction between legacy input compatibility and canonical output:
accepting an older value does not make it a supported new output value.
Missing observations remain missing, and `live`, `import` and `demo` data remain
separate. Do not rewrite external evidence or personal runtime records merely
to translate their language.
