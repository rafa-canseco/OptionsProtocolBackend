"""Validate and persist a signed CSP option observation."""

import argparse
import json
from pathlib import Path

from src.fund_nav.ingestion import ingest_observation_document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "observation",
        type=Path,
        help="JSON document containing one signed, block-bound observation",
    )
    args = parser.parse_args()

    observer = ingest_observation_document(json.loads(args.observation.read_text()))
    print(f"Stored verified observation from {observer}")


if __name__ == "__main__":
    main()
