import asyncio
import json
import unittest
from urllib.parse import parse_qs

import httpx

from services import ors_client


def make_mock_transport(locations, request_log):
    """Simulates the real Mapbox Matrix API: for a batch request with given source/destination
    indices (relative to the coordinates in the URL), returns a block of
    distance = 1000 * |i - j| meters, duration = 60 * |i - j| seconds, so the test can verify
    the reconstructed full matrix against a known formula regardless of batch boundaries."""

    def handler(request: httpx.Request) -> httpx.Response:
        request_log.append(str(request.url))
        # NOTE: stdlib urlparse treats an unescaped ";" in the *path* as a legacy RFC2396
        # "path parameter" delimiter and truncates everything after it - which corrupts our
        # semicolon-joined coordinate list. Split on "?" manually instead of using urlparse
        # on the whole URL.
        raw_path, _, raw_query = str(request.url).partition("?")
        query = parse_qs(raw_query)
        coords_part = raw_path.rsplit("/", 1)[-1]
        coords = coords_part.split(";")
        sources = [int(i) for i in query["sources"][0].split(";")]
        destinations = [int(i) for i in query["destinations"][0].split(";")]

        # Map each local coordinate back to its global index in `locations`.
        def global_index(local_coord_index):
            lng, lat = (float(v) for v in coords[local_coord_index].split(","))
            return locations.index([lng, lat])

        distances = []
        durations = []
        for s in sources:
            drow, urow = [], []
            for d in destinations:
                gi = global_index(s)
                gj = global_index(d)
                drow.append(1000.0 * abs(gi - gj))
                urow.append(60.0 * abs(gi - gj))
            distances.append(drow)
            durations.append(urow)
        return httpx.Response(200, json={"distances": distances, "durations": durations})

    return httpx.MockTransport(handler)


class MapboxBatchingTests(unittest.TestCase):
    def _run(self, n_locations, batch_size):
        locations = [[121.0 + i * 0.01, 14.5 + i * 0.01] for i in range(n_locations)]
        request_log = []
        transport = make_mock_transport(locations, request_log)

        real_async_client = httpx.AsyncClient

        class PatchedAsyncClient(real_async_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        import os
        os.environ["MAPBOX_MATRIX_BATCH_SIZE"] = str(batch_size)
        ors_client.httpx.AsyncClient = PatchedAsyncClient
        try:
            result = asyncio.run(ors_client._mapbox_matrix(locations, "fake-token-for-test"))
        finally:
            ors_client.httpx.AsyncClient = real_async_client
            del os.environ["MAPBOX_MATRIX_BATCH_SIZE"]
        return result, request_log, n_locations, batch_size

    def test_small_fleet_needs_a_single_batch(self):
        result, request_log, n, batch_size = self._run(n_locations=4, batch_size=5)
        self.assertEqual(len(request_log), 1)
        self._assert_matrix_correct(result, n)

    def test_larger_fleet_requires_multiple_batches_and_reconstructs_correctly(self):
        # 12 locations at batch_size=5 -> ceil(12/5)=3 blocks per axis -> 3*3=9 sub-requests.
        result, request_log, n, batch_size = self._run(n_locations=12, batch_size=5)
        self.assertEqual(len(request_log), 9)
        self._assert_matrix_correct(result, n)

    def test_fifty_locations_batches_predictably(self):
        # 50 locations at batch_size=5 -> ceil(50/5)=10 blocks per axis -> 100 sub-requests.
        result, request_log, n, batch_size = self._run(n_locations=50, batch_size=5)
        self.assertEqual(len(request_log), 100)
        self._assert_matrix_correct(result, n)

    def _assert_matrix_correct(self, result, n):
        dm = result["distance_matrix_km"]
        du = result["duration_matrix_min"]
        self.assertEqual(len(dm), n)
        self.assertTrue(all(len(row) == n for row in dm))
        self.assertEqual(len(du), n)
        self.assertTrue(all(len(row) == n for row in du))
        for i in range(n):
            for j in range(n):
                self.assertAlmostEqual(dm[i][j], abs(i - j) * 1.0, places=6)  # 1000m -> 1km
                self.assertAlmostEqual(du[i][j], abs(i - j) * 1.0, places=6)  # 60s -> 1min
        self.assertTrue(all(dm[i][i] == 0 for i in range(n)))
        self.assertEqual(result["provider"], "mapbox")


if __name__ == "__main__":
    unittest.main()
