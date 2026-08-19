from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


LIMITS = {"maxSpindleRpm": 12000, "maxFeedIpm": 300}


def test_health() -> None:
    result = client.get("/health")
    assert result.status_code == 200
    assert result.json()["status"] == "ok"


def test_cutting_parameters() -> None:
    result = client.post("/v1/cutting-parameters/calculate", json={
        "operation": "slotting",
        "material": "6061-T6 aluminum",
        "toolMaterial": "carbide",
        "toolDiameterIn": 0.25,
        "fluteCount": 2,
        "targetSurfaceSpeedSfm": 300,
        "targetChipLoadIn": 0.002,
        "machineLimits": LIMITS,
    })
    assert result.status_code == 200
    body = result.json()
    assert body["spindleRpm"] <= 12000
    assert body["feedIpm"] <= 300


def test_validator_flags_unsafe_template() -> None:
    result = client.post("/v1/programs/validate", json={
        "controller": "Fagor 8050 M",
        "units": "inch",
        "program": "G90 G0 X0 Y0\nS15000 M3\nG92 X0 Y0 Z0\nM30",
        "machineLimits": LIMITS,
        "clearanceZ": 0.5,
    })
    assert result.status_code == 200
    codes = {f["code"] for f in result.json()["findings"]}
    assert {"RAPID_BELOW_CLEARANCE", "SPINDLE_LIMIT", "G92_PRESET", "NO_M5"} <= codes


def test_outside_square_offset() -> None:
    result = client.post("/v1/toolpaths/offset", json={
        "units": "inch",
        "closedPolyline": [
            {"x": 0, "y": 0}, {"x": 2, "y": 0},
            {"x": 2, "y": 1}, {"x": 0, "y": 1},
        ],
        "toolDiameter": 0.25,
        "side": "outside",
        "cornerJoin": "miter",
    })
    assert result.status_code == 200
    points = result.json()["toolCenterPolyline"]
    assert min(p["x"] for p in points) == -0.125
    assert max(p["x"] for p in points) == 2.125


def test_api_key_protects_actions_but_not_health(monkeypatch) -> None:
    monkeypatch.setenv("CNC_API_KEY", "test-secret")
    assert client.get("/health").status_code == 200
    denied = client.post("/v1/cutting-parameters/calculate", json={})
    assert denied.status_code == 401
    monkeypatch.delenv("CNC_API_KEY")
