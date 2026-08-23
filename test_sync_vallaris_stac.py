import unittest

from scripts.sync_vallaris_stac import SyncError, clean_and_validate_features, safe_url


def feature(feature_type: str, geometry_type: str, coordinates: object) -> dict:
    return {
        "type": "Feature",
        "id": f"remote-{feature_type}",
        "geometry": {"type": geometry_type, "coordinates": coordinates},
        "properties": {
            "_id": f"private-{feature_type}",
            "type": feature_type,
            "pass_id": "suomi_npp_2026-08-23_ascending_20260823T053830Z",
            "planning_date": "2026-08-23",
            "satellite": "SUOMI NPP",
            "tca_utc": "2026-08-23T05:38:30Z",
        },
    }


class VallarisStacSyncTest(unittest.TestCase):
    def test_api_key_is_redacted_from_diagnostics(self) -> None:
        value = safe_url("https://beta.vallarismaps.com/search?api_key=secret&limit=10")
        self.assertNotIn("secret", value)
        self.assertIn("api_key=%3Credacted%3E", value)

    def test_complete_pass_is_validated_and_internal_properties_are_removed(self) -> None:
        features = [
            feature("visible_track", "LineString", [[100.0, 10.0], [101.0, 11.0]]),
            feature("aos", "Point", [100.0, 10.0]),
            feature("tca", "Point", [100.5, 10.5]),
            feature("los", "Point", [101.0, 11.0]),
        ]
        cleaned = clean_and_validate_features(features, "2026-08-23")
        self.assertEqual(len(cleaned), 4)
        self.assertTrue(all("_id" not in item["properties"] for item in cleaned))
        self.assertEqual({item["properties"]["type"] for item in cleaned}, {"visible_track", "aos", "tca", "los"})

    def test_incomplete_pass_is_rejected(self) -> None:
        with self.assertRaisesRegex(SyncError, "Incomplete pass"):
            clean_and_validate_features(
                [feature("visible_track", "LineString", [[100.0, 10.0], [101.0, 11.0]])],
                "2026-08-23",
            )

    def test_coordinates_outside_crs84_are_rejected(self) -> None:
        features = [
            feature("visible_track", "LineString", [[181.0, 10.0], [101.0, 11.0]]),
            feature("aos", "Point", [100.0, 10.0]),
            feature("tca", "Point", [100.5, 10.5]),
            feature("los", "Point", [101.0, 11.0]),
        ]
        with self.assertRaisesRegex(SyncError, "outside CRS84"):
            clean_and_validate_features(features, "2026-08-23")


if __name__ == "__main__":
    unittest.main()
