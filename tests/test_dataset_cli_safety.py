"""Dataset CLI reports must never replace the inputs or retained evidence."""

import json
import os
from dataclasses import asdict

import pytest

from crypto_market_analysis.cli import main
from crypto_market_analysis.dataset_lock import dataset_writer_lock
from crypto_market_analysis.history_dataset import DatasetConfig, HistoryDataset, RESTCapture


def _config(tmp_path):
    config = DatasetConfig("synthetic-safety", str(tmp_path / "dataset"), 0, 3600)
    path = tmp_path / "dataset-config.json"
    path.write_text(json.dumps(asdict(config)))
    return config, path


@pytest.mark.parametrize("target", ["database", "identity", "payload", "config"])
def test_report_output_protected_before_constructor_recovers_pending(tmp_path, monkeypatch, capsys, target):
    config, path = _config(tmp_path)
    dataset = HistoryDataset(config, clock=lambda: 7200)

    def interrupted(_self, _candles):
        raise RuntimeError("synthetic interruption before materialization")

    with monkeypatch.context() as patch:
        patch.setattr("crypto_market_analysis.storage.Store.put_candles", interrupted)
        with pytest.raises(RuntimeError):
            dataset.append_rest("BTC", "EUR", lambda *_: RESTCapture([[0, 10, 10, 10, 10, 1]], b"raw"), since=0)
    pending = list(dataset.directory.glob("pending-*.json"))
    assert len(pending) == 1
    outputs = {"database": dataset.directory / "observer.sqlite", "identity": dataset.directory / "dataset.json",
               "payload": next(dataset.directory.glob("*.payload")), "config": path}
    inventory = {p.name: p.read_bytes() for p in dataset.directory.iterdir() if p.is_file()}
    config_bytes = path.read_bytes()
    assert main(["history-dataset", "status", "--dataset-config", str(path),
                 "--output", str(outputs[target])]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in dataset.directory.iterdir() if p.is_file()} == inventory
    assert path.read_bytes() == config_bytes


def test_new_dataset_report_cannot_replace_config_or_create_dataset(tmp_path, capsys):
    config, path = _config(tmp_path)
    config_bytes = path.read_bytes()
    assert main(["history-dataset", "init", "--dataset-config", str(path), "--output", str(path)]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert path.read_bytes() == config_bytes
    assert not (tmp_path / "dataset").exists()


def test_parts_manifest_is_a_protected_report_input_before_recovery(tmp_path, capsys):
    config, path = _config(tmp_path)
    HistoryDataset(config, clock=lambda: 7200)
    parts = tmp_path / "parts.json"
    parts.write_text('{"parts": []}')
    original = parts.read_bytes()
    assert main(["history-dataset", "stage", "--dataset-config", str(path), "--parts-manifest", str(parts),
                 "--member", "XBTEUR_60.csv", "--captured-at", "0", "--output", str(parts)]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert parts.read_bytes() == original


@pytest.mark.parametrize("action", ["init", "status", "stage", "plan-repair", "refresh", "repair"])
def test_existing_dataset_cli_actions_share_auto_repair_writer_lock(tmp_path, monkeypatch, capsys, action):
    config, path = _config(tmp_path)
    dataset = HistoryDataset(config, clock=lambda: 7200)
    parts = tmp_path / "parts.json"
    parts.write_text('{"parts": []}')

    def forbidden(*_args, **_kwargs):
        pytest.fail("Busy CLI operation must not construct or recover the dataset")

    with dataset_writer_lock(dataset.store.path):
        monkeypatch.setattr("crypto_market_analysis.history_dataset.HistoryDataset", forbidden)
        assert main(["history-dataset", action, "--dataset-config", str(path), "--parts-manifest", str(parts),
                     "--member", "XBTEUR_60.csv", "--captured-at", "0"]) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "busy" and result["reason"] == "dataset_writer_busy"
        assert result["action"] == action and result["applied"] is False


def test_archive_part_cannot_be_overwritten_before_pending_recovery(tmp_path, monkeypatch, capsys):
    import hashlib

    config, path = _config(tmp_path)
    HistoryDataset(config, clock=lambda: 7200)
    archive = tmp_path / "source.zip.001"
    archive.write_bytes(b"unqualified synthetic archive part")
    parts = tmp_path / "parts.json"
    parts.write_text(json.dumps({"parts": [{"path": archive.name, "url": "https://example.invalid/archive",
                                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}))

    def forbidden(*_args, **_kwargs):
        pytest.fail("Report validation must precede dataset construction/recovery")

    monkeypatch.setattr("crypto_market_analysis.history_dataset.HistoryDataset", forbidden)
    assert main(["history-dataset", "stage", "--dataset-config", str(path), "--parts-manifest", str(parts),
                 "--member", "XBTEUR_60.csv", "--captured-at", "0", "--output", str(archive)]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert archive.read_bytes() == b"unqualified synthetic archive part"


@pytest.mark.parametrize("target", ["payload", "configuration", "database"])
def test_external_hardlink_report_cannot_truncate_protected_inode(tmp_path, capsys, target):
    config, path = _config(tmp_path)
    dataset = HistoryDataset(config, clock=lambda: 7200)
    capture = dataset.append_rest("BTC", "EUR", lambda *_: RESTCapture([[0, 10, 10, 10, 10, 1]], b"raw"), since=0)
    inputs = {"payload": dataset.directory / (capture["raw_sha256"] + ".payload"),
              "configuration": path, "database": dataset.store.path}
    source = inputs[target]
    report = tmp_path / "external-report.json"
    try:
        os.link(source, report)
    except (NotImplementedError, OSError):
        pytest.skip("Hardlinks unavailable")
    original = source.read_bytes()
    assert main(["history-dataset", "status", "--dataset-config", str(path), "--output", str(report)]) == 2
    assert "must not overwrite" in capsys.readouterr().err
    assert source.read_bytes() == report.read_bytes() == original


@pytest.mark.parametrize("command,extra", [
    ("maintain", ["--apply"]),
    ("restore-archive", ["--apply", "--archive", "unused.json"]),
    ("collect", []),
    ("demo", []),
    ("import-history", ["--file", "unused.csv", "--source", "kraken", "--quote", "EUR"]),
    ("import-calendar", ["--file", "unused.json"]),
    ("local-workflow", ["--request", "unused.json"]),
])
def test_configured_cli_writers_refuse_busy_dataset_before_engine_or_storage(tmp_path, monkeypatch, capsys, command, extra):
    config, _ = _config(tmp_path)
    dataset = HistoryDataset(config, clock=lambda: 7200)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Busy configured operation must not initialize the Engine or modify storage")

    with dataset_writer_lock(dataset.store.path):
        monkeypatch.setattr("crypto_market_analysis.cli.Engine", forbidden)
        monkeypatch.setattr("crypto_market_analysis.storage_policy.maintain_storage", forbidden)
        monkeypatch.setattr("crypto_market_analysis.storage_policy.restore_archive", forbidden)
        before = {p.name: p.read_bytes() for p in dataset.directory.iterdir() if p.is_file()}
        assert main([command, "--mode", "import", "--data-dir", str(dataset.directory), *extra]) == 1
        result = json.loads(capsys.readouterr().out)
        assert result["reason"] == "dataset_writer_busy" and result["applied"] is False
        assert result["action"] == command
        assert {p.name: p.read_bytes() for p in dataset.directory.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("alias", [False, True])
def test_named_backup_uses_writer_lock_without_creating_destination(tmp_path, monkeypatch, capsys, alias):
    config, _ = _config(tmp_path)
    dataset = HistoryDataset(config, clock=lambda: 7200)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Busy backup must not inspect or publish a cross-file backup")

    monkeypatch.setattr("crypto_market_analysis.recovery.create_backup", forbidden)
    destination = tmp_path / "bundle"
    database = dataset.store.path
    if alias:
        database = tmp_path / "external-database-alias.sqlite"
        try:
            database.symlink_to(dataset.store.path)
        except (NotImplementedError, OSError):
            pytest.skip("Symlinks unavailable")
    with dataset_writer_lock(dataset.store.path):
        assert main(["backup", "--database", str(database), "--destination", str(destination),
                     "--quiescent-dataset"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "dataset_writer_busy"
    assert not destination.exists()
