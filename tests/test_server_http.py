# -*- coding: utf-8 -*-
"""HTTP-слой сервера: /detect и /build должны отвечать, а не падать на аннотациях FastAPI."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STOCK = next(
    (ROOT / "MS45" / "firmware_spdaten" / "cal").glob("*4560BN00S*"), None)

try:  # fastapi и httpx нужны только для HTTP-слоя; ядро тестируется отдельно
    from fastapi.testclient import TestClient
    from server.app import create_app
except ImportError:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx не установлены")
class HttpLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_health(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_detect_and_build_answer_with_200(self) -> None:
        data = STOCK.read_bytes()
        detected = self.client.post(
            "/detect", files={"file": ("stock.bin", data)}, data={"lang": "ru"})
        self.assertEqual(detected.status_code, 200, detected.text)
        self.assertEqual(detected.json()["platform"], "MS45")

        request = {"stage": "stage1", "intents": [
            {"kind": "rev_limit", "value": 6700, "transmission": "mt",
             "acknowledge_risk": True}]}
        built = self.client.post(
            "/build", files={"file": ("stock.bin", data)},
            data={"request": json.dumps(request), "account": "http-test",
                  "vin": "WBAEV11000KL12345"})
        self.assertEqual(built.status_code, 200, built.text)
        self.assertEqual(built.json()["status"], "BUILT")

        # по умолчанию (заглушка) VIN не нужен: сборка без VIN проходит
        self.assertFalse(self.client.get("/health").json()["vin_required"])
        no_vin = self.client.post(
            "/build", files={"file": ("stock.bin", data)},
            data={"request": json.dumps(request), "account": "http-test-2"})
        self.assertEqual(no_vin.status_code, 200, no_vin.text)

    def test_vin_required_mode_over_http(self) -> None:
        import os
        data = STOCK.read_bytes()
        request = {"stage": "stage1", "intents": [
            {"kind": "rev_limit", "value": 6700, "transmission": "mt",
             "acknowledge_risk": True}]}
        old = os.environ.get("TORQUEX_VIN_REQUIRED")
        os.environ["TORQUEX_VIN_REQUIRED"] = "on"
        try:
            client = TestClient(create_app())
            self.assertTrue(client.get("/health").json()["vin_required"])
            no_vin = client.post(
                "/build", files={"file": ("stock.bin", data)},
                data={"request": json.dumps(request), "account": "http-v1"})
            self.assertEqual(no_vin.status_code, 422, no_vin.text)
            self.assertEqual(no_vin.json()["code"], "VIN_REQUIRED")
            bad = client.post(
                "/build", files={"file": ("stock.bin", data)},
                data={"request": json.dumps(request), "account": "http-v2", "vin": "123"})
            self.assertEqual(bad.json()["code"], "VIN_INVALID")
            ok = client.post(
                "/build", files={"file": ("stock.bin", data)},
                data={"request": json.dumps(request), "account": "http-v3",
                      "vin": "WBAEV11000KL12345"})
            self.assertEqual(ok.status_code, 200, ok.text)
        finally:
            if old is None:
                os.environ.pop("TORQUEX_VIN_REQUIRED", None)
            else:
                os.environ["TORQUEX_VIN_REQUIRED"] = old

    def test_path_in_request_is_refused_over_http(self) -> None:
        request = {"stage": "stage1", "intents": [
            {"kind": "ignition", "delta_deg": 1.0, "evidence": "/etc/passwd"}]}
        response = self.client.post(
            "/build", files={"file": ("stock.bin", STOCK.read_bytes())},
            data={"request": json.dumps(request), "account": "http-test-2"})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "REQUEST_INVALID")


if __name__ == "__main__":
    unittest.main()
