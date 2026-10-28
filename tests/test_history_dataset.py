"""Synthetic split archives and injected responses; no provider access."""
import hashlib
import io
import zipfile
from dataclasses import replace

import pytest

from crypto_market_analysis.config import Settings
from crypto_market_analysis.engine import Engine
from crypto_market_analysis.history_dataset import (
    ArchivePart,
    DatasetConfig,
    HistoryDataset,
    KrakenRESTCapture,
    RESTCapture,
    UnsupportedMarket,
)

NOW = 200 * 3600


def dataset(tmp_path, clock=lambda: NOW, **kwargs):
    return HistoryDataset(DatasetConfig("synthetic-research", str(tmp_path / "research"),
                                        0, 3 * 3600, **kwargs), clock=clock)


def row(hour, close=10):
    return [hour * 3_600_000, 10, max(10, close), min(10, close), close, 2]


def parts(tmp_path, payload=b"0,10,10,10,10,2,1\n3600,10,10,10,10,2,1\n", name="XBTEUR_60.csv"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
        archive.writestr("unselected.csv", "never imported")
    data = buffer.getvalue()
    # Boundaries cross file headers, compressed data and central directory.
    cuts = [0, 17, len(data) // 2, len(data) - 12, len(data)]
    result = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:], strict=False)):
        path = tmp_path / f"official.zip.{index:03d}"
        content = data[start:end]
        path.write_bytes(content)
        result.append(ArchivePart(str(path), f"https://example.invalid/{path.name}",
                                  hashlib.sha256(content).hexdigest()))
    return result, data, payload


def test_split_zip_exact_member_hashes_idempotent_and_mode(tmp_path):
    ds = dataset(tmp_path)
    archive, raw, member = parts(tmp_path)
    result = ds.stage_archive(archive, member="XBTEUR_60.csv", asset="BTC", quote="EUR", captured_at=100)
    assert result["archive_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["member_sha256"] == hashlib.sha256(member).hexdigest()
    assert result["captured_at"] == 100
    assert result["materialized_at"] == NOW
    assert all(c.observed_at == NOW for c in ds.store.candles("BTC", mode="import", as_of=NOW))
    assert ds.store.candles("BTC", mode="live", as_of=NOW) == []
    assert ds.stage_archive(archive, member="XBTEUR_60.csv", asset="BTC", quote="EUR",
                            captured_at=100)["id"] == result["id"]
    assert len(ds.manifest()["captures"]) == 1
    assert not list(ds.directory.glob("*.csv"))
    markets = ds.manifest()["markets"]
    assert len(markets) == 4
    assert markets[0]["gaps"] == [{"start": 7200, "end": 10800, "status": "unknown"}]
    assert all(m["coverage_status"] == "unavailable" for m in markets[1:])


def test_wrong_hash_member_budget_and_no_partial_materialization(tmp_path):
    ds = dataset(tmp_path)
    archive, _, _ = parts(tmp_path)
    archive[0] = replace(archive[0], sha256="0" * 64)
    with pytest.raises(ValueError, match="checksum mismatch"):
        ds.stage_archive(archive, member="XBTEUR_60.csv", asset="BTC", quote="EUR", captured_at=0)
    assert ds.manifest()["captures"] == []
    archive, _, _ = parts(tmp_path)
    with pytest.raises(ValueError, match="safe native"):
        ds.stage_archive(archive, member="../XBTEUR_60.csv", asset="BTC", quote="EUR", captured_at=0)
    bounded = dataset(tmp_path / "bounded", max_member_bytes=5)
    with pytest.raises(ValueError, match="configured bound"):
        bounded.stage_archive(archive, member="XBTEUR_60.csv", asset="BTC", quote="EUR", captured_at=0)
    assert bounded.manifest()["captures"] == []


def test_overlap_revision_actual_availability_and_reopen(tmp_path):
    clock = [NOW]
    ds = dataset(tmp_path, clock=lambda: clock[0])
    ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(0)], b"first"), since=0)
    clock[0] += 100
    capture = ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(0, 12)], b"revised"), since=0)
    assert capture["raw_sha256"] == hashlib.sha256(b"revised").hexdigest()
    assert capture["captured_at"] == clock[0]
    assert len(capture["conflicts"]) == 1
    assert ds.store.candles("BTC", mode="import", as_of=NOW)[0].close == 10
    reopened = HistoryDataset(ds.config, clock=lambda: clock[0])
    assert reopened.store.candles("BTC", mode="import", as_of=clock[0])[0].close == 12
    assert len(reopened.manifest()["captures"]) == 2
    assert reopened.manifest()["markets"][0]["attempts"][-1]["bytes"] == 7


def test_repair_resumes_old_hole_independent_of_newest(tmp_path):
    ds = dataset(tmp_path)
    ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(99)]), since=99 * 3600)
    ds.plan_repair("BTC", "EUR", 0, 3 * 3600)
    requested = []

    def fetch(_asset, _quote, since, _limit):
        requested.append(since)
        return RESTCapture([row(since // 3600)])

    state = ds.repair("BTC", "EUR", fetch, request_budget=1)
    assert state["cursor"] == 3600
    ds = HistoryDataset(ds.config, clock=lambda: NOW)
    state = ds.repair("BTC", "EUR", fetch, request_budget=2)
    assert state["status"] == "complete"
    assert state["cursor"] == 10800
    assert requested == [0, 3600, 7200]


def test_gap_error_unsupported_and_retention_remain_distinct(tmp_path):
    ds = dataset(tmp_path)
    ds.plan_repair("BTC", "EUR", 0, 3600)
    assert ds.repair("BTC", "EUR", lambda *_: RESTCapture([row(99)]))["status"] == "unknown_gap"

    def broken(*_):
        raise RuntimeError("access denied")

    assert ds.repair("BTC", "EUR", broken)["status"] == "error"

    def unsupported(*_):
        raise UnsupportedMarket("not listed")

    assert ds.repair("BTC", "EUR", unsupported)["status"] == "unsupported"
    ds.clock = lambda: 1000 * 3600
    assert ds.repair("BTC", "EUR", broken)["status"] == "retention_limited"
    assert len(ds.manifest()["markets"][0]["attempts"]) == 3


def test_foreign_dataset_identity_and_mode_are_rejected(tmp_path):
    ds = dataset(tmp_path)
    with pytest.raises(ValueError, match="mismatch"):
        HistoryDataset(replace(ds.config, name="other"))
    with pytest.raises(ValueError, match="import lineage"):
        DatasetConfig("bad", str(tmp_path / "bad"), 0, 3600, data_mode="live")
    other = tmp_path / "other"
    other.mkdir()
    (other / "observer.sqlite").touch()
    with pytest.raises(ValueError, match="empty directory"):
        HistoryDataset(replace(ds.config, directory=str(other)))


def test_public_adapter_native_symbol_and_raw_response():
    class Client:
        last_http_response = '{"result":"synthetic"}'

        def load_markets(self):
            return {"BTC/EUR": {"spot": True}}

        def fetch_ohlcv(self, symbol, timeframe, *, since, limit):
            assert (symbol, timeframe, since, limit) == ("BTC/EUR", "1h", 3600000, 720)
            return [row(1)]

    adapter = KrakenRESTCapture(Client())
    captured = adapter("BTC", "EUR", 3600, 720)
    assert captured.raw == b'{"result":"synthetic"}'
    assert captured.capture_evidence == "injected_client_unqualified"
    with pytest.raises(UnsupportedMarket):
        adapter("BTC", "USD", 3600, 720)


def test_invalid_rest_fails_without_data_and_records_error(tmp_path):
    ds = dataset(tmp_path)
    invalid = row(0)
    invalid[2] = 1
    with pytest.raises(ValueError):
        ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([invalid]), since=0)
    assert ds.store.candles("BTC", mode="import", as_of=NOW) == []
    assert ds.manifest()["markets"][0]["attempts"][-1]["status"] == "error"
    assert not list(ds.directory.glob("pending-*.json"))
    assert HistoryDataset(ds.config, clock=lambda: NOW).manifest()["captures"] == []


def test_restart_recovers_capture_journal_before_gap_resume(tmp_path, monkeypatch):
    ds = dataset(tmp_path)
    original = ds.store.put_candles

    def interrupted(candles):
        original(candles)
        raise RuntimeError("process interrupted after candle commit")

    monkeypatch.setattr(ds.store, "put_candles", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(0)], b"receipt"), since=0)
    assert len(list(ds.directory.glob("pending-*.json"))) == 1
    recovered = HistoryDataset(ds.config, clock=lambda: NOW + 100)
    assert len(recovered.manifest()["captures"]) == 1
    assert len(recovered.store.candles("BTC", mode="import", as_of=NOW + 100)) == 1
    assert not list(ds.directory.glob("pending-*.json"))


def test_normal_import_engine_reads_dataset_directory_without_mode_union(tmp_path):
    ds = dataset(tmp_path)
    ds.append_rest("BTC", "EUR", lambda *_: RESTCapture([row(0)]), since=0)
    settings = Settings(data_dir=ds.directory)
    assert ds.store.path == settings.database
    candles, _ = Engine(settings, "import").selected_candles("BTC", "kraken", "EUR", as_of=NOW)
    assert len(candles) == 1
    assert Engine(settings, "live").selected_candles("BTC", "kraken", "EUR", as_of=NOW)[0] == []
    evidence = ds.manifest()["markets"][0]["capture_evidence"]
    assert evidence == {"public_rest_captures_observed": 0, "injected_unqualified_captures": 1,
                        "report_qualification": "unverified"}


def test_archive_cleanup_preserves_preexisting_staging_name(tmp_path):
    ds = dataset(tmp_path)
    archive, _, _ = parts(tmp_path)
    existing = ds.directory / "XBTEUR_60.csv"
    existing.write_bytes(b"preexisting local evidence")
    with pytest.raises(FileExistsError):
        ds.stage_archive(archive, member="XBTEUR_60.csv", asset="BTC", quote="EUR", captured_at=100)
    assert existing.read_bytes() == b"preexisting local evidence"
    assert ds.manifest()["captures"] == []


def test_manifest_bounds_captures_conflicts_attempts_without_changing_version(tmp_path):
    clock = [NOW]
    ds = dataset(tmp_path, clock=lambda: clock[0])
    for close in (10, 11, 12):
        ds.append_rest("BTC", "EUR", lambda *_, close=close: RESTCapture(
            [row(0, close), row(1, close), row(2, close)]), since=0)
        clock[0] += 1
    complete = ds.manifest()
    bounded = ds.manifest(max_records=1, max_conflicts=1)
    assert bounded["dataset_version"] == complete["dataset_version"]
    assert bounded["captures_total"] == 3
    assert bounded["captures_omitted"] == 2
    assert len(bounded["captures"]) == 1
    capture = bounded["captures"][0]
    assert len(capture["conflicts"]) == 1
    assert capture["conflicts_total"] == 3
    assert capture["conflicts_omitted"] == 2
    market = bounded["markets"][0]
    assert market["attempts_total"] == 3
    assert market["attempts_omitted"] == 2
    assert market["capture_evidence"]["injected_unqualified_captures"] == 3
    assert len(market["attempts"]) == 1
