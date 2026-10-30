"""Discoverable native-instrument read contracts and synthetic examples."""


def contracts(obj, integer, enum, array):
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    clock = integer(253402300799, -62135596800)
    cutoff = {"type": ["integer", "null"], "minimum": -62135596800, "maximum": 253402300799}
    mode = enum("live", "import", "demo")
    nonnegative = {"type": "number", "minimum": 0}
    selection = {"series_id": text, "data_mode": mode, "currency": text, "start": clock, "end": clock}
    example = {"series_id": "synthetic:series:0", "data_mode": "demo", "currency": "USD",
               "start": 1704153600, "end": 1705017600}

    def entry(schema, request, **extra):
        return {"input_schema": obj({"request": schema, "as_of": cutoff, **extra}, ("request",)),
                "examples": [{"request": request, "as_of": 1705017600}]}

    annualization = obj({"periods_per_year": {"type": "number", "exclusiveMinimum": 0}, "convention": text},
                        ("periods_per_year", "convention"))
    cash_plan = obj({"currency": text, "initial_cash": nonnegative,
                     "deposits": array(obj({"at": clock, "amount": nonnegative}, ("at", "amount")), 1000),
                     "purchases": array(obj({"at": clock, "cash_amount": nonnegative}, ("at", "cash_amount")), 1000),
                     "fee_per_trade": {"type": ["number", "null"], "minimum": 0}, "distributions": enum("cash"),
                     "actions_complete": {"type": "boolean"}},
                    ("currency", "initial_cash", "deposits", "purchases", "fee_per_trade", "distributions", "actions_complete"))
    result = {
        "get_market_capabilities": {"input_schema": obj({}), "examples": [{}]},
        "get_instrument_registry": entry(obj({"effective_at": clock, "universe": text, "provider": text,
                                              "symbol": text, "listing_id": text}), {"effective_at": 1704240000}),
        "get_market_calendar": entry(obj({"calendar_id": text, "calendar_version": text, "start": clock,
                                           "end": clock, "interval": enum("session", "1h", "reference")},
                                          ("calendar_id", "calendar_version", "start", "end")),
                                      {"calendar_id": "synthetic:exchange", "calendar_version": "synthetic-v1",
                                       "start": example["start"], "end": example["end"], "interval": "session"}),
        "get_instrument_analysis": entry(obj({**selection, "annualization": annualization},
                                             (*selection, "annualization")),
                                          {**example, "annualization": {"periods_per_year": 252,
                                                                         "convention": "Declared synthetic daily sessions"}}),
        "simulate_instrument_plan": entry(obj({**selection, "plan": cash_plan}, (*selection, "plan")),
                                           {**example, "plan": {"currency": "USD", "initial_cash": 1000,
                                            "deposits": [], "purchases": [{"at": 1704211200, "cash_amount": 500}],
                                            "fee_per_trade": 1, "distributions": "cash", "actions_complete": False}}),
        "get_instrument_series": entry(obj({"series_id": text, "data_mode": mode, "start": clock, "end": clock,
                                             "max_rows": integer(512, 1, default=128), "all_vintages": {"type": "boolean", "default": False}}, ("series_id", "data_mode")),
                                        {"series_id": "synthetic:series:0", "data_mode": "demo", "max_rows": 128}),
        "get_market_fleet": entry(obj({"series_ids": array(text, 1000, 1, uniqueItems=True), "data_mode": mode,
                                        "start": clock, "end": clock, "max_rows": integer(1000000, 1)},
                                       ("series_ids", "data_mode", "start", "end")),
                                   {"series_ids": ["synthetic:series:0"], "data_mode": "demo",
                                    "start": example["start"], "end": example["end"]}, snapshot_name=text),
        "get_market_snapshot_status": {"input_schema": obj({"snapshot_name": text}), "examples": [{}]},
        "get_instrument_outcome": entry(obj({"assessment_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
                                              ("assessment_id",)), {"assessment_id": "0" * 64}),
    }
    future = obj({"expiry": clock, "multiplier": {"type": "number", "exclusiveMinimum": 0},
                  "unit": text, "settlement": text, "underlying_instrument_id": text},
                 ("expiry", "multiplier", "unit", "settlement"))
    position_fields = {key: {"type": "number"} for key in ("entry_price", "mark_price")}
    position_fields.update({key: clock for key in ("entry_at", "mark_at", "mark_observed_at")})
    position_fields.update({key: nonnegative for key in ("collateral", "initial_margin_per_contract", "maintenance_margin_per_contract")})
    position_fields.update(currency=text, quantity={"type": "integer"}, fees={"type": ["number", "null"], "minimum": 0})
    valuation = {"contract": {"expiry": 100, "multiplier": 1000, "unit": "barrel", "settlement": "cash"},
                 "native_currency": "USD", "position": {"currency": "USD", "quantity": 1, "entry_price": 10,
                 "entry_at": 1, "mark_price": -37.63, "mark_at": 20, "mark_observed_at": 21,
                 "collateral": 10000, "initial_margin_per_contract": 10000, "maintenance_margin_per_contract": 5000, "fees": 0}}
    result["calculate_futures_value"] = entry(obj({"contract": future, "position": obj(position_fields, position_fields),
                                                  "native_currency": text}, ("contract", "position", "native_currency")), valuation)
    result["calculate_futures_value"]["examples"][0]["as_of"] = 30
    result["build_futures_research_series"] = entry(obj({
        "schema": {"const": "continuous-futures-request.v1"}, "descriptor": {"type": "object"},
        "method": enum("unadjusted"), "version": text,
        "contracts": array(obj({"instrument_id": text, "available_at": clock, "contract": future,
                                 "series": {"type": "object"}}, ("instrument_id", "available_at", "contract", "series")), 1000, 1),
        "rolls": array(obj({"effective_at": clock, "known_at": clock, "instrument_id": text,
                             "reason": text, "source_url": text},
                            ("effective_at", "known_at", "instrument_id", "reason", "source_url")), 2000, 1)},
        ("schema", "descriptor", "method", "version", "contracts", "rolls")),
        {"schema": "continuous-futures-request.v1", "descriptor": {}, "method": "unadjusted", "version": "v1",
         "contracts": [], "rolls": []})
    descriptor = {"id": "synthetic:continuous", "instrument_id": "synthetic:derived", "listing_id": None,
                  "provider": "synthetic", "provider_symbol": "EXAMPLE", "native_currency": "USD", "unit": "barrel",
                  "price_basis": "continuous", "data_mode": "demo", "calendar_id": "synthetic:calendar",
                  "calendar_version": "synthetic-v1", "interval": "session", "revision_semantics": "point_in_time"}
    source = {**descriptor, "id": "synthetic:future-series", "instrument_id": "synthetic:future",
              "listing_id": "synthetic:future-listing", "price_basis": "raw"}
    curve = {"schema": "continuous-futures-request.v1", "descriptor": descriptor,
             "method": "unadjusted", "version": "synthetic-v1", "contracts": [{
                 "instrument_id": source["instrument_id"], "available_at": -10,
                 "contract": valuation["contract"], "series": {"schema": "market-series.v1", "descriptor": source,
                 "bars": [{"start": 0, "end": 10, "open": 10, "high": 10, "low": 10, "close": 10, "observed_at": 10}],
                 "actions": []}}], "rolls": [{"effective_at": 0, "known_at": -1,
                 "instrument_id": source["instrument_id"], "reason": "Synthetic initial selection",
                 "source_url": "https://example.invalid/synthetic-chain"}]}
    result["build_futures_research_series"]["examples"] = [{"request": curve, "as_of": 30}]
    result["build_futures_research_series"]["example_file"] = "examples/market-futures-curve.json"
    return result
