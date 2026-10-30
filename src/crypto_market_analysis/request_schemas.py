"""Small on-demand request contract registry with synthetic executable examples.

Schemas document structural contracts; runtime validators still enforce temporal,
cashflow, identity and cross-field invariants. No provider fees are inferred.
"""
from __future__ import annotations

from copy import deepcopy

from .market_requests import contracts as _market_contracts
from .plans import RULE_VERSION as _PLAN_RULE_VERSION
from .validation_protocol import freeze_validation_protocol as _freeze_validation_protocol


def obj(properties, required=(), **extra):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False, **extra}


def integer(maximum=253402300799, minimum=0, **extra):
    return {"type": "integer", "minimum": minimum, "maximum": maximum, **extra}


def enum(*values, **extra):
    return {"type": "string", "enum": list(values), **extra}


def array(items, maximum, minimum=0, **extra):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum, **extra}


TEXT = {"type": "string", "minLength": 1, "maxLength": 100}
DECIMAL = {"type": "string", "pattern": r"^(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?$",
           "description": "Exact nonnegative decimal string; runtime checks enforce positive values where required."}
MODE = enum("live", "import", "demo")
ASSET = {"type": "string", "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,31}$", "maxLength": 32,
         "description": "Explicit CCXT asset code; EUR/USD are quotes, and syntax does not prove economic identity or provider support."}
QUOTE = enum("EUR", "USD")
FEES = obj({"rate": {**DECIMAL, "default": "0"}, "fixed": {**DECIMAL, "default": "0"},
            "asset": enum("quote", "base", default="quote")},
           description="Caller-declared hypothetical plan costs. Omitted zero defaults are assumptions, not exchange fees.")
EXECUTION = obj({**{key: {**DECIMAL, "default": default} for key, default in
                   (("spread_bps", "0"), ("slippage_bps", "0"), ("quantity_step", "0.00000001"), ("price_step", "0.00000001"))},
                 "timeframe": enum("1h", "1d", default="1h"),
                 "max_price_age_seconds": integer(86400, default=3600),
                 "assumption": enum("historical_configured_costs", default="historical_configured_costs")})
POLICY_COMMON = {"max_wait_days": integer(90, 1, default=7),
                 "fallback": enum("buy_at_deadline", "reserve", default="buy_at_deadline"),
                 "max_deferred_reserve_quote": {"anyOf": [DECIMAL, {"type": "null"}], "default": None}}
POLICIES = obj({"split": obj({**POLICY_COMMON, "initial_fraction": {**DECIMAL, "default": "0.5"},
                 "levels": array(obj({"decline": DECIMAL, "fraction": DECIMAL}, ("decline", "fraction")), 8, 1,
                                 default=[{"decline": "0.05", "fraction": "0.5"}])}),
                "deferred": obj({**POLICY_COMMON, "fraction": {**DECIMAL, "default": "0.5"},
                                 "decline": {**DECIMAL, "default": "0.05"}})})
ACTION = obj({"time": integer(2**63 - 1), "amount": DECIMAL}, ("time", "amount"))
PLAN = obj({"schema_version": {"type": "integer", "const": 1}, "asset": ASSET, "quote": QUOTE,
            "source": TEXT, "data_mode": {**MODE, "default": "live"}, "start_time": integer(2**63 - 1),
            "contributions": array(ACTION, 2000), "schedule": array(ACTION, 2000), "fees": FEES,
            "execution": EXECUTION, "plans": POLICIES,
            "starting_holdings": obj({"quantity": {**DECIMAL, "default": "0"},
                                      "cost": {"anyOf": [DECIMAL, {"type": "null"}], "default": None}})},
           ("schema_version", "asset", "quote", "source", "start_time", "contributions", "schedule"))
CONSTRUCTOR = obj({"asset": ASSET, "quote": QUOTE, "source": TEXT, "data_mode": {**MODE, "default": "live"},
                   "start_date": {"type": "string", "format": "date-time", "description": "Explicit UTC whole-second ISO timestamp."},
                   "recurrence": obj({"frequency": enum("daily", "weekly", "monthly"), "count": integer(181, 1),
                                      "month_end_rule": enum("clip", "skip")}, ("frequency", "count")),
                   "contribution_amount": DECIMAL, "purchase_amount": DECIMAL, "fees": FEES,
                   "execution": EXECUTION, "plans": POLICIES},
                  ("asset", "quote", "source", "start_date", "recurrence", "contribution_amount", "purchase_amount"),
                  description="Monthly recurrence requires month_end_rule. Expanded schedule must fit 180 days.")
ASSUMPTION_FIELDS = ("fee_rate", "fee_step", "fixed_fee_quote", "payment_cost_quote", "withdrawal_cost_quote",
                     "withdrawal_cost_base", "quantity_step", "price_step", "min_quantity", "min_notional")
ASSUMPTIONS = obj({**{key: {"anyOf": [DECIMAL, {"type": "null"}]} for key in ASSUMPTION_FIELDS},
                   "fee_currency": enum("quote", "base"), "input_reference": TEXT, "fee_tier": TEXT},
                  description="No fee defaults. Missing or null fields produce unknown cost/product rules, not zero costs.")
ELIGIBILITY = obj({"jurisdiction": TEXT, "legal_entity": TEXT, "service": TEXT, "input_reference": TEXT,
                   "status": enum("eligible", "ineligible", "unknown")},
                  ("jurisdiction", "legal_entity", "service", "input_reference", "status"))
SNAPSHOT_BASE = {"source": TEXT, "asset": ASSET, "quote": QUOTE, "data_mode": MODE,
                 "event_time": integer(), "captured_at": integer(), "input_reference": TEXT}
SNAPSHOT_REQUIRED = tuple(SNAPSHOT_BASE)
PURCHASE_SNAPSHOT = obj({**SNAPSHOT_BASE, "product": TEXT, "asks_complete": {"type": "boolean"},
                         "asks": array(obj({"price": DECIMAL, "quantity": DECIMAL}, ("price", "quantity")), 500),
                         "assumptions": {"anyOf": [ASSUMPTIONS, {"type": "null"}]},
                         "purchase_eligibility": {"anyOf": [ELIGIBILITY, {"type": "null"}]}},
                        (*SNAPSHOT_REQUIRED, "asks", "asks_complete"))
PRICE_SNAPSHOT = obj({**SNAPSHOT_BASE, "price": DECIMAL, "best_bid": DECIMAL, "best_ask": DECIMAL},
                     (*SNAPSHOT_REQUIRED, "price"))
VENUE_BASE = {"asset": ASSET, "quote": QUOTE, "data_mode": MODE,
              "sources": array(TEXT, 32, 1, uniqueItems=True),
              "max_age_seconds": integer(86400), "max_skew_seconds": integer(86400)}
VENUE_REQUIRED = tuple(VENUE_BASE)
PURCHASE = obj({**VENUE_BASE, "budget": DECIMAL, "snapshots": array(PURCHASE_SNAPSHOT, 32)},
               (*VENUE_REQUIRED, "budget", "snapshots"))
DISAGREEMENT = obj({**VENUE_BASE, "price_basis": enum("mid", "last", "best_ask"),
                   "outlier_threshold_bps": DECIMAL, "snapshots": array(PRICE_SNAPSHOT, 32)},
                  (*VENUE_REQUIRED, "price_basis", "outlier_threshold_bps", "snapshots"))
QUOTE_OBSERVATION = obj({"base": enum("EUR", "USD", "USDT", "USDC"), "quote": QUOTE, "value": DECIMAL,
                         "event_time": integer(), "observed_at": integer(), "source": TEXT,
                         "source_url": {"type": "string", "pattern": "^https://"}, "data_version": TEXT,
                         "data_mode": MODE, "price_type": enum("indicative_reference", "observed_spot")},
                        ("base", "quote", "value", "event_time", "observed_at", "source", "source_url", "data_version", "data_mode", "price_type"))
QUOTE_POLICY = {"max_age_seconds": integer(31536000, default=86400),
                "max_endpoint_skew_seconds": integer(31536000, default=300)}
QUOTES = obj({"data_mode": MODE, "fx_reference": {"anyOf": [QUOTE_OBSERVATION, {"type": "null"}]},
              "stablecoin_prices": array(QUOTE_OBSERVATION, 100, default=[]), **QUOTE_POLICY}, ("data_mode",))
DERIVATIVE_POLICY = obj({"max_point_age_seconds": integer(86400, 1, default=3600),
                         "endpoint_tolerance_seconds": integer(3600, default=900)})
OBSERVATION = obj({"source": TEXT, "asset": TEXT, "metric": TEXT, "event_time": {"anyOf": [integer(2**63 - 1), {"type": "null"}]},
                   "observed_at": {"anyOf": [integer(2**63 - 1), {"type": "null"}]},
                   "value": {"type": ["number", "null"], "minimum": -1e100, "maximum": 1e100},
                   "unit": {"type": ["string", "null"], "maxLength": 120}, "quote": {"type": ["string", "null"], "maxLength": 120},
                   "metadata": {"type": "object", "description": "Native contract, counting convention and period dimensions; bounded to 4096 JSON bytes."},
                   "data_mode": MODE}, ("source", "asset", "metric", "data_mode"))
DERIVATIVES = obj({"schema_version": {"type": "integer", "const": 1}, "asset": ASSET, "data_mode": MODE,
                   "source": TEXT, "quote": TEXT, "policy": DERIVATIVE_POLICY,
                   "observations": array(OBSERVATION, 5000)}, ("schema_version", "asset", "data_mode", "observations"))
STORED_COMMON = {"asset": ASSET, "quote": QUOTE, "data_mode": MODE,
                 "sources": array(TEXT, 32, 1, uniqueItems=True)}
STORED_VENUE = {**STORED_COMMON, "max_age_seconds": integer(86400, default=300),
                "max_skew_seconds": integer(86400, default=30)}
SOURCE_MAP = {"type": "object", "additionalProperties": ASSUMPTIONS, "maxProperties": 32}
QUALIFICATION = obj({"sources": array({**TEXT, "maxLength": 64}, 16, uniqueItems=True, default=[]),
                     "markets": array({"type": "string", "pattern": r"^[A-Z0-9][A-Z0-9._-]{0,31}/(?:EUR|USD)$"}, 4, 1, uniqueItems=True,
                                      default=["BTC/EUR", "ETH/EUR", "BTC/USD", "ETH/USD"]),
                     "market_sources": {"type": "object", "additionalProperties": array(TEXT, 16, 1, uniqueItems=True), "maxProperties": 4},
                     "data_mode": {**MODE, "default": "live"}, "required_history_days": integer(20000, 1, default=730),
                     "required_start": integer(), "required_end": integer(),
                     "freshness_seconds": integer(315360000, default=10800), "max_gap_hours": integer(500000, default=0),
                     "max_gap_details": integer(10000, default=100), "max_evidence_rows": integer(10000, default=256),
                     "max_query_rows": integer(2000000, 1, default=1000000), "max_requests": integer(10000, default=0),
                     "max_storage_bytes": integer(2**63 - 1, 1)},
                    description="Each market needs explicit sources globally or in market_sources. Aligned required range ends by as_of; max_requests is a declared ceiling, never a measured HTTP count.")

PLAN_EXAMPLE = {"schema_version": 1, "asset": "BTC", "quote": "EUR", "source": "synthetic",
                "data_mode": "demo", "start_time": 1704067200, "contributions": [{"time": 1704067200, "amount": "100"}],
                "schedule": [{"time": 1704067200, "amount": "90"}], "fees": {"rate": "0.001", "fixed": "0", "asset": "quote"}}
AS_OF = 1704067200
_COMMON_EXAMPLE = {"asset": "BTC", "quote": "EUR", "data_mode": "demo", "sources": ["synthetic"],
                   "max_age_seconds": 300, "max_skew_seconds": 30, "snapshots": []}


def _contract(argument, schema, example, **arguments):
    properties = {argument: schema, **arguments}
    if "as_of" in arguments:
        example = {**example, "as_of": AS_OF}
    return {"input_schema": obj(properties, (argument,)), "examples": [example]}


_AS_OF = {"type": ["integer", "null"], "minimum": 0, "description": "UTC epoch seconds; null uses current time."}
REGISTRY = {
    "construct_purchase_plan": _contract("request", CONSTRUCTOR, {"request": {"asset": "BTC", "quote": "EUR", "source": "synthetic",
        "data_mode": "demo", "start_date": "2024-01-01T00:00:00Z", "recurrence": {"frequency": "monthly", "count": 2, "month_end_rule": "clip"},
        "contribution_amount": "100", "purchase_amount": "90", "fees": {"rate": "0.001", "fixed": "0", "asset": "quote"}}}),
    "calculate_purchase_costs": _contract("request", PURCHASE, {"request": {**_COMMON_EXAMPLE, "budget": "100"}}, as_of=_AS_OF),
    "calculate_venue_disagreement": _contract("request", DISAGREEMENT, {"request": {**_COMMON_EXAMPLE, "price_basis": "mid", "outlier_threshold_bps": "100"}}, as_of=_AS_OF),
    "get_quote_context": _contract("request", QUOTES, {"request": {"data_mode": "demo", "stablecoin_prices": []}}, as_of=_AS_OF),
    "get_derivatives_context": _contract("request", DERIVATIVES, {"request": {"schema_version": 1, "asset": "BTC", "data_mode": "demo", "observations": []}}, as_of=_AS_OF),
    "get_stored_derivatives": _contract("request", obj({"asset": ASSET, "source": TEXT, "quote": TEXT, "data_mode": MODE, "policy": DERIVATIVE_POLICY}, ("asset", "source", "quote", "data_mode")),
        {"request": {"asset": "BTC", "source": "synthetic", "quote": "USD", "data_mode": "demo"}}, as_of=_AS_OF),
    "get_stored_quotes": _contract("request", obj({**STORED_COMMON, **QUOTE_POLICY}, ("quote", "data_mode", "sources")),
        {"request": {"quote": "EUR", "data_mode": "demo", "sources": ["synthetic"]}}, as_of=_AS_OF),
    "get_stored_venue_costs": _contract("request", obj({**STORED_VENUE, "budget": DECIMAL, "assumptions_by_source": SOURCE_MAP,
        "product_by_source": {"type": "object", "additionalProperties": TEXT}, "purchase_eligibility_by_source": {"type": "object", "additionalProperties": ELIGIBILITY}},
        ("asset", "quote", "data_mode", "sources", "budget")), {"request": {**{k: v for k, v in _COMMON_EXAMPLE.items() if k != "snapshots"}, "budget": "100"}}, as_of=_AS_OF),
    "get_stored_venue_disagreement": _contract("request", obj({**STORED_VENUE, "price_basis": enum("mid", "last", "best_ask", default="mid"),
        "outlier_threshold_bps": {**DECIMAL, "default": "100"}}, ("asset", "quote", "data_mode", "sources")),
        {"request": {k: v for k, v in _COMMON_EXAMPLE.items() if k != "snapshots"}}, as_of=_AS_OF),
    "qualify_sources": _contract("request", QUALIFICATION, {"request": {"sources": ["synthetic"], "markets": ["BTC/EUR"],
        "data_mode": "demo", "required_start": AS_OF - 86400, "required_end": AS_OF}}, as_of=_AS_OF),
}
for _name in ("preview_purchase_funding", "compare_purchase_plans", "evaluate_purchase_plans"):
    REGISTRY[_name] = _contract("config", PLAN, {"config": PLAN_EXAMPLE}, as_of=_AS_OF)
    if _name != "preview_purchase_funding":
        REGISTRY[_name]["input_schema"]["properties"]["validation"] = enum("known_at", "retrospective", default="known_at" if _name == "evaluate_purchase_plans" else "retrospective")
REGISTRY["evaluate_purchase_plans"]["input_schema"]["properties"].update({
    "origins": array(integer(), 24), "held_out_start": integer(), "policy_frozen_at": integer()})
REGISTRY["compare_cost_sensitivity"] = _contract("config", PLAN, {"config": PLAN_EXAMPLE,
    "cost_cases": [{"name": "declared", "fees": {"rate": "0.001", "fixed": "0", "asset": "quote"}}]},
    cost_cases=array(obj({"name": {**TEXT, "maxLength": 40}, "fees": FEES,
                         "execution": obj({key: DECIMAL for key in ("spread_bps", "slippage_bps")})}, ("name",)), 3, 1), as_of=_AS_OF,
    validation=enum("known_at", "retrospective", default="retrospective"))
REGISTRY["compare_cost_sensitivity"]["input_schema"]["required"].append("cost_cases")
REGISTRY["preview_shared_budget"] = _contract("request", obj({"schema_version": {"type": "integer", "const": 1}, "quote": QUOTE,
    "data_mode": MODE, "contributions": array(ACTION, 2000), "plans": array(obj({"id": TEXT, "family": enum("regular", "split", "deferred"), "config": PLAN},
    ("id", "family", "config")), 16, 2)}, ("schema_version", "quote", "data_mode", "contributions", "plans")),
    {"request": {"schema_version": 1, "quote": "EUR", "data_mode": "demo", "contributions": [{"time": AS_OF, "amount": "200"}],
     "plans": [{"id": "btc", "family": "regular", "config": PLAN_EXAMPLE},
               {"id": "eth", "family": "regular", "config": {**PLAN_EXAMPLE, "asset": "ETH"}}]}}, as_of=_AS_OF)
REGISTRY["get_accumulation_brief"] = {"input_schema": obj({"asset": ASSET, "source": TEXT, "quote": QUOTE,
    "as_of": _AS_OF, "plan_config": PLAN}), "examples": [{"asset": "BTC", "source": "synthetic", "quote": "EUR", "as_of": AS_OF, "plan_config": PLAN_EXAMPLE}]}

# Keep examples small but exercise the nested input contracts callers need.
_BOOK = {"source": "synthetic", "asset": "BTC", "quote": "EUR", "data_mode": "demo",
         "event_time": AS_OF - 10, "captured_at": AS_OF - 9, "input_reference": "synthetic-book-v1",
         "product": "spot_order_book", "asks_complete": False,
         "asks": [{"price": "100", "quantity": "2"}],
         "assumptions": {**{key: "0" for key in ASSUMPTION_FIELDS}, "quantity_step": "0.01", "price_step": "0.01",
                         "fee_currency": "quote", "input_reference": "synthetic-declared-costs-v1", "fee_tier": "synthetic"}}
REGISTRY["calculate_purchase_costs"]["examples"][0]["request"]["snapshots"] = [_BOOK]
REGISTRY["calculate_venue_disagreement"]["examples"][0]["request"].update(price_basis="last", snapshots=[{
    key: value for key, value in {**_BOOK, "price": "100"}.items() if key in {*SNAPSHOT_BASE, "price"}}])
_FX = {"base": "EUR", "quote": "USD", "value": "1.1", "event_time": AS_OF - 10, "observed_at": AS_OF - 9,
       "source": "synthetic", "source_url": "https://example.invalid/synthetic", "data_version": "synthetic-v1",
       "data_mode": "demo", "price_type": "indicative_reference"}
REGISTRY["get_quote_context"]["examples"][0]["request"].update(fx_reference=_FX,
    stablecoin_prices=[{**_FX, "base": "USDC", "quote": "USD", "value": "0.99", "price_type": "observed_spot"}])
REGISTRY["get_derivatives_context"]["examples"][0]["request"]["observations"] = [{
    "source": "synthetic", "asset": "BTC", "metric": "open_interest_amount", "event_time": AS_OF - 10,
    "observed_at": AS_OF - 9, "value": 2, "unit": "BTC", "quote": "USD", "data_mode": "demo",
    "metadata": {"symbol": "BTC/USD:BTC", "market_type": "swap", "settle": "BTC", "counting_convention": "one_sided"}}]

_DOSSIER_COSTS = obj({"budget": DECIMAL, "assumptions_by_source": SOURCE_MAP,
    "product_by_source": {"type": "object", "additionalProperties": TEXT, "maxProperties": 16},
    "purchase_eligibility_by_source": {"type": "object", "additionalProperties": {"type": "object"}, "maxProperties": 16}},
    ("budget",), description="Explicit caller assumptions; missing fees, products and eligibility remain unknown.")
_DOSSIER_VENUES = obj({"sources": array(TEXT, 16, 1, uniqueItems=True),
    "max_age_seconds": integer(86400, default=300), "max_skew_seconds": integer(86400, default=30),
    "price_basis": enum("mid", "last", "best_ask"), "outlier_threshold_bps": DECIMAL,
    "purchase_costs": _DOSSIER_COSTS}, ("sources",))
_DOSSIER_MARKET = obj({"asset": ASSET, "source": TEXT, "quote": QUOTE,
    "lookbacks": array(integer(3650, 1), 16, 1), "venues": _DOSSIER_VENUES,
    "derivatives": obj({"source": TEXT, "quote": {"type": "string", "minLength": 1, "maxLength": 120},
                        "policy": DERIVATIVE_POLICY}, ("source", "quote")),
    "quotes": obj({"sources": array(TEXT, 16, 1, uniqueItems=True),
                   "max_age_seconds": integer(86400, default=300),
                   "max_endpoint_skew_seconds": integer(86400, default=30)}, ("sources",))},
    ("asset", "source", "quote"))
DOSSIER = obj({"evidence_mode": enum("full", "references", default="full"), "data_mode": MODE, "markets": array(_DOSSIER_MARKET, 4, 1),
    "calendar": obj({"source": TEXT, "start": integer(), "end": integer()})}, ("data_mode", "markets"),
    description="One coherent read transaction, maximum 100000 total candles, 100 calendar rows, 64 KiB request and 32 MiB full result. Native quote identities remain separate.")
REGISTRY["get_analysis_dossier"] = _contract("request", DOSSIER,
    {"request": {"data_mode": "demo", "markets": [{"asset": "BTC", "source": "synthetic", "quote": "EUR",
        "lookbacks": [7], "venues": {"sources": ["synthetic"]},
        "derivatives": {"source": "synthetic", "quote": "USDT"},
        "quotes": {"sources": ["synthetic"]}}]}, "as_of": AS_OF}, as_of=_AS_OF)
_INSTALLATION_FIELDS = {key: value for key, value in QUALIFICATION["properties"].items()
    if key not in ("required_start", "required_end", "max_requests")}
_INSTALLATION_FIELDS.update({"sources": array({**TEXT, "maxLength": 64}, 4, 1, uniqueItems=True, default=["kraken"]),
    "refresh": {"type": "boolean", "const": False, "default": False},
    "as_of": integer(), "required_history_days": integer(3650, 1, default=730),
    "history_days": integer(30, 1, default=7), "max_pages": integer(4, 1, default=2),
    "max_logical_operations": integer(256, 1, default=64)})
REGISTRY["get_installation_qualification"] = _contract("request", obj(_INSTALLATION_FIELDS),
    {"request": {"sources": ["synthetic"], "data_mode": "demo", "refresh": False,
                 "required_history_days": 730}, "as_of": AS_OF}, as_of=_AS_OF)

CHANGES = obj({"dossier": DOSSIER, "previous_as_of": integer(), "max_changes": integer(500, 1, default=100)},
              ("dossier", "previous_as_of"),
              description="Previous cutoff must precede as_of. Both known-at views share one physical snapshot; deleted evidence is unknown.")
REGISTRY["get_analysis_changes"] = _contract("request", CHANGES,
    {"request": {"dossier": {"data_mode": "demo", "markets": [{"asset": "BTC", "source": "synthetic", "quote": "EUR", "lookbacks": [7]}]},
                 "previous_as_of": AS_OF - 3600}}, as_of=_AS_OF)
_READINESS_DOSSIER = deepcopy(DOSSIER)
_READINESS_DOSSIER["properties"]["markets"]["items"]["properties"]["source"]["maxLength"] = 64
READINESS = obj({"evidence_mode": enum("full", "references", default="full"), "dossier": _READINESS_DOSSIER,
    "qualification": obj({key: value for key, value in QUALIFICATION["properties"].items()
                          if key not in ("sources", "markets", "market_sources", "data_mode")}),
    "windows": obj({"horizons": array(integer(365, 1), 16, 1, uniqueItems=True),
                    "thresholds": array({"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1}, 12, 1, uniqueItems=True),
                    "max_price_age": integer(604800, 1)},
                   description="Runtime limits the aggregate market × horizon × threshold grid to 64 cells."),
    "freshness_seconds": integer(604800, default=7200)}, ("dossier",),
    description="Evidence readiness is independent of provider access, rights and France eligibility. No collection or mutation.")
REGISTRY["get_analysis_readiness"] = _contract("request", READINESS,
    {"request": {"dossier": {"data_mode": "demo", "markets": [{"asset": "BTC", "source": "synthetic", "quote": "EUR", "lookbacks": [7]}]},
                 "qualification": {"required_history_days": 7, "max_gap_details": 8},
                 "windows": {"horizons": [1], "thresholds": [0.05]}}}, as_of=_AS_OF)
_ROBUST_PLAN = deepcopy(PLAN)
_ROBUST_PLAN["required"] = [*_ROBUST_PLAN["required"], "fees"]
_ROBUST_PLAN["properties"]["fees"] = obj(deepcopy(FEES["properties"]), ("asset", "rate", "fixed"),
    description="Explicit hypothetical fee assumptions are required; omitted costs are not scored as zero.")
ROBUSTNESS = obj({"schema_version": {"type": "integer", "const": 1}, "config": _ROBUST_PLAN,
    "origins": array(integer(10**11), 4, 1, uniqueItems=True), "validation": enum("known_at", "retrospective", default="known_at"),
    "policy_frozen_at": integer(10**11), "held_out_start": integer(10**11),
    "cost_cases": array(obj({"name": {**TEXT, "maxLength": 40}, "fees": FEES,
                              "execution": obj({"spread_bps": DECIMAL, "slippage_bps": DECIMAL})}, ("name",)), 3, 1),
    "missing_data_cases": array(obj({"name": {**TEXT, "maxLength": 40},
                                     "removed_hours": array(integer(10**11, multipleOf=3600), 168, 1, uniqueItems=True)},
                                    ("name", "removed_hours")), 2)},
    ("schema_version", "config", "origins", "cost_cases"),
    description="Fixed predeclared grid with bounded operation/action work. No fitting, ranking or prospective performance claim.")
REGISTRY["get_plan_robustness"] = _contract("request", ROBUSTNESS,
    {"request": {"schema_version": 1, "config": PLAN_EXAMPLE, "origins": [AS_OF],
                 "cost_cases": [{"name": "declared costs"}]}}, as_of=_AS_OF)

_OPERATING_PAGE = {"source": TEXT, "limit": integer(100, 1, default=20),
                   "cursor": {"type": ["string", "null"], "maxLength": 16384}}
REGISTRY["get_collection_capacity"] = {
    "input_schema": obj({"tasks": array(enum("market", "liquidity", "options", "context"), 4, 1, uniqueItems=True)}),
    "examples": [{"tasks": ["market"]}]}
REGISTRY["get_operating_health"] = _contract("request", obj({**_OPERATING_PAGE,
    "required_history_hours": integer(8760, 1, default=24), "freshness_seconds": integer(31536000, default=10800),
    "max_gap_hours": integer(8760, default=0), "max_query_rows": integer(2000000, 1, default=250000)}),
    {"request": {"limit": 20, "required_history_hours": 24}}, as_of=_AS_OF)
REGISTRY["get_operating_costs"] = _contract("request", obj({**_OPERATING_PAGE, "task": TEXT, "start": integer(),
    "prices": obj({"currency": {**TEXT, "maxLength": 12},
                   "per_million_requests": {"type": "number", "minimum": 0},
                   "per_gib_decoded_bytes": {"type": "number", "minimum": 0}}, ("currency",))}),
    {"request": {"limit": 20}}, as_of=_AS_OF)
REGISTRY["get_operating_acceptance"] = _contract("request", obj({
    "session_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
    "refresh": {"type": "boolean", "const": False, "default": False}}),
    {"request": {"session_id": "first-public-observation", "refresh": False}})
_SNAPSHOT_NAME = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "default": "default"}
REGISTRY["get_analytical_snapshot_status"] = {"input_schema": obj({"snapshot_name": _SNAPSHOT_NAME}),
                                           "examples": [{"snapshot_name": "research"}]}
REGISTRY["get_analytical_fleet"] = _contract("request", obj({"assets": array(ASSET, 1000, 1, uniqueItems=True),
    "start": integer(multipleOf=3600), "end": integer(multipleOf=3600), "as_of": integer(),
    "source": TEXT, "quote": TEXT, "mode": MODE, "max_assets": integer(1000, 1, default=1000),
    "max_series": integer(10000, 1, default=10000)}, ("assets", "start", "end", "as_of")),
    {"request": {"assets": ["BTC", "ETH", "SOL"], "start": AS_OF - 86400, "end": AS_OF,
                 "as_of": AS_OF, "source": "synthetic", "quote": "EUR", "mode": "demo"}, "snapshot_name": "research"},
    snapshot_name=_SNAPSHOT_NAME)
_VALIDATION_EVENT = obj({"event": enum("endpoint_below_anchor", "below_anchor_touch", "observable_endpoint"),
    "convention": enum("exact_deadline_v1", "fully_contained_hourly_bars_v1", "hourly_after_deadline_v1"),
    "time_anchor": {"type": "string", "const": "issuance"}, "horizon_days": integer(365, 1),
    "threshold": {"type": ["number", "null"], "minimum": 0, "maximum": 1}},
    ("event", "convention", "time_anchor", "horizon_days", "threshold"))
_VALIDATION_COHORT = obj({"cohort_id": TEXT, "source": TEXT, "asset": ASSET, "quote": QUOTE,
    "data_mode": MODE, "dataset_lineage": {"type": ["object", "null"]}, "rules": {"type": "object"},
    "model_version": TEXT, "rule_version": TEXT, "events": array(_VALIDATION_EVENT, 64, 1)},
    ("cohort_id", "source", "asset", "quote", "data_mode", "dataset_lineage", "rules", "model_version", "rule_version", "events"))
VALIDATION_DECLARATION = obj({"name": TEXT, "mode": enum("prospective", "retrospective"),
    "starts_at": integer(), "ends_at": integer(), "cohorts": array(_VALIDATION_COHORT, 1000, 1),
    "criterion": obj({"minimum_resolved_nonoverlapping": integer(100, 1),
                      "maximum_missing_fraction": DECIMAL}, ("minimum_resolved_nonoverlapping", "maximum_missing_fraction")),
    "baseline": obj({"family": {"type": "string", "const": "regular"}, "reserve_yield": {"type": "string", "const": "0"},
                     "rule_version": TEXT, "fees": FEES, "execution": EXECUTION},
                    ("family", "reserve_yield", "rule_version", "fees", "execution"))},
    ("name", "mode", "starts_at", "ends_at", "cohorts", "criterion", "baseline"))
VALIDATION_PROTOCOL = obj({"schema_version": {"type": "integer", "const": 1},
    "protocol_version": {"type": "string", "const": "prospective_validation_v1"}, "frozen_at": integer(),
    "protocol_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "declaration": VALIDATION_DECLARATION},
    ("schema_version", "protocol_version", "frozen_at", "protocol_hash", "declaration"))
REGISTRY["get_prospective_validation"] = _contract("request", obj({"protocol": VALIDATION_PROTOCOL,
    "cohort_page": integer(999, default=0), "page_size": integer(100, 1, default=10),
    "origins_per_cohort": integer(100, 1, default=100), "record_budget": integer(1000, 1, default=1000)}, ("protocol",)),
    {"request": {"protocol": {"schema_version": 1, "protocol_version": "prospective_validation_v1",
                              "frozen_at": AS_OF, "protocol_hash": "0" * 64, "declaration": {}}, "page_size": 1}},
    as_of=_AS_OF)


# The example is executable and hash-valid; it remains explicitly synthetic.

_VALIDATION_EXAMPLE = {
    "name": "Synthetic schema example", "mode": "retrospective", "starts_at": AS_OF, "ends_at": AS_OF + 86400,
    "criterion": {"minimum_resolved_nonoverlapping": 2, "maximum_missing_fraction": "0.1"},
    "baseline": {"family": "regular", "reserve_yield": "0", "rule_version": _PLAN_RULE_VERSION,
                 "fees": {"rate": "0", "fixed": "0", "asset": "quote"},
                 "execution": {"timeframe": "1h", "max_price_age_seconds": 3600, "spread_bps": "0",
                               "slippage_bps": "0", "quantity_step": "0.00000001", "price_step": "0.00000001",
                               "assumption": "historical_configured_costs"}},
    "cohorts": [{"cohort_id": "synthetic-btc-eur", "source": "synthetic", "asset": "BTC", "quote": "EUR",
                 "data_mode": "demo", "dataset_lineage": None, "rules": {"purchase_windows": 1},
                 "model_version": "purchase_windows_v1", "rule_version": "issuance_hourly_v1",
                 "events": [{"event": "endpoint_below_anchor", "convention": "exact_deadline_v1",
                             "time_anchor": "issuance", "horizon_days": 1, "threshold": None}]}]}
REGISTRY["get_prospective_validation"]["examples"] = [{"request": {
    "protocol": _freeze_validation_protocol(_VALIDATION_EXAMPLE, frozen_at=AS_OF-1), "page_size": 1}, "as_of": AS_OF}]


REGISTRY.update(_market_contracts(obj, integer, enum, array))


def get_request_schema(tool_name: str) -> dict:
    """Return one bounded contract instead of serializing the complete registry."""
    if tool_name == "list":
        return {"tools": sorted(REGISTRY), "schema_version": 1,
                "usage": "Call get_request_schema with one tool_name for its input schema and synthetic example."}
    if tool_name not in REGISTRY:
        raise ValueError("Unknown request tool; use tool_name='list'.")
    return {"tool_name": tool_name, "schema_version": 1, **deepcopy(REGISTRY[tool_name]),
            "validation": "Structural JSON Schema plus runtime cross-field/chronology checks; synthetic examples are not market evidence."}
