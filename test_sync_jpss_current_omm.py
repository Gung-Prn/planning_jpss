import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).parent / "scripts" / "sync_jpss_current_omm.py"
SPEC = importlib.util.spec_from_file_location("sync_jpss_current_omm", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def fixture(norad_id):
    names = {
        37849: ("SUOMI NPP", "2011-061A"),
        43013: ("NOAA 20 (JPSS-1)", "2017-073A"),
        54234: ("NOAA 21 (JPSS-2)", "2022-150A"),
    }
    name, object_id = names[norad_id]
    return [{
        "OBJECT_NAME": name,
        "OBJECT_ID": object_id,
        "EPOCH": "2026-08-23T06:00:00.000000",
        "MEAN_MOTION": 14.1953,
        "ECCENTRICITY": 0.0002,
        "INCLINATION": 98.78,
        "RA_OF_ASC_NODE": 174.1,
        "ARG_OF_PERICENTER": 77.7,
        "MEAN_ANOMALY": 282.4,
        "EPHEMERIS_TYPE": 0,
        "CLASSIFICATION_TYPE": "U",
        "NORAD_CAT_ID": norad_id,
        "ELEMENT_SET_NO": 999,
        "REV_AT_EPOCH": 100,
        "BSTAR": 0.000034,
        "MEAN_MOTION_DOT": 0.00000028,
        "MEAN_MOTION_DDOT": 0,
    }]


class CurrentOmmSnapshotTests(unittest.TestCase):
    def test_builds_three_satellite_snapshot_with_explicit_contract(self):
        def fetcher(url):
            norad_id = int(url.split("CATNR=")[1].split("&")[0])
            return fixture(norad_id)

        snapshot = MODULE.build_snapshot(
            fetcher=fetcher,
            generated_at=datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
        )

        self.assertEqual([37849, 43013, 54234], [row["norad_id"] for row in snapshot["satellites"]])
        self.assertEqual("OGC:CRS84", snapshot["position_contract"]["crs"])
        self.assertEqual(["longitude", "latitude"], snapshot["position_contract"]["coordinate_order"])
        self.assertFalse(snapshot["position_contract"]["telemetry"])

    def test_rejects_mismatched_norad_id(self):
        expected = MODULE.SATELLITES[0]
        with self.assertRaisesRegex(MODULE.SnapshotError, "NORAD mismatch"):
            MODULE.validate_omm(
                fixture(43013),
                expected,
                datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc),
            )

    def test_rejects_stale_omm(self):
        expected = MODULE.SATELLITES[0]
        with self.assertRaisesRegex(MODULE.SnapshotError, "older than 7 days"):
            MODULE.validate_omm(
                fixture(37849),
                expected,
                datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
