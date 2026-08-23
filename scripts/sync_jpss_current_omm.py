#!/usr/bin/env python3
"""Publish the latest JPSS OMM elements as a small, validated web snapshot."""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

import requests


CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad_id}&FORMAT=JSON"
SATELLITES = (
    {"satellite_id": "suomi_npp", "satellite": "SUOMI NPP", "short_name": "NPP", "norad_id": 37849},
    {"satellite_id": "jpss_1", "satellite": "NOAA 20 (JPSS-1)", "short_name": "JPSS-1", "norad_id": 43013},
    {"satellite_id": "jpss_2", "satellite": "NOAA 21 (JPSS-2)", "short_name": "JPSS-2", "norad_id": 54234},
)
REQUIRED_NUMERIC_FIELDS = (
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "BSTAR",
    "MEAN_MOTION_DOT",
    "MEAN_MOTION_DDOT",
)


class SnapshotError(RuntimeError):
    """Raised when a source response cannot produce a safe tracking snapshot."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_epoch(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError("OMM EPOCH is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"Invalid OMM EPOCH: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_omm(payload: Any, expected: dict[str, Any], generated_at: datetime) -> dict[str, Any]:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise SnapshotError(f"Expected one OMM record for NORAD {expected['norad_id']}")

    omm = payload[0]
    try:
        norad_id = int(omm["NORAD_CAT_ID"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError("OMM NORAD_CAT_ID is missing or invalid") from exc
    if norad_id != expected["norad_id"]:
        raise SnapshotError(f"NORAD mismatch: expected {expected['norad_id']}, got {norad_id}")

    for field in REQUIRED_NUMERIC_FIELDS:
        try:
            number = float(omm[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError(f"OMM {field} is missing or invalid for NORAD {norad_id}") from exc
        if not math.isfinite(number):
            raise SnapshotError(f"OMM {field} is not finite for NORAD {norad_id}")

    mean_motion = float(omm["MEAN_MOTION"])
    eccentricity = float(omm["ECCENTRICITY"])
    inclination = float(omm["INCLINATION"])
    if not 10 <= mean_motion <= 20:
        raise SnapshotError(f"Unexpected JPSS mean motion for NORAD {norad_id}: {mean_motion}")
    if not 0 <= eccentricity < 1:
        raise SnapshotError(f"Invalid eccentricity for NORAD {norad_id}: {eccentricity}")
    if not 0 <= inclination <= 180:
        raise SnapshotError(f"Invalid inclination for NORAD {norad_id}: {inclination}")

    epoch = parse_epoch(omm.get("EPOCH"))
    age_hours = (generated_at - epoch).total_seconds() / 3600
    if age_hours < -1:
        raise SnapshotError(f"OMM epoch is in the future for NORAD {norad_id}")
    if age_hours > 168:
        raise SnapshotError(f"OMM is older than 7 days for NORAD {norad_id}")

    return {
        **expected,
        "object_id": omm.get("OBJECT_ID"),
        "epoch_utc": iso_utc(epoch),
        "age_hours_at_sync": round(max(0.0, age_hours), 2),
        "omm": omm,
    }


def fetch_json(url: str, attempts: int = 3, timeout_seconds: int = 20) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": "JPSS-Planning-Tracker/1.0"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            retryable = status_code == 429 or 500 <= status_code < 600
            if not retryable or attempt == attempts:
                raise SnapshotError(f"CelesTrak HTTP {status_code}") from exc
        except (requests.RequestException, json.JSONDecodeError) as exc:
            if attempt == attempts:
                raise SnapshotError(f"CelesTrak request failed: {exc}") from exc
        time.sleep(2 ** (attempt - 1))
    raise SnapshotError("CelesTrak request failed")


def build_snapshot(
    fetcher: Callable[[str], Any] = fetch_json,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    records = []
    for expected in SATELLITES:
        url = CELESTRAK_URL.format(norad_id=expected["norad_id"])
        records.append(validate_omm(fetcher(url), expected, generated_at))

    return {
        "schema_version": "1.0",
        "generated_at": iso_utc(generated_at),
        "source": {
            "provider": "CelesTrak",
            "data_type": "CCSDS OMM / General Perturbations",
            "query_template": CELESTRAK_URL,
            "documentation": "https://celestrak.org/NORAD/documentation/gp-data-formats.php",
        },
        "position_contract": {
            "method": "Browser-side SGP4 propagation from the latest OMM",
            "temporal_reference": "UTC",
            "crs": "OGC:CRS84",
            "coordinate_order": ["longitude", "latitude"],
            "altitude_unit": "km",
            "telemetry": False,
        },
        "satellites": records,
    }


def write_snapshot(snapshot: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=output_path.parent, delete=False) as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("satellite_data/current_omm.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = build_snapshot()
    write_snapshot(snapshot, args.output)
    epochs = ", ".join(f"{item['short_name']}={item['epoch_utc']}" for item in snapshot["satellites"])
    print(f"[INFO] Published {len(snapshot['satellites'])} JPSS OMM records to {args.output}")
    print(f"[INFO] Epochs: {epochs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
