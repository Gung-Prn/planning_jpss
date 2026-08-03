#!/usr/bin/env python3
"""Create a JPSS station pass plan from CelesTrak TLEs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent / ".python_deps"))

from sgp4.api import Satrec, jday  # type: ignore

try:
    import certifi  # type: ignore
except ImportError:  # pragma: no cover - only used on minimal Python installs.
    certifi = None

try:
    from shapely.geometry import LineString, Point, Polygon, mapping, shape  # type: ignore
    from shapely.ops import unary_union  # type: ignore
except ImportError:  # pragma: no cover - only required when --swath-file is used.
    Point = None
    Polygon = None
    LineString = None
    mapping = None
    shape = None
    unary_union = None


EARTH_RADIUS_KM = 6371.0088
WGS84_A_KM = 6378.137
WGS84_F = 1 / 298.257223563
DEFAULT_BUFFER_KM = 2500.0
DEFAULT_MIN_ELEVATION_DEG = 5.0
DEFAULT_STEP_SECONDS = 30
SATELLITES = {
    "Suomi-NPP": "37849",
    "JPSS-1": "43013",
    "JPSS-2": "54234",
}


@dataclass(frozen=True)
class GroundPoint:
    time: datetime
    lon: float
    lat: float
    distance_km: float
    ecef_km: tuple[float, float, float]
    altitude_km: float
    azimuth_deg: float
    elevation_deg: float
    slant_range_km: float


@dataclass(frozen=True)
class PassEvent:
    satellite: str
    direction: str
    start: GroundPoint
    end: GroundPoint
    tca: GroundPoint
    max_elevation_point: GroundPoint
    min_distance_km: float
    min_elevation_deg: float
    tle_epoch: datetime
    tle_age_hours: float
    duration_minutes: float
    track: list[GroundPoint]
    aoi_covered: bool
    aoi_points_inside: int


@dataclass(frozen=True)
class SwathReference:
    path: Path
    features: list[dict[str, object]]
    source_count: int
    station_inside_count: int
    buffer_intersects_count: int
    satellite_names: list[str]
    sensors: list[str]
    datetimes: list[str]


@dataclass(frozen=True)
class AoiReference:
    path: Path
    geometry: object
    feature_count: int


def read_station(path: Path) -> tuple[float, float]:
    data = json.loads(path.read_text())
    for feature in data.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "Point":
            lon, lat = geometry["coordinates"][:2]
            return float(lon), float(lat)
    raise ValueError(f"No Point feature found in {path}")


def load_aoi_reference(path: Path) -> AoiReference:
    if shape is None or unary_union is None:
        raise RuntimeError("Shapely is required for --aoi-file. Install it with `python3 -m pip install shapely`.")
    data = json.loads(path.read_text())
    geometries = [shape(feature["geometry"]) for feature in data.get("features", []) if feature.get("geometry")]
    if not geometries:
        raise ValueError(f"No polygon geometry found in {path}")
    geometry = unary_union(geometries)
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"AOI must contain Polygon or MultiPolygon geometry: {geometry.geom_type}")
    return AoiReference(path=path, geometry=geometry, feature_count=len(geometries))


def fetch_tle(catnr: str, cache_dir: Path) -> tuple[str, str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{catnr}.tle"
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=TLE"
    try:
        context = ssl.create_default_context(cafile=certifi.where()) if certifi else None
        with urllib.request.urlopen(url, timeout=30, context=context) as response:
            text = response.read().decode("utf-8").strip()
        cache_path.write_text(text + "\n")
    except Exception:
        if not cache_path.exists():
            raise
        text = cache_path.read_text().strip()

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        raise ValueError(f"Invalid TLE response for CATNR {catnr}: {text!r}")
    return lines[0], lines[1], lines[2]


def parse_tle_epoch(line1: str) -> datetime:
    year = int(line1[18:20])
    day_of_year = float(line1[20:32])
    full_year = 1900 + year if year >= 57 else 2000 + year
    start_of_year = datetime(full_year, 1, 1, tzinfo=UTC)
    return start_of_year + timedelta(days=day_of_year - 1)


def tle_quality(age_hours: float) -> str:
    age_days = abs(age_hours) / 24.0
    if age_days <= 3:
        return "good"
    if age_days <= 14:
        return "medium"
    return "stale"


def scalar_property(value: object) -> object:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def load_swath_reference(path: Path, station_lon: float, station_lat: float, buffer_km: float) -> SwathReference:
    if Point is None or Polygon is None or shape is None:
        raise RuntimeError("Shapely is required for --swath-file. Install it with `python3 -m pip install shapely`.")

    data = json.loads(path.read_text())
    station_point = Point(station_lon, station_lat)
    buffer_polygon = Polygon(geodesic_circle(station_lon, station_lat, buffer_km))
    output_features: list[dict[str, object]] = []
    station_inside_count = 0
    buffer_intersects_count = 0
    satellite_names: set[str] = set()
    sensors: set[str] = set()
    datetimes: list[str] = []

    for index, feature in enumerate(data.get("features", [])):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        geometry_shape = shape(geometry)
        properties = feature.get("properties") or {}
        station_inside = bool(geometry_shape.covers(station_point))
        buffer_intersects = bool(geometry_shape.intersects(buffer_polygon))
        station_inside_count += int(station_inside)
        buffer_intersects_count += int(buffer_intersects)

        satellite_name = str(properties.get("satellite_name") or properties.get("satellite") or "")
        sensor = str(properties.get("sensor") or properties.get("sensor_name") or "")
        datetime_value = str(properties.get("datetime") or "")
        if satellite_name:
            satellite_names.add(satellite_name)
        if sensor:
            sensors.add(sensor)
        if datetime_value:
            datetimes.append(datetime_value)

        minx, miny, maxx, maxy = geometry_shape.bounds
        output_features.append(
            {
                "type": "Feature",
                "properties": {
                    "type": "reference_swath",
                    "source": path.name,
                    "source_index": index,
                    "satellite": properties.get("satellite"),
                    "satellite_name": properties.get("satellite_name"),
                    "sensor": properties.get("sensor"),
                    "sensor_name": properties.get("sensor_name"),
                    "product": properties.get("product"),
                    "datetime": datetime_value or None,
                    "filename": properties.get("filename"),
                    "station_inside": station_inside,
                    "station_buffer_intersects": buffer_intersects,
                    "bounds": [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)],
                    "planning_note": "reference swath sample; not time-matched to the generated TLE planning window",
                },
                "geometry": geometry,
            }
        )

    return SwathReference(
        path=path,
        features=output_features,
        source_count=len(output_features),
        station_inside_count=station_inside_count,
        buffer_intersects_count=buffer_intersects_count,
        satellite_names=sorted(satellite_names),
        sensors=sorted(sensors),
        datetimes=sorted(datetimes),
    )


def gmst_radians(time: datetime) -> float:
    jd, fr = jday(
        time.year,
        time.month,
        time.day,
        time.hour,
        time.minute,
        time.second + time.microsecond / 1_000_000,
    )
    days_since_j2000 = (jd + fr) - 2451545.0
    gmst_deg = 280.46061837 + 360.98564736629 * days_since_j2000
    return math.radians(gmst_deg % 360.0)


def eci_to_ecef(time: datetime, position_km: Iterable[float]) -> tuple[float, float, float]:
    x, y, z = position_km
    theta = gmst_radians(time)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return cos_t * x + sin_t * y, -sin_t * x + cos_t * y, z


def ecef_to_lon_lat_alt(ecef_km: tuple[float, float, float]) -> tuple[float, float, float]:
    x_ecef, y_ecef, z = ecef_km
    lon = math.degrees(math.atan2(y_ecef, x_ecef))
    hyp = math.hypot(x_ecef, y_ecef)
    lat = math.degrees(math.atan2(z, hyp))
    altitude = math.sqrt(x_ecef * x_ecef + y_ecef * y_ecef + z * z) - EARTH_RADIUS_KM
    return normalize_lon(lon), lat, altitude


def normalize_lon(lon: float) -> float:
    return ((lon + 180.0) % 360.0) - 180.0


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(normalize_lon(lon2 - lon1))
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def destination_point(lon: float, lat: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    angular_distance = distance_km / EARTH_RADIUS_KM
    bearing = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)

    sin_phi2 = (
        math.sin(phi1) * math.cos(angular_distance)
        + math.cos(phi1) * math.sin(angular_distance) * math.cos(bearing)
    )
    phi2 = math.asin(sin_phi2)
    lambda2 = lambda1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(phi1),
        math.cos(angular_distance) - math.sin(phi1) * sin_phi2,
    )
    return normalize_lon(math.degrees(lambda2)), math.degrees(phi2)


def geodesic_circle(lon: float, lat: float, radius_km: float, vertices: int = 180) -> list[list[float]]:
    ring = [
        list(destination_point(lon, lat, bearing_deg=360.0 * index / vertices, distance_km=radius_km))
        for index in range(vertices)
    ]
    ring.append(ring[0])
    return ring


def station_ecef(lon: float, lat: float, height_km: float = 0.0) -> tuple[float, float, float]:
    lon_rad = math.radians(lon)
    lat_rad = math.radians(lat)
    e2 = WGS84_F * (2 - WGS84_F)
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)
    prime_vertical = WGS84_A_KM / math.sqrt(1 - e2 * sin_lat * sin_lat)
    x = (prime_vertical + height_km) * cos_lat * math.cos(lon_rad)
    y = (prime_vertical + height_km) * cos_lat * math.sin(lon_rad)
    z = (prime_vertical * (1 - e2) + height_km) * sin_lat
    return x, y, z


def topocentric_az_el(
    satellite_ecef: tuple[float, float, float],
    station_lon: float,
    station_lat: float,
    station_position: tuple[float, float, float],
) -> tuple[float, float, float]:
    rx = satellite_ecef[0] - station_position[0]
    ry = satellite_ecef[1] - station_position[1]
    rz = satellite_ecef[2] - station_position[2]
    lon_rad = math.radians(station_lon)
    lat_rad = math.radians(station_lat)
    sin_lon = math.sin(lon_rad)
    cos_lon = math.cos(lon_rad)
    sin_lat = math.sin(lat_rad)
    cos_lat = math.cos(lat_rad)

    east = -sin_lon * rx + cos_lon * ry
    north = -sin_lat * cos_lon * rx - sin_lat * sin_lon * ry + cos_lat * rz
    up = cos_lat * cos_lon * rx + cos_lat * sin_lon * ry + sin_lat * rz

    slant_range = math.sqrt(east * east + north * north + up * up)
    azimuth = math.degrees(math.atan2(east, north)) % 360.0
    elevation = math.degrees(math.asin(up / slant_range))
    return azimuth, elevation, slant_range


def propagate(sat: Satrec, time: datetime, station_lon: float, station_lat: float) -> GroundPoint:
    jd, fr = jday(
        time.year,
        time.month,
        time.day,
        time.hour,
        time.minute,
        time.second + time.microsecond / 1_000_000,
    )
    error, position, _velocity = sat.sgp4(jd, fr)
    if error:
        raise RuntimeError(f"SGP4 propagation error {error} at {time.isoformat()}")
    ecef = eci_to_ecef(time, position)
    lon, lat, altitude = ecef_to_lon_lat_alt(ecef)
    station_position = station_ecef(station_lon, station_lat)
    azimuth, elevation, slant_range = topocentric_az_el(ecef, station_lon, station_lat, station_position)
    distance = haversine_km(station_lon, station_lat, lon, lat)
    return GroundPoint(
        time=time,
        lon=lon,
        lat=lat,
        distance_km=distance,
        ecef_km=ecef,
        altitude_km=altitude,
        azimuth_deg=azimuth,
        elevation_deg=elevation,
        slant_range_km=slant_range,
    )


def interpolate_point(
    first: GroundPoint,
    second: GroundPoint,
    fraction: float,
    *,
    distance_km: float | None = None,
    elevation_deg: float | None = None,
) -> GroundPoint:
    fraction = min(1.0, max(0.0, fraction))
    seconds = (second.time - first.time).total_seconds()
    lon_delta = normalize_lon(second.lon - first.lon)
    az_delta = ((second.azimuth_deg - first.azimuth_deg + 180.0) % 360.0) - 180.0
    ecef = tuple(
        first.ecef_km[index] + (second.ecef_km[index] - first.ecef_km[index]) * fraction
        for index in range(3)
    )
    return GroundPoint(
        time=first.time + timedelta(seconds=seconds * fraction),
        lon=normalize_lon(first.lon + lon_delta * fraction),
        lat=first.lat + (second.lat - first.lat) * fraction,
        distance_km=distance_km
        if distance_km is not None
        else first.distance_km + (second.distance_km - first.distance_km) * fraction,
        ecef_km=ecef,  # type: ignore[arg-type]
        altitude_km=first.altitude_km + (second.altitude_km - first.altitude_km) * fraction,
        azimuth_deg=(first.azimuth_deg + az_delta * fraction) % 360.0,
        elevation_deg=elevation_deg
        if elevation_deg is not None
        else first.elevation_deg + (second.elevation_deg - first.elevation_deg) * fraction,
        slant_range_km=first.slant_range_km + (second.slant_range_km - first.slant_range_km) * fraction,
    )


def interpolate_boundary(
    inside: GroundPoint,
    outside: GroundPoint,
    buffer_km: float,
    entering: bool,
) -> GroundPoint:
    denom = outside.distance_km - inside.distance_km
    fraction_from_inside = 0.0 if abs(denom) < 1e-9 else (buffer_km - inside.distance_km) / denom
    fraction_from_inside = min(1.0, max(0.0, fraction_from_inside))

    if entering:
        fraction = 1.0 - fraction_from_inside
        return interpolate_point(outside, inside, fraction, distance_km=buffer_km)
    fraction = fraction_from_inside
    return interpolate_point(inside, outside, fraction, distance_km=buffer_km)


def interpolate_elevation_boundary(
    visible: GroundPoint,
    hidden: GroundPoint,
    min_elevation_deg: float,
    entering: bool,
) -> GroundPoint:
    denom = hidden.elevation_deg - visible.elevation_deg
    fraction_from_visible = (
        0.0 if abs(denom) < 1e-9 else (min_elevation_deg - visible.elevation_deg) / denom
    )
    fraction_from_visible = min(1.0, max(0.0, fraction_from_visible))
    if entering:
        return interpolate_point(hidden, visible, 1.0 - fraction_from_visible, elevation_deg=min_elevation_deg)
    return interpolate_point(visible, hidden, fraction_from_visible, elevation_deg=min_elevation_deg)


def find_passes(
    name: str,
    sat: Satrec,
    station_lon: float,
    station_lat: float,
    start_time: datetime,
    end_time: datetime,
    buffer_km: float,
    min_elevation_deg: float,
    tle_epoch: datetime,
    step_seconds: int,
    aoi: AoiReference | None = None,
) -> list[PassEvent]:
    points: list[GroundPoint] = []
    current = start_time
    while current <= end_time:
        points.append(propagate(sat, current, station_lon, station_lat))
        current += timedelta(seconds=step_seconds)

    passes: list[PassEvent] = []
    active: list[GroundPoint] = []
    start_boundary: GroundPoint | None = None

    previous = points[0]
    if previous.elevation_deg >= min_elevation_deg:
        start_boundary = previous
        active.append(previous)

    for point in points[1:]:
        was_visible = previous.elevation_deg >= min_elevation_deg
        is_visible = point.elevation_deg >= min_elevation_deg
        if not was_visible and is_visible:
            start_boundary = interpolate_elevation_boundary(point, previous, min_elevation_deg, entering=True)
            active = [start_boundary, point]
        elif was_visible and is_visible:
            active.append(point)
        elif was_visible and not is_visible and start_boundary is not None:
            end_boundary = interpolate_elevation_boundary(previous, point, min_elevation_deg, entering=False)
            active.append(end_boundary)
            if len(active) >= 3:
                passes.append(build_pass(name, start_boundary, end_boundary, active, min_elevation_deg, tle_epoch, aoi))
            active = []
            start_boundary = None
        previous = point

    return passes


def build_pass(
    name: str,
    start: GroundPoint,
    end: GroundPoint,
    track: list[GroundPoint],
    min_elevation_deg: float,
    tle_epoch: datetime,
    aoi: AoiReference | None = None,
) -> PassEvent:
    mid_index = len(track) // 2
    before = track[max(0, mid_index - 1)]
    after = track[min(len(track) - 1, mid_index + 1)]
    direction = "ascending" if after.lat >= before.lat else "descending"
    max_elevation_point = max(track, key=lambda point: point.elevation_deg)
    tca = min(track, key=lambda point: point.distance_km)
    min_distance = min(point.distance_km for point in track)
    duration = (end.time - start.time).total_seconds() / 60.0
    aoi_points_inside = 0
    aoi_covered = False
    if aoi is not None and Point is not None and LineString is not None:
        aoi_points_inside = sum(int(aoi.geometry.covers(Point(point.lon, point.lat))) for point in track)
        aoi_covered = bool(LineString([(point.lon, point.lat) for point in track]).intersects(aoi.geometry))
    return PassEvent(
        satellite=name,
        direction=direction,
        start=start,
        end=end,
        tca=tca,
        max_elevation_point=max_elevation_point,
        min_distance_km=min_distance,
        min_elevation_deg=min_elevation_deg,
        tle_epoch=tle_epoch,
        tle_age_hours=(start.time - tle_epoch).total_seconds() / 3600.0,
        duration_minutes=duration,
        track=track,
        aoi_covered=aoi_covered,
        aoi_points_inside=aoi_points_inside,
    )


def iso(time: datetime) -> str:
    return time.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pass_to_record(event: PassEvent, swath_reference: SwathReference | None = None) -> dict[str, object]:
    record: dict[str, object] = {
        "satellite": event.satellite,
        "direction": event.direction,
        "planning_method": "topocentric_elevation_access",
        "min_elevation_deg": round(event.min_elevation_deg, 2),
        "tle_epoch_utc": iso(event.tle_epoch),
        "tle_age_hours": round(event.tle_age_hours, 2),
        "tle_quality": tle_quality(event.tle_age_hours),
        "aos_utc": iso(event.start.time),
        "los_utc": iso(event.end.time),
        "tca_utc": iso(event.tca.time),
        "max_elevation_utc": iso(event.max_elevation_point.time),
        "duration_minutes": round(event.duration_minutes, 2),
        "min_distance_km": round(event.min_distance_km, 2),
        "max_elevation_deg": round(event.max_elevation_point.elevation_deg, 2),
        "tca_azimuth_deg": round(event.tca.azimuth_deg, 2),
        "tca_elevation_deg": round(event.tca.elevation_deg, 2),
        "tca_slant_range_km": round(event.tca.slant_range_km, 2),
        "closest_lon": round(event.tca.lon, 6),
        "closest_lat": round(event.tca.lat, 6),
        "aos_lon": round(event.start.lon, 6),
        "aos_lat": round(event.start.lat, 6),
        "aos_azimuth_deg": round(event.start.azimuth_deg, 2),
        "los_lon": round(event.end.lon, 6),
        "los_lat": round(event.end.lat, 6),
        "los_azimuth_deg": round(event.end.azimuth_deg, 2),
        "confidence": "medium",
        "aoi_covered": event.aoi_covered,
        "aoi_points_inside": event.aoi_points_inside,
    }
    if swath_reference is not None:
        record.update(
            {
                "reference_swath_file": swath_reference.path.name,
                "reference_swath_source_count": swath_reference.source_count,
                "reference_swath_station_inside_count": swath_reference.station_inside_count,
                "reference_swath_buffer_intersects_count": swath_reference.buffer_intersects_count,
                "reference_swath_satellites": ";".join(swath_reference.satellite_names),
                "reference_swath_sensors": ";".join(swath_reference.sensors),
                "reference_swath_time_min": swath_reference.datetimes[0] if swath_reference.datetimes else None,
                "reference_swath_time_max": swath_reference.datetimes[-1] if swath_reference.datetimes else None,
                "reference_swath_note": "sample footprint only; not time-matched to this planning pass",
            }
        )
    return record


def write_outputs(
    events: list[PassEvent],
    output_dir: Path,
    station_lon: float,
    station_lat: float,
    buffer_km: float,
    min_elevation_deg: float,
    swath_reference: SwathReference | None = None,
    aoi: AoiReference | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = [pass_to_record(event, swath_reference) for event in events]
    (output_dir / "jpss_passes.json").write_text(json.dumps(records, indent=2) + "\n")

    with (output_dir / "jpss_passes.csv").open("w", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=list(records[0].keys()) if records else [])
        if records:
            writer.writeheader()
            writer.writerows(records)

    features = [
        {
            "type": "Feature",
            "properties": {
                "type": "station",
                "buffer_km": buffer_km,
                "min_elevation_deg": min_elevation_deg,
                "reference_swath_file": swath_reference.path.name if swath_reference else None,
                "reference_swath_source_count": swath_reference.source_count if swath_reference else 0,
                "reference_swath_station_inside_count": swath_reference.station_inside_count
                if swath_reference
                else 0,
                "aoi_file": aoi.path.name if aoi else None,
                "aoi_feature_count": aoi.feature_count if aoi else 0,
            },
            "geometry": {
                "type": "Point",
                "coordinates": [station_lon, station_lat],
            },
        },
        {
            "type": "Feature",
            "properties": {
                "type": "station_buffer",
                "buffer_km": buffer_km,
                "planning_note": "reference buffer from the original rough planning flow",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [geodesic_circle(station_lon, station_lat, buffer_km)],
            },
        }
    ]
    if swath_reference is not None:
        features.extend(swath_reference.features)

    if aoi is not None and mapping is not None:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "type": "aoi",
                    "source": aoi.path.name,
                    "feature_count": aoi.feature_count,
                },
                "geometry": mapping(aoi.geometry),
            }
        )

    for event in events:
        base = pass_to_record(event, swath_reference)
        features.append(
            {
                "type": "Feature",
                "properties": {**base, "type": "visible_track"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[point.lon, point.lat] for point in event.track],
                },
            }
        )
        features.append(
            {
                "type": "Feature",
                "properties": {**base, "type": "aos", "color": "cyan"},
                "geometry": {"type": "Point", "coordinates": [event.start.lon, event.start.lat]},
            }
        )
        features.append(
            {
                "type": "Feature",
                "properties": {**base, "type": "tca", "color": "yellow"},
                "geometry": {"type": "Point", "coordinates": [event.tca.lon, event.tca.lat]},
            }
        )
        features.append(
            {
                "type": "Feature",
                "properties": {**base, "type": "los", "color": "green"},
                "geometry": {"type": "Point", "coordinates": [event.end.lon, event.end.lat]},
            }
        )

    geojson = {"type": "FeatureCollection", "features": features}
    (output_dir / "jpss_passes.geojson").write_text(json.dumps(geojson, indent=2) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan JPSS passes over a station buffer.")
    parser.add_argument("--station", default="station.geojson", type=Path)
    parser.add_argument("--output-dir", default="outputs", type=Path)
    parser.add_argument("--cache-dir", default="tle_cache", type=Path)
    parser.add_argument("--buffer-km", default=DEFAULT_BUFFER_KM, type=float)
    parser.add_argument(
        "--min-elevation-deg",
        default=DEFAULT_MIN_ELEVATION_DEG,
        type=float,
        help="Minimum topocentric elevation used for AOS/LOS access planning.",
    )
    parser.add_argument("--step-seconds", default=DEFAULT_STEP_SECONDS, type=int)
    parser.add_argument(
        "--swath-file",
        type=Path,
        help="Optional sample JPSS swath GeoJSON to include as a reference coverage layer.",
    )
    parser.add_argument(
        "--aoi-file",
        type=Path,
        help="Optional AOI GeoJSON; mark passes whose ground track intersects the AOI.",
    )
    parser.add_argument("--start", help="UTC start time, for example 2026-07-07T00:00:00Z")
    parser.add_argument("--hours", default=24.0, type=float, help="Planning cycle length in hours.")
    return parser.parse_args()


def parse_start(value: str | None) -> datetime:
    if value:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(UTC)
    return datetime.now(UTC).replace(microsecond=0)


def main() -> None:
    args = parse_args()
    station_lon, station_lat = read_station(args.station)
    start_time = parse_start(args.start)
    end_time = start_time + timedelta(hours=args.hours)
    swath_reference = (
        load_swath_reference(args.swath_file, station_lon, station_lat, args.buffer_km)
        if args.swath_file
        else None
    )
    aoi = load_aoi_reference(args.aoi_file) if args.aoi_file else None

    events: list[PassEvent] = []
    for name, catnr in SATELLITES.items():
        tle_name, line1, line2 = fetch_tle(catnr, args.cache_dir)
        tle_epoch = parse_tle_epoch(line1)
        sat = Satrec.twoline2rv(line1, line2)
        satellite_events = find_passes(
            name=tle_name if tle_name else name,
            sat=sat,
            station_lon=station_lon,
            station_lat=station_lat,
            start_time=start_time,
            end_time=end_time,
            buffer_km=args.buffer_km,
            min_elevation_deg=args.min_elevation_deg,
            tle_epoch=tle_epoch,
            step_seconds=args.step_seconds,
            aoi=aoi,
        )
        events.extend(satellite_events)

    events.sort(key=lambda event: event.start.time)
    write_outputs(
        events,
        args.output_dir,
        station_lon,
        station_lat,
        args.buffer_km,
        args.min_elevation_deg,
        swath_reference,
        aoi,
    )

    print(
        f"Station: lon={station_lon}, lat={station_lat}, "
        f"buffer={args.buffer_km:g} km, min_elevation={args.min_elevation_deg:g} deg"
    )
    print(f"Cycle: {iso(start_time)} to {iso(end_time)}")
    print(f"Passes: {len(events)}")
    if swath_reference is not None:
        print(
            f"Reference swath: {swath_reference.path.name}, features={swath_reference.source_count}, "
            f"station_inside={swath_reference.station_inside_count}, "
            f"buffer_intersects={swath_reference.buffer_intersects_count}, "
            f"satellites={','.join(swath_reference.satellite_names) or 'unknown'}, "
            f"sensors={','.join(swath_reference.sensors) or 'unknown'}"
        )
    if aoi is not None:
        print(
            f"AOI: {aoi.path.name}, features={aoi.feature_count}, "
            f"covered_passes={sum(event.aoi_covered for event in events)}"
        )
    for event in events:
        print(
            f"{iso(event.start.time)} - {iso(event.end.time)} "
            f"{event.satellite} {event.direction} "
            f"duration={event.duration_minutes:.1f} min "
            f"max_el={event.max_elevation_point.elevation_deg:.1f} deg "
            f"min_distance={event.min_distance_km:.0f} km"
        )


if __name__ == "__main__":
    main()
