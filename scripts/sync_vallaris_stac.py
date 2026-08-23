#!/usr/bin/env python3
"""Create a public, credential-free web snapshot from Vallaris JPSS STAC.

The API key is read only from ``VALLARIS_API_KEY`` and is added at the HTTP
boundary. It is never written to output files or printed in error messages.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


DEFAULT_SEARCH_URL = (
    "https://beta.vallarismaps.com/core/api/stac/1.0/"
    "jpss_planning/search"
)
ALLOWED_HOST = "beta.vallarismaps.com"
PASS_TYPES = {"visible_track", "aos", "tca", "los"}
GEOMETRY_TYPES = {"LineString", "Point"}
TYPE_ORDER = {"visible_track": 0, "aos": 1, "tca": 2, "los": 3}
INTERNAL_PROPERTIES = {
    "_id",
    "_createdAt",
    "_createdBy",
    "_updatedAt",
    "_updatedBy",
}


class SyncError(RuntimeError):
    """Raised when remote metadata or feature data violate the web contract."""


def safe_url(value: str) -> str:
    """Return a URL suitable for diagnostics without credentials."""
    parsed = urlparse(value)
    query = [(key, "<redacted>" if key.lower() == "api_key" else val) for key, val in parse_qsl(parsed.query)]
    return urlunparse(parsed._replace(query=urlencode(query)))


def authenticated_url(value: str, api_key: str, **extra_query: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise SyncError(f"Refusing untrusted Vallaris URL: {safe_url(value)}")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(extra_query)
    query["api_key"] = api_key
    return urlunparse(parsed._replace(query=urlencode(query)))


def request_json(value: str, api_key: str, *, attempts: int = 4) -> dict[str, Any]:
    target = authenticated_url(value, api_key)
    for attempt in range(attempts):
        try:
            response = requests.get(
                target,
                headers={"Accept": "application/geo+json, application/json", "User-Agent": "jpss-planning-stac-sync/1.0"},
                timeout=(10, 30),
            )
            if response.status_code >= 400:
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable or attempt == attempts - 1:
                    raise SyncError(f"Vallaris HTTP {response.status_code} at {safe_url(value)}")
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt + random.random()
                time.sleep(min(delay, 15))
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                raise SyncError(f"Expected a JSON object from {safe_url(value)}")
            return payload
        except (requests.RequestException, requests.JSONDecodeError) as error:
            if attempt == attempts - 1:
                raise SyncError(f"Unable to read Vallaris JSON at {safe_url(value)}: {type(error).__name__}") from None
            time.sleep(min(2**attempt + random.random(), 15))
    raise AssertionError("request retry loop exhausted")


def next_link(document: dict[str, Any]) -> str | None:
    for link in document.get("links", []):
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            return str(link["href"])
    return None


def collect_stac_items(search_url: str, api_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    current: str | None = search_url
    while current:
        clean = safe_url(current)
        if clean in seen_urls:
            raise SyncError(f"STAC pagination loop detected at {clean}")
        seen_urls.add(clean)
        page = request_json(current, api_key)
        if page.get("type") != "FeatureCollection" or not isinstance(page.get("features"), list):
            raise SyncError("STAC search response must be a FeatureCollection")
        items.extend(item for item in page["features"] if isinstance(item, dict))
        current = next_link(page)
    return items


def choose_data_asset(item: dict[str, Any]) -> dict[str, Any]:
    assets = item.get("assets")
    if not isinstance(assets, dict):
        raise SyncError(f"STAC Item {item.get('id')} has no assets")
    preferred = assets.get("data")
    if isinstance(preferred, dict) and preferred.get("href"):
        return preferred
    for asset in assets.values():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue
        roles = set(asset.get("roles") or [])
        media_type = str(asset.get("type") or "")
        if roles.intersection({"data", "features"}) or "geo+json" in media_type:
            return asset
    raise SyncError(f"STAC Item {item.get('id')} has no Feature data asset")


def collect_feature_pages(asset_href: str, api_key: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    current: str | None = authenticated_url(asset_href, api_key, limit="10000")
    while current:
        clean = safe_url(current)
        if clean in seen_urls:
            raise SyncError(f"Feature pagination loop detected at {clean}")
        seen_urls.add(clean)
        page = request_json(current, api_key)
        if page.get("type") != "FeatureCollection" or not isinstance(page.get("features"), list):
            raise SyncError("Vallaris Feature asset must be a FeatureCollection")
        features.extend(feature for feature in page["features"] if isinstance(feature, dict))
        current = next_link(page)
    return features


def coordinate_pairs(coordinates: Any) -> Iterable[tuple[float, float]]:
    if (
        isinstance(coordinates, list)
        and len(coordinates) >= 2
        and isinstance(coordinates[0], (int, float))
        and isinstance(coordinates[1], (int, float))
    ):
        yield float(coordinates[0]), float(coordinates[1])
        return
    if isinstance(coordinates, list):
        for child in coordinates:
            yield from coordinate_pairs(child)


def clean_and_validate_features(features: list[dict[str, Any]], planning_date: str) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    stable_ids: set[str] = set()
    pass_parts: dict[str, set[str]] = {}
    for feature in features:
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            raise SyncError("Every Feature must contain geometry and properties objects")
        feature_type = str(properties.get("type") or "")
        if feature_type not in PASS_TYPES:
            continue
        geometry_type = geometry.get("type")
        if geometry_type not in GEOMETRY_TYPES:
            raise SyncError(f"Unsupported {geometry_type} geometry for {feature_type}")
        if feature_type == "visible_track" and geometry_type != "LineString":
            raise SyncError("visible_track geometry must be LineString")
        if feature_type != "visible_track" and geometry_type != "Point":
            raise SyncError(f"{feature_type} geometry must be Point")
        pairs = list(coordinate_pairs(geometry.get("coordinates")))
        if not pairs:
            raise SyncError(f"Empty geometry for Feature {feature.get('id')}")
        if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in pairs):
            raise SyncError(f"Coordinate outside CRS84 range for Feature {feature.get('id')}")
        if str(properties.get("planning_date") or planning_date) != planning_date:
            raise SyncError(f"Feature planning_date does not match STAC Item {planning_date}")
        pass_id = str(properties.get("pass_id") or "")
        if not pass_id:
            raise SyncError("Every JPSS pass Feature must contain pass_id")
        if not properties.get("satellite"):
            raise SyncError(f"Feature {feature.get('id')} has no satellite property")
        feature_id = str(feature.get("id") or properties.get("_id") or f"{pass_id}_{feature_type}")
        if feature_id in stable_ids:
            raise SyncError(f"Duplicate Feature id: {feature_id}")
        stable_ids.add(feature_id)
        pass_parts.setdefault(pass_id, set()).add(feature_type)
        public_properties = {key: value for key, value in properties.items() if key not in INTERNAL_PROPERTIES}
        public_properties["planning_date"] = planning_date
        cleaned.append({"type": "Feature", "id": feature_id, "geometry": geometry, "properties": public_properties})

    incomplete = {pass_id: sorted(PASS_TYPES - parts) for pass_id, parts in pass_parts.items() if parts != PASS_TYPES}
    if incomplete:
        first_pass, missing = next(iter(incomplete.items()))
        raise SyncError(f"Incomplete pass {first_pass}; missing {', '.join(missing)}")
    cleaned.sort(
        key=lambda feature: (
            str(feature["properties"].get("tca_utc") or ""),
            str(feature["properties"].get("satellite") or ""),
            TYPE_ORDER[str(feature["properties"]["type"])],
        )
    )
    return cleaned


def planning_date_for(item: dict[str, Any]) -> str:
    properties = item.get("properties") or {}
    candidate = properties.get("planning_date") or properties.get("datetime")
    if not candidate:
        raise SyncError(f"STAC Item {item.get('id')} has no planning date")
    value = str(candidate)[:10]
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SyncError(f"Invalid planning date in STAC Item {item.get('id')}") from None
    return value


def public_item_metadata(item: dict[str, Any], planning_date: str) -> dict[str, Any]:
    properties = item.get("properties") or {}
    return {
        "id": item.get("id"),
        "collection": item.get("collection"),
        "bbox": item.get("bbox"),
        "geometry": item.get("geometry"),
        "planning_date": planning_date,
        "datetime": properties.get("datetime"),
        "start_datetime": properties.get("start_datetime"),
        "end_datetime": properties.get("end_datetime"),
        "feature_collection_title": properties.get("feature_collection_title"),
        "feature_count": properties.get("feature_count"),
        "pass_count": properties.get("pass_count"),
        "source": "Vallaris STAC jpss_planning",
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def sync(search_url: str, output_dir: Path, api_key: str) -> dict[str, Any]:
    items = collect_stac_items(search_url, api_key)
    if not items:
        raise SyncError("Vallaris STAC search returned no JPSS planning Items")

    records: list[dict[str, Any]] = []
    seen_dates: set[str] = set()
    for item in items:
        planning_date = planning_date_for(item)
        if planning_date in seen_dates:
            raise SyncError(f"More than one STAC Item found for planning date {planning_date}")
        seen_dates.add(planning_date)
        asset = choose_data_asset(item)
        features = clean_and_validate_features(collect_feature_pages(str(asset["href"]), api_key), planning_date)
        tracks = [feature for feature in features if feature["properties"]["type"] == "visible_track"]
        passes = [dict(feature["properties"]) for feature in tracks]
        date_dir = output_dir / planning_date
        atomic_json(date_dir / "jpss_passes.geojson", {"type": "FeatureCollection", "features": features})
        atomic_json(date_dir / "jpss_passes.json", passes)
        atomic_json(date_dir / "stac_item.json", public_item_metadata(item, planning_date))
        records.append(
            {
                "id": str(item.get("collection") or item.get("id") or planning_date),
                "date": planning_date,
                "stac_item_id": item.get("id"),
                "stac_collection_id": item.get("collection"),
                "feature_path": f"stac_data/{planning_date}/jpss_passes.geojson",
                "passes_path": f"stac_data/{planning_date}/jpss_passes.json",
                "metadata_path": f"stac_data/{planning_date}/stac_item.json",
                "feature_count": len(features),
                "pass_count": len(passes),
                "bbox": item.get("bbox"),
                "datetime": (item.get("properties") or {}).get("datetime"),
            }
        )

    records.sort(key=lambda record: record["date"])
    index = {
        "schema_version": 1,
        "source": "Vallaris STAC jpss_planning",
        "source_endpoint": DEFAULT_SEARCH_URL,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "crs": "OGC:CRS84",
        "axis_order": ["longitude", "latitude"],
        "datasets": records,
        "default_dataset_id": records[-1]["id"],
    }
    atomic_json(output_dir / "index.json", index)
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-url", default=DEFAULT_SEARCH_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("stac_data"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("VALLARIS_API_KEY", "").strip()
    if not api_key:
        print("[ERROR] VALLARIS_API_KEY is required", file=sys.stderr)
        return 2
    try:
        index = sync(args.search_url, args.output_dir, api_key)
    except SyncError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1
    feature_count = sum(int(record["feature_count"]) for record in index["datasets"])
    pass_count = sum(int(record["pass_count"]) for record in index["datasets"])
    print(f"[INFO] Synced {len(index['datasets'])} planning day(s), {pass_count} pass(es), {feature_count} feature(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
