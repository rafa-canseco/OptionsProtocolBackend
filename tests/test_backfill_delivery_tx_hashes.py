"""Regression coverage for the delivery transaction backfill script."""

import importlib
import logging
import sys


def test_backfill_module_import_does_not_load_production_config(monkeypatch, tmp_path):
    """Importing helpers must not read credentials or create remote clients."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    sys.modules.pop("scripts._backfill_delivery_tx_hashes", None)
    root_logger = logging.getLogger()
    original_handlers = root_logger.handlers[:]
    original_level = root_logger.level

    try:
        root_logger.handlers.clear()
        root_logger.setLevel(logging.WARNING)
        module = importlib.import_module("scripts._backfill_delivery_tx_hashes")

        assert root_logger.handlers == []
        assert root_logger.level == logging.WARNING
        assert module.db is None
        assert module.w3 is None
        assert module.ctrl is None
    finally:
        root_logger.handlers[:] = original_handlers
        root_logger.setLevel(original_level)
