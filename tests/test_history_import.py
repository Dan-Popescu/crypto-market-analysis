"""Synthetic files exercise official schemas, never real market data."""

import hashlib
import zipfile

import pytest

from crypto_market_analysis.history_import import ImportLimits, import_history
from crypto_market_analysis.storage import Store


def write_zip(path, member, text):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kline(timestamp, divisor):
    return f"{timestamp * divisor},10,12,8,11,2,{(timestamp + 3600) * divisor - 1},21,3,1,9,0\n"


def test_headerless_kraken_preserves_native_quote_and_import_availability(tmp_path):
    path = tmp_path / "XBTEUR_60.csv"
    path.write_text("0,10,12,8,11,2,3\n7200,11,13,10,12,4,5\n")
    result = import_history(path, source="kraken", asset="BTC", quote="EUR", observed_at=10800)
    assert [c.open_time for c in result.candles] == [0, 7200]
    assert all(
        c.quote == "EUR" and c.data_mode == "import" and c.observed_at == 10800 for c in result.candles
    )
    assert result.provenance["missing_hour_count"] == 1
    assert result.provenance["historical_known_at"] == "unknown"
    assert result.provenance["data_version"] == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    store = Store(tmp_path / "local.sqlite")
    assert store.put_candles(result.candles) == 2
    assert store.candles("BTC", mode="import", as_of=10799) == []
    assert store.put_candles(result.candles) == 0


@pytest.mark.parametrize(
    ("timestamp", "divisor", "name"),
    [
        (1735686000, 1000, "BTCUSDT-1h-2024-12-31.csv"),
        (1735689600, 1_000_000, "BTCUSDT-1h-2025-01-01.csv"),
    ],
)
def test_binance_timestamp_migration_and_checksum(tmp_path, timestamp, divisor, name):
    path = tmp_path / name.replace(".csv", ".zip")
    digest = write_zip(path, name, kline(timestamp, divisor))
    result = import_history(
        path,
        source="binance",
        asset="BTC",
        quote="USDT",
        observed_at=timestamp + 3600,
        member=name,
        expected_sha256=f"{digest}  {path.name}",
    )
    candle = result.candles[0]
    assert (candle.open_time, candle.close_time, candle.quote) == (timestamp, timestamp + 3600, "USDT")
    assert (candle.quote_volume, candle.taker_buy_quote_volume) == (21, 9)
    assert result.provenance["checksum_verified"] is True
    with pytest.raises(ValueError, match="checksum mismatch"):
        import_history(
            path,
            source="binance",
            asset="BTC",
            quote="USDT",
            observed_at=timestamp + 3600,
            member=name,
            expected_sha256="0" * 64,
        )


def test_no_checksum_wrong_market_daily_and_wrong_units_rejected(tmp_path):
    path = tmp_path / "ETHUSD-1h-2025-01.csv"
    path.write_text(kline(1735689600, 1000))
    kwargs = dict(source="binance", asset="ETH", quote="USD", observed_at=1735693200)
    with pytest.raises(ValueError, match="require a SHA256"):
        import_history(path, **kwargs)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="2025 migration"):
        import_history(path, **kwargs, expected_sha256=digest)
    with pytest.raises(ValueError, match="hourly imports"):
        import_history(path, **kwargs, expected_sha256=digest, timeframe="1d")
    with pytest.raises(ValueError, match="selected native"):
        import_history(path, **{**kwargs, "quote": "USDC"}, expected_sha256=digest)


@pytest.mark.parametrize("name", ["../XBTUSD_60.csv", "/XBTUSD_60.csv", "dir\\XBTUSD_60.csv"])
def test_unsafe_members_never_extracted(tmp_path, name):
    path = tmp_path / "archive.zip"
    write_zip(path, name, "0,10,12,8,11,2,3\n")
    with pytest.raises(ValueError, match="unsafe"):
        import_history(path, source="kraken", asset="BTC", quote="USD", observed_at=3600, member=name)
    assert not (tmp_path.parent / "XBTUSD_60.csv").exists()


def test_archive_limits_duplicate_members_and_rows(tmp_path):
    path = tmp_path / "archive.zip"
    name = "XBTUSD_60.csv"
    write_zip(path, name, "0,10,12,8,11,2,3\n3600,10,12,8,11,2,3\n")
    kwargs = dict(source="kraken", asset="BTC", quote="USD", observed_at=7200, member=name)
    with pytest.raises(ValueError, match="row-count"):
        import_history(path, **kwargs, limits=ImportLimits(max_rows=1))
    with pytest.raises(ValueError, match="size limit"):
        import_history(path, **kwargs, limits=ImportLimits(max_member_bytes=5))
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(name, "")
    with pytest.raises(ValueError, match="duplicated"):
        import_history(path, **kwargs)


@pytest.mark.parametrize(
    "text",
    [
        "0,10,12,8,11,2,3\n0,10,12,8,10,2,3\n",
        "0,nan,12,8,11,2,3\n",
        "0,10,9,8,11,2,3\n",
        "3600,10,12,8,11,2,3\n",
    ],
)
def test_conflicting_invalid_or_future_candles_fail_entire_import(tmp_path, text):
    path = tmp_path / "XBTUSD_60.csv"
    path.write_text(text)
    with pytest.raises(ValueError, match="Invalid history CSV row"):
        import_history(path, source="kraken", asset="BTC", quote="USD", observed_at=3600)


def test_daily_or_header_input_not_silently_treated_as_hourly(tmp_path):
    path = tmp_path / "XBTUSD_1440.csv"
    path.write_text("0,10,12,8,11,2,3\n")
    with pytest.raises(ValueError, match="_60.csv"):
        import_history(path, source="kraken", asset="BTC", quote="USD", observed_at=86400)
    path = tmp_path / "XBTUSD_60.csv"
    path.write_text("timestamp,open,high,low,close,volume,trades\n")
    with pytest.raises(ValueError, match="Invalid history CSV row"):
        import_history(path, source="kraken", asset="BTC", quote="USD", observed_at=86400)


def test_input_file_size_and_member_count_are_bounded(tmp_path):
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("XBTUSD_60.csv", "0,10,12,8,11,2,3\n")
        archive.writestr("ETHUSD_60.csv", "0,10,12,8,11,2,3\n")
    kwargs = dict(source="kraken", asset="BTC", quote="USD", observed_at=3600, member="XBTUSD_60.csv")
    with pytest.raises(ValueError, match="member-count"):
        import_history(path, **kwargs, limits=ImportLimits(max_members=1))
    with pytest.raises(ValueError, match="file-size"):
        import_history(path, **kwargs, limits=ImportLimits(max_file_bytes=1))
