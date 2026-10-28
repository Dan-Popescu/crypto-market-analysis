# Improvement review and selected roadmap

For subsequent implementation, see the preserved [0.3.0 status](IMPROVEMENT_STATUS.md)
and [0.4.0 offline completion status](OFFLINE_STATUS.md). This document retains the
original dated proposals and selection rather than acting as the current checklist.

Review date: October 28, 2026 (Europe/Paris). Reviewed application revision:
`39725acd0ac1b2a764f6f17440cb132778155e77`, version 0.2.0.

The recommended next release has three priorities: repair demonstrated result
and evidence defects, establish one usable free-data route, and provide one
coherent review of the next purchase tranche. Fixed-policy cost sensitivity is
the next useful addition. More indicators and a larger interface are lower
priorities than these improvements.

This document records a review and proposed work. It implements none of the
application changes below. The existing implementation status remains a dated
delivery record; the findings here supplement its earlier verification.

## Goals and decision criteria

The target is gradual BTC/ETH spot accumulation over weeks and months, from
France. Historical context, 1/3/7/14-day purchase windows, 90/180-day outcomes and
exceptional 4/8-hour vigilance serve different questions. Regular purchases are
the comparison baseline. Holdings plus remaining cash, execution costs, waiting
and missing opportunities matter alongside acquisition cost.

EUR and USD require separate native-market analyses. USDT and USDC retain their
own identities. France purchase eligibility remains specific to a legal entity
and service; globally material analytical venues remain relevant regardless of
MiCA status. Ledgerly remains authoritative for actual transactions and holdings.
Personal budgets are optional and local.

Proposals were selected for direct decision value, evidence in the current code,
smallest useful scope, free-data feasibility and maintenance burden. No scoring
formula or estimated investment return was used to manufacture a ranking. Effort
labels S/M/L are relative engineering scope, not elapsed-time promises.

## Review method and actual agreement

Six agents first reviewed independently: statistics, product workflow, free
sources, reliability, market structure and a skeptical scope/value review. They
then criticized a common shortlist. The integrating reviewer inspected their
evidence and independently reproduced the principal behavioral findings.

The agreement is on **focused repairs → a proven native-market data route → a
minimal next-tranche brief**, followed by offline cost sensitivity. This is a
phased backlog, not a requirement to build every proposal before the first useful
report can ship. Reviewers did not independently reproduce every other's finding.

The critical round changed the selection:

- The skeptical reviewer rejected making the next delivery a broad ingestion,
  benchmarking and dashboard program. The selected first data route starts with
  BTC/EUR, then separately qualifies ETH/EUR, BTC/USD and ETH/USD. A bounded manual
  repair operation precedes automatic gap scheduling.
- The product reviewer kept a full scorecard and cross-venue panel out of the
  first three priorities. A brief can use the existing explicit plan configuration;
  a constructor is a small subsequent convenience, not a new planning platform.
- Reliability review required selected-market capture and late-data compaction
  policies. Increasing limits alone was rejected as a remedy.
- Source and statistics reviewers resolved an important disagreement: imported
  provenance does not permanently disqualify prospective evidence. Actual
  availability, frozen issuance and subsequent outcome capture determine temporal
  validity. Historical pre-ingestion origins remain retrospective. Cohorts retain
  their distinct dataset/mode lineage; neither `live` nor `import` alone proves
  or disproves prospective validity.

Independent working notes and cross-review addenda are local under
`.checks/improvement-review/`, excluded from Git. The evidence, selection and
acceptance criteria needed for this roadmap are retained here.

## Findings in the current application

### Demonstrated defects and resource-contract problems

| ID | Finding and user consequence | Evidence at the reviewed revision |
| --- | --- | --- |
| F1 | Evaluation Markdown omits the actual evaluation metrics, sample size and exclusions. Plan Markdown interrupts its table with terminal prose and omits waiting/funding exceptions. A reader can miss material underperformance. | `reporting.py:149`, `:166`, `:173`; evaluation fields originate in `plan_evaluation.py:154`. A real evaluation with two completed origins renders neither sample size nor underperformance metrics. |
| F2 | Waiting summaries count successful purchases only. A fully deferred EUR 100 tranche expiring after seven days into reserve reports zero maximum and total waiting. | `plan_evaluation.py:117`, `:130`, `:148`; the recorded expiry has a seven-day wait in `plans.py:346`. Reproduced with the existing flat-price fixture. |
| F3 | A late raw liquidity observation can replace a previously compacted whole-hour median with the late batch alone. Historical charts then change meaning. | `retention.py:37`, `:66`, `:102`. Values 1 and 3 compact to median 2/count 2; adding late value 100 and compacting yields 100/count 1. The combined median/count would be 3/3. |
| F4 | Capture caps apply across markets before the requested source/quote is selected; high-frequency observations can displace slower context. Issuance also archives observations its context/window calculation does not consume. | `storage.py:304`, `:316`, `:347`; `engine.py:80`; `application.py:104`; `evidence.py:75`. Reduced-limit SQL reproduces a stored requested-market row disappearing; a three-observation fixture drops MACRO behind two book observations. |
| F5 | Funding interval metadata is interpreted inconsistently. A valid seconds-only interval works in the canonical normalizer but is rejected by vigilance. | `evidence.py:34` versus `vigilance.py:176`. Metadata `funding_interval_seconds=28800` normalizes to 28,800 seconds while vigilance returns `unknown_native_convention`. |

Python paths in this table are relative to `src/crypto_market_analysis/`.

For F4, a serialized-size probe used 120,960 representative book observations:
three venues × two quotes × ten metrics × 288 snapshots/day × seven days, for
one asset. Their canonical envelope was **41,283,666 bytes**, above the 32 MiB
uncompressed evidence allowance, before adding candles. The probe repeats a
representative row set to measure serialization; it is not a full SQLite load
test or evidence that those live markets currently supply this volume. The
500,000-candle query-order issue was demonstrated with a reduced cap, not a
500,000-row benchmark. These boundaries matter when estimating operating capacity.

The repair should capture each operation's actual required evidence, preserve
per-family coverage and hard bounds, and fail explicitly if required inputs do
not fit. The selected market must be filtered before its cap. A late observation
must either remain explicitly unmerged/partial or trigger an exact supported
rebuild; medians cannot be reconstructed from prior medians and counts alone.
Do not delay F1/F2/F5 behind a larger resource benchmark or storage redesign.

Minor report polish can also address residual authored French validation errors
in `storage.py:93`, `:95`, `:199`, `:204`, contrary to the English-output policy.
These are lower priority than correctness and evidence integrity.

### Material limitations that should not be mislabeled as simulator defects

1. **A complete terminal valuation is not a qualified execution history.** Only
   three daily candles spanning 360 days can yield two `complete` 180-day
   comparisons and an overall `experimental` result, while the regular baseline
   bought zero because execution windows lacked observations. The simulator
   correctly avoids invented fills. Evaluation needs separate path coverage and
   evidence eligibility, rather than treating these scenarios as qualified
   performance samples (`plans.py:327`, `plan_evaluation.py:104`, `:142`).
2. **Most exact prospective endpoints are unobservable with hourly candles.**
   Issuance uses actual wall-clock seconds; the resolver needs a close exactly
   at issuance plus the horizon. Issuing at 00:30 remains endpoint-ambiguous even
   with perfect hourly coverage. This is an intentional honest limitation,
   asserted in tests, not an arithmetic error (`application.py:79`,
   `purchase_windows.py:105`, `tests/test_purchase_windows.py:55`).
3. **Incremental collection does not repair older gaps automatically.** Its
   cursor uses the newest candle minus an overlap. A missing hour at 7,200 seconds
   was not revisited when the next requested start was 10,800. Explicit `--days`
   can refetch, but there is no durable missing-range progress contract
   (`pipeline.py:43`, `storage.py:356`).
4. **Archive and live refresh are separate datasets today.** Imports use
   `data_mode="import"`; ordinary REST collection uses `live`. Joining an archive
   to recent REST is therefore a new explicit lineage contract, not something
   the existing importer already does (`history_import.py:69`, `engine.py:77`).
5. **The example's split and deferred policies are equivalent.** Both allocate
   half immediately and half at the same decline/deadline/cap/fallback. Identical
   outputs are expected. Distinct example presets would teach the choices more
   clearly; this is not an accounting error (`examples/accumulation-plan.json:28`).

## Selected delivery sequence

| Lot | Smallest useful outcome | Priority / effort | Dependency |
| --- | --- | --- | --- |
| C0 | Focused fixes F1–F5 with behavioral regressions | First; S changes, M capture/retention work | Existing interfaces; shared storage changes need one owner |
| P1 | One verified free native-market history and refresh route, with measured gaps and a bounded repair manifest | First data delivery; M | Source access, intended-use qualification and C0 evidence contracts |
| P2 | One read-only next-tranche brief from a pinned market and coherent snapshot | First product addition; M | Report fixes; real analytical claims require P1; policy preview can work offline |
| P3 | Fixed-policy fee/spread/slippage sensitivity with identical cashflows | Next addition; S/M | Existing simulator, corrected waiting and explicit caller assumptions |
| P4 | A separately named observable hourly endpoint and minimal prospective cohort protocol | Contract early; M; richer aggregation later | Versioned events, genuine subsequent observations, evidence qualification |
| P5 | Optional live purchase-cost and same-quote venue-disagreement panels | Conditional later; M | Working aligned feeds, product/fee/depth provenance and a demonstrated use |

### P1: make free data practically usable

Start with one actual BTC/EUR route, not all fifteen venues. Expand acceptance
separately to ETH/EUR, BTC/USD and ETH/USD, recording the source for each. The
same venue need not supply all four. Retain the global analytical catalogue and
material non-MiCA sources; prioritize their sampling by useful coverage and
request budget rather than using MiCA as an analytical filter.

The existing local archive parser is useful. A practical bootstrap must stage
real official files within resource bounds, record file/member hashes, actual
first/last hours, gaps and revisions, and then show a reproducible context report.
Target two years where actually available; never replace a failed 730-day
requirement with an unlabeled shorter interval.

Kraken documents at most 720 recent OHLC entries, including its unfinished bar;
older history cannot be recovered by changing `since`. Its published full archive
currently has five approximately 2 GB parts through June 30, 2026, plus quarterly
updates. This exceeds the importer's default 256 MiB file bound. Use a deliberate
bounded staging/import path with checksum and member provenance. Verify actual
overlap and gaps between archive and refresh, rather than assuming quarter names
establish continuous coverage. Documentation checked October 28, 2026:
[Kraken OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data),
[official archives](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data).

If that route needs archive-plus-REST composition, materialize an explicitly
named local research dataset with an audited append-as-import path. Preserve
source, asset, quote, hourly timeframe, actual original capture and materialization
times, raw hashes, overlap conflicts and deterministic version selection. A
separate local dataset can keep initial implementation small. Never introduce a
hidden cross-mode union or relabel archive candles as live. A native REST-only
history is an alternative if its actual coverage is demonstrated.

Keep historical reconstruction and prospectiveness separate: a backtest origin
before ingestion remains retrospective. A forecast frozen after the training
imports were available may qualify for future outcomes with genuine subsequent
captures and fixed definitions, in its own lineage cohort. Imported historical
training data does not require a second provider to become usable for future
research. Historical plan decisions cannot become prospective by issuing them now.

Persist missing ranges per dataset/mode/source/asset/quote/timeframe, with an
explicit request budget and resumable repair progress. Distinguish retention
limits, unsupported pairs, failed requests and unknown gaps. Infer no-trade gaps
only when supported by source evidence. Track last successful market event and
failure separately; one fresh pair must not conceal another stale pair. Measure
requests, duration, bytes/day, storage and missed cadence before permanent use.

**Acceptance:** one real native-market report works end to end; the other three
have independent coverage records. Repeating imports is idempotent. Inserting a
recent candle does not mark older holes complete. A repair interrupted by its
budget resumes. Optional context outages do not disable the accumulation plan.
If runtime access remains blocked, deliver runnable qualification commands and
mark real-data acceptance blocked, rather than claiming success.

**Cost boundary:** core context, accounting, sensitivity and journal operations
can use local data and public routes without paid API subscriptions. This does
not promise zero storage, bandwidth, electricity or hosting cost, complete free
historical books, or unrestricted data rights. Public authentication, intended
use, French purchase eligibility and actual runtime access remain separate.
Coinbase's market-data terms include material sharing and AI-related provisions;
their applicability to the intended product/workflow must be qualified. This is
an existing source-selection question, not grounds to block calculations using
other qualified inputs. No user-specific contract was inspected:
[Coinbase market-data terms](https://www.coinbase.com/legal/market_data), updated
August 7, 2026, checked October 28, 2026.

### P2: make the next purchase review understandable

Provide one CLI/MCP operation using one captured snapshot and an explicitly
pinned source/native quote. Show the observed anchor and age, long-context
coverage, purchase-window support, next funded tranche, fees, immediate/deferred
amounts, free versus locked cash, thresholds, deadline and fallback. Missing
inputs have visible reasons. The brief describes declared choices; it does not
choose a policy from experimental frequencies or produce a buy score.

Keep current observed conditions, hypothetical future policy states, and completed
historical outcomes distinct. Show paired holdings-plus-cash differences against
regular purchases only when both outcomes are comparable. A future 90/180-day
outcome remains pending. A protected cash floor is different from the existing
cap on cash locked in dip targets.

A small non-interactive constructor can follow: ISO dates, explicit monthly
month-end convention, decimal cash amounts, recurrence and fee assumptions,
emitting the existing schema. Preview funding without requiring market prices.
Flag policies that are equivalent. Do not add an allocation interview, holdings
integration or a new website as prerequisites.

**Acceptance:** every analytical section shares market identity, cutoff and input
reference. Stale prices suppress current price-based interpretation. A user can
see that EUR 100 cannot fund EUR 100 notional plus quote fees. Pending targets show
their amount, threshold, deadline and fallback. EUR/USD requests remain separate.

### P3: test economic robustness before adding timing complexity

Use the existing engine on a tiny caller-declared low/base/high cost grid. Fix
the policy, contributions, origins and terminal dates; vary fee/spread/slippage
assumptions only. Show net units, fees, reserve, waiting and paired wealth
differences, including reversals where extra split-purchase fees erase a price
discount. Costs remain assumptions unless actually measured.

An optional bounded flat/rising/prolonged-falling/dip-and-recovery panel can
illustrate opportunity cost. These are constructed scenarios without assigned
probabilities, kept outside observed market storage. Do not optimize thresholds
against the displayed scenarios or presume recovery.

**Acceptance:** independent fixed-fee hand calculations reconcile all cash;
identical inputs yield reproducible comparisons; changing costs can visibly
reverse a small apparent gain. A never-triggered target in a rising scenario
shows the retained cash and missed exposure. No new API is required.

### P4: make future learning possible without manufacturing evidence

Freeze a new named/versioned observation convention, for example the first
completed hourly mark at or after the deadline with an explicit maximum offset.
Keep the exact-deadline event unknown when hourly bars cannot observe it. Show
mark time and offset. Old issued records retain their original definitions.

Do not score an old anchor-relative frequency against a newly shifted endpoint.
Forecast and outcome definitions must match before probability scoring. Start
with an inspectable local journal summary: issued, mature, resolved, ambiguous,
missing, pending and excluded counts; no duplicate samples from repeated outcome
appends. Group by source, asset, quote, dataset/mode, rule and event convention.
Define first-observed versus revised outcomes and overlap handling explicitly.

Plan performance aggregates require execution-path evidence, not just a terminal
price. Preserve every scenario for inspection, but separate qualified performance
samples from missing-data simulations. Freeze eligibility before seeing returns.
Wholly prospective, retrospective and hybrid plans have distinct labels. A richer
scorecard follows actual records; implementation cannot accelerate the elapsed
90/180 days needed for genuine subsequent outcomes.

**Acceptance:** an issue at 00:30 receives a separately named observable mark with
its actual offset; exact 00:30 price stays unknown. Two outcome observations of
one issue remain one event. Sparse three-candle scenarios cannot count as two
qualified performance samples. Missing outcomes remain visible in denominators.
No predictive or accumulation advantage is claimed from the software delivery.

### P5: retain useful conditional additions without blocking the core

For actual venue choice, a future amount-specific panel can estimate quantity
received for an explicitly stated budget. It needs fresh native-quote books,
amount-relevant ask coverage, product/fee-tier assumptions, minimum/step rules and
separate payment or optional withdrawal assumptions. Order-book trading and
instant/recurring-buy products may have different unknown charges. Top-of-book
alone cannot establish whole-order cost; a partial book cannot guarantee a fill.
No unconditional cheapest-platform ranking is justified if unknown fees can
reverse it. Purchase candidates stay separate from historical reference venues.

A same-quote venue-disagreement display can first serve data-quality diagnosis:
individual venue prices, spread, coverage, event/capture age and inter-source skew.
Start only after at least two useful aligned feeds exist. Retain outliers and
missing sources visibly; do not produce an implicit fair-value index, cross-quote
average or quorum-based purchase signal. Broader derivatives/context should remain
descriptive until a declared test shows incremental decision value.

## Other useful additions, deliberately below the selected core

| Addition | Bounded useful version | Why later |
| --- | --- | --- |
| Backup and offline replay | SQLite backup/restore plus checked assessment replay; packaged build/lock provenance | Strong operational value as the journal grows; no replacement database needed |
| ECB policy calendar | Official policy meetings/press conferences, correct European timezone, schedule versus actual publication | Relevant to EUR context; never infer price direction or an undocumented release time |
| FX and stablecoin basis explanation | Separate dated EUR/USD reference and directly observed stablecoin/fiat basis with explicit transformation provenance | Requires defensible common endpoints; informational FX is not an executable rate |
| Small usable derivatives set | Known native OI/counting units, funding periods/history and bounded 4h/24h comparisons on one or two sources | Repair F5 now; broader adapters and EUR-adjacent native-USDT context need measured coverage |
| Shared BTC/ETH budget envelope | Validate dated commitments against one optional local cash budget without summing asset quantities | Useful if both plans share funding; should not become a second portfolio/tax ledger |

The [ECB official calendar](https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html)
distinguishes monetary-policy and other meetings (checked October 28, 2026).
These are candidate additions, not integrations or live data verified by this
review. Existing source and prospective-availability constraints still apply.

Defer new forecast/ML models, automatically optimized thresholds, composite buy
scores, precise crash probabilities, comprehensive liquidation maps, historical
full-book replay, automated comprehensive ETF scraping, a full web application,
embedded LLM, exchange keys, Ledgerly synchronization and external notifications.
They add dependencies before resolving the current data and decision workflow.
Do not lower sample gates or count overlapping windows as independent merely to
populate an attractive report.

## Safe parallel execution if this roadmap is implemented

1. **Repair wave:** independent owners can address report rendering, waiting
   metrics, funding normalization and late-compaction guards. One owner controls
   shared storage/capture/application contracts and integrates related tests.
2. **First useful-data/product wave:** once lineage and capture interfaces are
   fixed, source/bootstrap qualification can proceed alongside the local brief
   adapter and offline cost sensitivity. The latter can use synthetic fixtures;
   real-data acceptance stays separate. Constructor work is optional and bounded.
3. **Evidence wave:** settle the observable endpoint/cohort protocol before new
   prospective records rely on it. Qualify execution paths and add a minimal
   journal summary; richer statistics follow real records.
4. **Conditional wave:** only then consider live venue cost/disagreement panels
   and additional descriptive context, each with its own data and acceptance gate.

Use isolated worktrees, one owner per shared file/schema, frozen interfaces and
behavioral checks based on independently calculated expectations. Each wave can
produce a reviewable result. Collector activation, source downloads at large
scale, deployment and messaging are distinct operational actions, not side
effects of this review.

## Fresh verification and reproducible principal probes

The integrating reviewer ran the unchanged application at the exact revision
above on October 28, 2026:

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/implementation/mpl-cache uv run --frozen python tools/check.py
```

Result: offline lock coherent (73 resolved packages), Ruff clean, **244 tests
passed in 28.45 seconds**. Local network-socket permission was used for local
MCP transport tests. This suite passing does not cover every finding above; the
following probes expose cases absent from the current regression suite.

Run from the repository root with its installed environment:

```bash
.venv/bin/python - <<'PY'
import runpy
from tempfile import TemporaryDirectory
from pathlib import Path
from crypto_market_analysis.reporting import render_accumulation_report
from crypto_market_analysis.models import Observation
from crypto_market_analysis.storage import Store
from crypto_market_analysis.retention import compact_liquidity
from crypto_market_analysis.evidence import funding_period_seconds
from crypto_market_analysis.vigilance import _derivatives

x = runpy.run_path('tests/test_plan_evaluation.py')
D = x['DAY']
r = x['evaluate_plans'](x['config'](), x['bars'](), 550*D,
                        origins=[0, 180*D])
o = r['origins'][0]['outcomes']['180']['deferred']
print('waiting', o['uninvested_reserve'], o['maximum_waiting_seconds'])
sparse = [c for c in x['bars']() if c.open_time in {-D, 179*D, 359*D}]
r = x['evaluate_plans'](x['config'](), sparse, 360*D,
                        origins=[0, 180*D])
print('sparse', r['status'], r['sample_size'],
      [z['outcomes']['180']['regular']['net_quantity'] for z in r['origins']])
report = render_accumulation_report(r, 'evaluate-plans')
print('markdown', 'sample' in report.lower(), 'underperformance' in report.lower())
w = runpy.run_path('tests/test_purchase_windows.py')
r = w['resolve_window'](w['hours'](50), 100, 0, 1800, 1, .05, 180000)
print('endpoint', r['endpoint_below_anchor']['state'], r['coverage']['missing_hours'])

t = 10*86400
def obs(ts, value, observed=None):
    return Observation('fixture', 'BTC', 'depth_bid_1pct', ts,
        ts if observed is None else observed, value, 'USD', 'USD',
        {'coverage_complete': True, 'band_percent': 1})
with TemporaryDirectory() as d:
    s = Store(Path(d)/'probe.sqlite')
    s.put_observations([obs(t+100, 1), obs(t+200, 3)])
    compact_liquidity(s, now=20*86400)
    a = s.observations(as_of=20*86400, days=30)[0]
    s.put_observations([obs(t+300, 100, 20*86400+1)])
    compact_liquidity(s, now=20*86400+2)
    b = s.observations(as_of=20*86400+2, days=30)[0]
    print('retention', (a.value, a.metadata['sample_count']),
          (b.value, b.metadata['sample_count']))
f = Observation('fixture', 'BTC', 'funding_rate_actual', 3600, 3600,
    .0001, 'rate', 'USDT',
    {'symbol': 'BTC/USDT:USDT', 'funding_interval_seconds': 28800})
v, _ = _derivatives([f], 3601, {'derivatives_max_age': 7200})
print('funding', funding_period_seconds(f.metadata), v[0]['status'])
PY
```

Observed at the reviewed revision:

```text
waiting 100 0
sparse experimental 2 ['0', '0']
markdown False False
endpoint ambiguous 0
retention (2.0, 2) (100, 1)
funding 28800 unknown_native_convention
```

The probes use synthetic in-memory data or temporary SQLite databases. No new
application tests or fixes were introduced. No live source collection, download,
permanent job, order, account connection, deployment or external message was
performed. Official documentation was researched through the browsing tool;
that does not prove runtime API access. The managed cloud still restricts source
hosts. No investment advantage has been established by this review.
