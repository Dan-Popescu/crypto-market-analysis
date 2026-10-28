"""Explicit import-only Kraken research datasets and manual, bounded gap repair.

Construction, archive staging and manifests are offline. Only ``append_rest`` and
``repair`` invoke the supplied fetcher. No scheduler or cross-mode union exists.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import time
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from .history_import import ImportLimits, import_history
from .models import Candle
from .storage import Store

MARKETS = (("BTC", "EUR"), ("ETH", "EUR"), ("BTC", "USD"), ("ETH", "USD"))


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _epoch(value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a nonnegative integer UTC epoch second.")


def _range(start: int, end: int) -> None:
    _epoch(start)
    _epoch(end)
    if start % 3600 or end % 3600 or end <= start:
        raise ValueError("Expected a nonempty aligned half-open hourly range.")


@dataclass(frozen=True)
class DatasetConfig:
    name: str
    directory: str
    target_start: int
    target_end: int
    source: str = "kraken"
    data_mode: str = "import"
    max_parts: int = 8
    max_part_bytes: int = 3 * 1024**3
    max_archive_bytes: int = 16 * 1024**3
    max_member_bytes: int = 64 * 1024**2
    max_members: int = 100_000
    max_directory_bytes: int = 32 * 1024**2
    max_path_bytes: int = 1024
    max_rows: int = 200_000
    max_capture_bytes: int = 4 * 1024**2
    max_disk_bytes: int = 512 * 1024**2
    max_requests: int = 20

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", self.name):
            raise ValueError("Dataset needs an explicit safe research name.")
        if self.source != "kraken" or self.data_mode != "import":
            raise ValueError("This dataset only supports explicit Kraken import lineage.")
        _range(self.target_start, self.target_end)
        if (self.target_end - self.target_start) // 3600 > self.max_rows:
            raise ValueError("Target hours exceed the configured row budget.")
        for key, value in asdict(self).items():
            if key.startswith("max_") and (type(value) is not int or value <= 0):
                raise ValueError("Dataset resource bounds must be positive integers.")


@dataclass(frozen=True)
class ArchivePart:
    path: str
    url: str
    sha256: str


@dataclass(frozen=True)
class RESTCapture:
    """Rows are CCXT OHLCV milliseconds; raw is the exact OHLC response if available."""
    rows: list[list]
    raw: bytes | None = None
    endpoint: str = "https://api.kraken.com/0/public/OHLC"
    capture_evidence: str = "injected_fetch_unqualified"


class UnsupportedMarket(ValueError):
    """The source catalogue explicitly lacks the requested native spot pair."""


class KrakenRESTCapture:
    """Explicit one-shot no-key adapter; injected clients remain caller-owned.

    One call may also load CCXT's public catalogue. Capture.raw contains only the
    final OHLC response, not catalogue bytes. Budgets count OHLC fetch calls.
    """
    def __init__(self, client=None):
        self.capture_evidence = "public_rest_adapter" if client is None else "injected_client_unqualified"
        if client is None:
            import ccxt
            client = ccxt.kraken({"enableRateLimit": True, "timeout": 10000,
                                  "requests_trust_env": True})
        self.client = client

    def __call__(self, asset: str, quote: str, since: int, limit: int) -> RESTCapture:
        symbol = f"{asset}/{quote}"
        markets = self.client.load_markets()
        if symbol not in markets or not markets[symbol].get("spot", False):
            raise UnsupportedMarket(f"Unsupported Kraken native spot market: {symbol}")
        rows = self.client.fetch_ohlcv(symbol, "1h", since=since * 1000, limit=limit)
        raw = getattr(self.client, "last_http_response", None)
        return RESTCapture(rows, raw.encode() if isinstance(raw, str) else raw,
                           capture_evidence=self.capture_evidence)


class _Parts(io.RawIOBase):
    """Seekable virtual concatenation for a binary-split ZIP; never copies parts."""
    def __init__(self, streams, sizes, max_read):
        self.max_read = max_read
        self.streams, self.sizes, self.position = streams, sizes, 0
        self.length = sum(sizes)

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if whence not in (0, 1, 2):
            raise ValueError("Invalid archive seek mode.")
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.length + offset
        if position < 0:
            raise ValueError("Negative archive seek.")
        self.position = position
        return position

    def read(self, size=-1):
        remaining = max(0, self.length - self.position) if size < 0 else size
        if remaining > self.max_read:
            raise ValueError("Archive directory read exceeds the configured byte bound.")
        chunks = []
        base = 0
        for stream, length in zip(self.streams, self.sizes, strict=True):
            if base <= self.position < base + length and remaining:
                stream.seek(self.position - base)
                chunk = stream.read(min(remaining, base + length - self.position))
                chunks.append(chunk)
                self.position += len(chunk)
                remaining -= len(chunk)
            base += length
        return b"".join(chunks)


class HistoryDataset:
    def __init__(self, config: DatasetConfig, *, clock: Callable[[], float] = time.time):
        self.config, self.clock = config, clock
        self.directory = Path(config.directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = self.directory / "dataset.json"
        encoded = _json(asdict(replace(config, directory=str(self.directory))))
        if identity.exists():
            if identity.read_text() != encoded:
                raise ValueError("Dataset identity/configuration mismatch.")
        else:
            if any(self.directory.iterdir()):
                raise ValueError("A new dataset requires its own empty directory.")
            identity.write_text(encoded)
        self.store = Store(self.directory / "observer.sqlite")
        with self.store.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS history_captures (
                    id TEXT PRIMARY KEY, asset TEXT, quote TEXT, materialized_at INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS history_repairs (
                    market TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS history_attempts (
                    id INTEGER PRIMARY KEY, asset TEXT, quote TEXT, payload TEXT);
            """)
            if con.execute("SELECT 1 FROM candles WHERE data_mode!='import' OR source!='kraken' LIMIT 1").fetchone():
                raise ValueError("Dataset contains a foreign source or mode.")

        self._recover_pending()

    def _recover_pending(self):
        # Journal first: after interruption, replay original immutable payloads
        # before accepting new appends. Store insertion itself is idempotent.
        for path in sorted(self.directory.glob("pending-*.json")):
            record = json.loads(path.read_text())
            digest = record["normalized_sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Invalid pending capture digest.")
            payload = (self.directory / (digest + ".payload")).read_bytes()
            if _hash(payload) != digest:
                raise ValueError("Pending capture payload checksum mismatch.")
            candles = [Candle(**c) for c in json.loads(payload)]
            if any(c.source != "kraken" or c.data_mode != "import" or
                   (c.asset, c.quote) != (record["asset"], record["quote"]) for c in candles):
                raise ValueError("Pending capture has a foreign source, mode or market.")
            self.store.put_candles(candles)
            with self.store.connection() as con:
                con.execute("INSERT OR IGNORE INTO history_captures VALUES(?,?,?,?,?)",
                            (record["id"], record["asset"], record["quote"],
                             record["materialized_at"], _json(record)))
            path.unlink()

    def _now(self) -> int:
        now = int(self.clock())
        _epoch(now)
        return now

    def _market(self, asset, quote):
        if (asset, quote) not in MARKETS:
            raise ValueError("Select a native Kraken BTC/ETH EUR/USD market.")

    def _space(self, additional=0):
        total = sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())
        if total + additional > self.config.max_disk_bytes:
            raise ValueError("Dataset exceeds its configured disk budget.")

    def _blob(self, payload: bytes) -> str:
        digest = _hash(payload)
        path = self.directory / (digest + ".payload")
        if not path.exists():
            self._space(len(payload))
            path.write_bytes(payload)
        return digest

    def _materialize(self, candles, lineage, *, raw=None):
        now = self._now()
        if lineage["captured_at"] > now:
            raise ValueError("Capture time cannot follow materialization.")
        candles = [replace(c, observed_at=now, data_mode="import") for c in candles]
        normalized = _json([asdict(c) for c in candles]).encode()
        # A capture identity includes its receipt time, never an invented past known-at.
        lineage = {**lineage, "raw_response_sha256": _hash(raw) if raw is not None else None}
        identity = _hash(_json(lineage).encode())
        with self.store.connection() as con:
            prior = con.execute("SELECT payload FROM history_captures WHERE id=?", (identity,)).fetchone()
        if prior:
            return json.loads(prior[0])
        self._space(len(normalized) * 4 + (len(raw) if raw else 0))
        conflicts = []
        old = {c.open_time: c for c in self.store.candles(
            lineage["asset"], source="kraken", quote=lineage["quote"], mode="import", as_of=now)}
        for candle in candles:
            previous = old.get(candle.open_time)
            if previous and replace(previous, observed_at=now) != candle:
                conflicts.append({"open_time": candle.open_time, "previous": asdict(previous),
                                  "replacement": asdict(candle)})
        record = {**lineage, "id": identity, "dataset": self.config.name, "data_mode": "import",
                  "materialized_at": now, "data_version": "sha256:" + identity, "normalized_sha256": self._blob(normalized),
                  "raw_sha256": self._blob(raw) if raw is not None else None,
                  "raw_available": raw is not None, "historical_known_at": "unknown",
                  "revision_rule": "latest_materialization_then_insertion_wins",
                  "conflicts": conflicts, "rows": len(candles)}
        journal = self.directory / ("pending-" + identity + ".json")
        journal_payload = _json(record)
        self._space(len(journal_payload.encode()))
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(journal_payload)
        temporary.replace(journal)
        self.store.put_candles(candles)
        with self.store.connection() as con:
            con.execute("INSERT INTO history_captures VALUES(?,?,?,?,?)",
                        (identity, lineage["asset"], lineage["quote"], now, journal_payload))
        journal.unlink()
        return record

    def stage_archive(self, parts: list[ArchivePart], *, member: str, asset: str,
                      quote: str, captured_at: int, build_metadata: dict | None = None) -> dict:
        """Stage one exact member from an ordered binary-split official local ZIP.

        Expected part SHA256 values are mandatory. The concatenated archive hash
        is computed in the same streaming pass. No download/extraction occurs.
        """
        self._market(asset, quote)
        _epoch(captured_at)
        if captured_at > self._now():
            raise ValueError("Archive receipt time is in the future.")
        expected_member = ("XBT" if asset == "BTC" else asset) + quote + "_60.csv"
        safe = PurePosixPath(member)
        if (len(member.encode()) > self.config.max_path_bytes or safe.is_absolute()
                or ".." in safe.parts or "\\" in member or safe.name != expected_member):
            raise ValueError("Select the exact safe native hourly archive member.")
        cfg = self.config
        if not parts or len(parts) > cfg.max_parts:
            raise ValueError("Archive part count exceeds the configured bound.")
        if len({str(Path(p.path).resolve()) for p in parts}) != len(parts):
            raise ValueError("Archive parts must be distinct and explicitly ordered.")
        with ExitStack() as stack:
            streams, sizes, evidence, stamps = [], [], [], []
            archive_hash = hashlib.sha256()
            for part in parts:
                if not re.fullmatch(r"[0-9a-fA-F]{64}", part.sha256):
                    raise ValueError("Each local archive part requires its expected SHA256.")
                if len(part.path.encode()) > cfg.max_path_bytes:
                    raise ValueError("Archive part path exceeds the configured bound.")
                stream = stack.enter_context(Path(part.path).open("rb"))
                stat = Path(part.path).stat()
                size = stat.st_size
                sizes.append(size)
                if size > cfg.max_part_bytes or sum(sizes) > cfg.max_archive_bytes:
                    raise ValueError("Local archive exceeds the configured byte budget.")
                digest = hashlib.sha256()
                while block := stream.read(1024**2):
                    digest.update(block)
                    archive_hash.update(block)
                if digest.hexdigest() != part.sha256.lower():
                    raise ValueError("Archive part SHA256 checksum mismatch.")
                streams.append(stream)
                stamps.append((stat.st_ino, size, stat.st_mtime_ns))
                evidence.append({"filename": Path(part.path).name, "url": part.url,
                                 "sha256": digest.hexdigest(), "bytes": size})
            virtual = _Parts(streams, sizes, cfg.max_directory_bytes)
            with zipfile.ZipFile(virtual) as archive:
                if len(archive.infolist()) > cfg.max_members:
                    raise ValueError("Archive member count exceeds the configured bound.")
                matches = [i for i in archive.infolist() if i.filename == member]
                if len(matches) != 1:
                    raise ValueError("Exact archive member is missing or duplicated.")
                entry = matches[0]
                if entry.is_dir() or entry.flag_bits & 1 or entry.file_size > cfg.max_member_bytes:
                    raise ValueError("Archive member is encrypted or exceeds the configured bound.")
                self._space(entry.file_size * 5)
                staged = self.directory / expected_member
                try:
                    with archive.open(entry) as source, staged.open("xb") as target:
                        total = 0
                        while block := source.read(1024**2):
                            total += len(block)
                            if total > cfg.max_member_bytes:
                                raise ValueError("Archive member exceeds the configured bound.")
                            target.write(block)
                    for part, stamp in zip(parts, stamps, strict=True):
                        stat = Path(part.path).stat()
                        if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != stamp:
                            raise ValueError("Local archive part changed during staging.")
                    imported = import_history(staged, source="kraken", asset=asset, quote=quote,
                                              observed_at=self._now(), limits=ImportLimits(
                                                  max_file_bytes=cfg.max_member_bytes,
                                                  max_member_bytes=cfg.max_member_bytes,
                                                  max_rows=cfg.max_rows))
                    return self._materialize(imported.candles, {
                        "asset": asset, "quote": quote, "source": "kraken", "timeframe": "1h",
                        "acquisition": "official_local_split_zip", "original_mode": "archive",
                        "captured_at": captured_at, "parts": evidence,
                        "archive_sha256": archive_hash.hexdigest(), "member": member,
                        "member_sha256": imported.provenance["member_sha256"],
                        "build_metadata": build_metadata or {}}, raw=staged.read_bytes())
                finally:
                    staged.unlink(missing_ok=True)

    def append_rest(self, asset: str, quote: str, fetch: Callable, *, since: int, limit: int = 720) -> dict:
        self._market(asset, quote)
        _epoch(since)
        if type(limit) is not int or not 1 <= limit <= min(720, self.config.max_rows):
            raise ValueError("Kraken fetch limit must be between 1 and 720 within the row budget.")
        started = time.monotonic()
        attempt = {"attempted_at": self._now(), "since": since, "limit": limit}
        try:
            capture = fetch(asset, quote, since, limit)
            received = self._now()
            if not isinstance(capture, RESTCapture) or len(capture.rows) > limit:
                raise ValueError("Fetcher must return a bounded RESTCapture.")
            encoded = _json(capture.rows).encode()
            if len(encoded) > self.config.max_capture_bytes or (
                capture.raw is not None and len(capture.raw) > self.config.max_capture_bytes
            ):
                raise ValueError("REST capture exceeds the configured payload budget.")
            candles = []
            seen = {}
            for row in capture.rows:
                if len(row) != 6 or type(row[0]) is not int or row[0] % 3_600_000:
                    raise ValueError("REST capture requires aligned hourly CCXT OHLCV rows.")
                event = row[0] // 1000
                values = row[1:6]
                if (event < 0 or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                        or min(values[:4]) <= 0 or values[4] < 0
                        or values[1] < max(values[0], values[2], values[3])
                        or values[2] > min(values[0], values[1], values[3])):
                    raise ValueError("Invalid REST OHLCV values.")
                if event in seen:
                    if seen[event] != row:
                        raise ValueError("Conflicting duplicate REST candle.")
                    continue
                seen[event] = row
                if event + 3600 <= received:
                    candles.append(Candle("kraken", asset, quote, "1h", event, event + 3600,
                                          *row[1:6], received, data_mode="import"))
            result = self._materialize(candles, {
                "source": "kraken", "asset": asset, "quote": quote, "timeframe": "1h",
                "acquisition": "public_rest", "original_mode": "public_rest_capture",
                "capture_evidence": capture.capture_evidence,
                "captured_at": received, "endpoint": capture.endpoint, "since": since,
                "response_rows_sha256": _hash(encoded)}, raw=capture.raw)
            attempt.update(status="success", captured_at=received, count=len(candles),
                           newest_event=max((c.close_time for c in candles), default=None),
                           bytes=len(capture.raw) if capture.raw is not None else None,
                           normalized_response_bytes=len(encoded))
            return result
        except Exception as exc:
            attempt.update(status="unsupported" if isinstance(exc, UnsupportedMarket) else "error",
                           error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            attempt["duration_seconds"] = time.monotonic() - started
            with self.store.connection() as con:
                con.execute("INSERT INTO history_attempts(asset,quote,payload) VALUES(?,?,?)",
                            (asset, quote, _json(attempt)))

    def plan_repair(self, asset: str, quote: str, start: int, end: int) -> dict:
        self._market(asset, quote)
        _range(start, end)
        state = {"dataset": self.config.name, "mode": "import", "source": "kraken",
                 "asset": asset, "quote": quote, "timeframe": "1h", "start": start,
                 "end": end, "cursor": start, "status": "pending", "requests": 0}
        self._save_repair(asset, quote, state)
        return state

    def _save_repair(self, asset, quote, state):
        with self.store.connection() as con:
            con.execute("INSERT OR REPLACE INTO history_repairs VALUES(?,?)", (asset + quote, _json(state)))

    def repair(self, asset: str, quote: str, fetch: Callable, *, request_budget: int = 1) -> dict:
        self._market(asset, quote)
        if type(request_budget) is not int or not 1 <= request_budget <= self.config.max_requests:
            raise ValueError("Repair request budget exceeds the configured bound.")
        with self.store.connection() as con:
            row = con.execute("SELECT payload FROM history_repairs WHERE market=?", (asset + quote,)).fetchone()
        if row is None:
            raise ValueError("Plan an explicit repair range first.")
        state = json.loads(row[0])
        for _ in range(request_budget):
            # Cursor is the first missing hour in this explicit plan, independent of MAX(event).
            present = {c.open_time for c in self.store.candles(asset, source="kraken", quote=quote,
                                                               mode="import", as_of=self._now())}
            while state["cursor"] < state["end"] and state["cursor"] in present:
                state["cursor"] += 3600
            if state["cursor"] >= state["end"]:
                state["status"] = "complete"
                break
            if state["cursor"] < (self._now() // 3600 - 719) * 3600:
                state["status"] = "retention_limited"
                break
            state["requests"] += 1
            try:
                result = self.append_rest(asset, quote, fetch, since=state["cursor"],
                                          limit=min(720, self.config.max_rows))
                state["last_capture"] = result["id"]
                refreshed = {c.open_time for c in self.store.candles(
                    asset, source="kraken", quote=quote, mode="import", as_of=self._now())}
                previous = state["cursor"]
                while state["cursor"] < state["end"] and state["cursor"] in refreshed:
                    state["cursor"] += 3600
                state["status"] = ("complete" if state["cursor"] >= state["end"] else
                                   "unknown_gap" if state["cursor"] == previous else "pending")
                state.pop("error", None)
            except Exception as exc:
                state["status"] = "unsupported" if isinstance(exc, UnsupportedMarket) else "error"
                state["error"] = f"{type(exc).__name__}: {exc}"
            self._save_repair(asset, quote, state)
            if state["status"] != "pending":
                break
        self._save_repair(asset, quote, state)
        return state

    def manifest(self) -> dict:
        """Four separate coverage records; no access/rights/success inferred from code."""
        records = []
        with self.store.connection() as con:
            captures = [json.loads(r[0]) for r in con.execute("SELECT payload FROM history_captures ORDER BY rowid")]
            attempts = [dict(r) for r in con.execute("SELECT asset,quote,payload FROM history_attempts ORDER BY id")]
            repairs = {r[0]: json.loads(r[1]) for r in con.execute("SELECT market,payload FROM history_repairs")}
        for asset, quote in MARKETS:
            rows = self.store.candles(asset, source="kraken", quote=quote, mode="import", as_of=self._now())
            present = {c.open_time for c in rows}
            gaps, cursor = [], self.config.target_start
            while cursor < self.config.target_end:
                if cursor in present:
                    cursor += 3600
                    continue
                start = cursor
                while cursor < self.config.target_end and cursor not in present:
                    cursor += 3600
                gaps.append({"start": start, "end": cursor, "status": "unknown"})
            own_attempts = [json.loads(a["payload"]) for a in attempts if (a["asset"], a["quote"]) == (asset, quote)]
            own_captures = [c for c in captures if (c["asset"], c["quote"]) == (asset, quote)]
            public_captures = sum(c.get("capture_evidence") == "public_rest_adapter" and c["rows"] > 0
                                  for c in own_captures)
            injected_captures = sum(c.get("capture_evidence", "").startswith("injected_")
                                    for c in own_captures)
            records.append({"asset": asset, "quote": quote, "source": "kraken", "data_mode": "import",
                            "count": len(rows), "first_open_time": rows[0].open_time if rows else None,
                            "last_close_time": rows[-1].close_time if rows else None, "gaps": gaps,
                            "target_hours": (self.config.target_end - self.config.target_start) // 3600,
                            "coverage_status": "complete" if not gaps else "incomplete" if rows else "unavailable",
                            "real_acceptance": "unverified",
                            "capture_evidence": {"public_rest_captures_observed": public_captures,
                                                 "injected_unqualified_captures": injected_captures,
                                                 "report_qualification": "unverified"},
                            "intended_use": "local_research_unqualified",
                            "attempts": own_attempts, "repair": repairs.get(asset + quote)})
        return {"dataset": self.config.name, "source": "kraken", "data_mode": "import",
                "dataset_version": "sha256:" + _hash(_json([c["id"] for c in captures]).encode()),
                "database": str(self.store.path), "target_start": self.config.target_start,
                "target_end": self.config.target_end, "markets": records, "captures": captures,
                "revision_rule": "latest_materialization_then_insertion_wins"}
