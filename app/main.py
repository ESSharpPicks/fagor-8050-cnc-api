from __future__ import annotations

import math
import os
import re
from typing import Literal

import pyclipper
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator


API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(value: str | None = Security(API_KEY_HEADER)) -> None:
    expected = os.getenv("CNC_API_KEY", "").strip()
    if expected and value != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


app = FastAPI(
    title="Fagor 8050 M CNC Advisory API",
    version="1.0.0",
    servers=[{"url": "https://fagor-8050-cnc-api.onrender.com"}],
    description=(
        "Static G-code checks and transparent machining calculations. "
        "Advisory only: always simulate, dry-run, and obtain operator approval."
    ),
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point2D(StrictModel):
    x: float
    y: float


class Point3D(StrictModel):
    x: float | None = None
    y: float | None = None
    z: float | None = None


class MachineLimits(StrictModel):
    maxSpindleRpm: float = Field(gt=0)
    maxFeedIpm: float = Field(gt=0)
    maxPlungeIpm: float | None = Field(default=None, gt=0)
    travelMin: Point3D | None = None
    travelMax: Point3D | None = None


class ValidationRequest(StrictModel):
    controller: str
    units: Literal["inch", "millimeter"]
    program: str = Field(min_length=1, max_length=200_000)
    machineLimits: MachineLimits
    clearanceZ: float | None = None
    stockMin: Point3D | None = None
    stockMax: Point3D | None = None
    requiredWorkOffset: str | None = None
    allowedCodes: list[str] = Field(default_factory=list)
    forbiddenCodes: list[str] = Field(default_factory=list)


class Finding(StrictModel):
    severity: Literal["info", "warning", "error"]
    code: str
    line: int | None = None
    message: str
    recommendation: str | None = None


class ValidationResponse(StrictModel):
    status: Literal["no_static_errors_found", "warnings_found", "errors_found"]
    findings: list[Finding]
    disclaimer: str


class CuttingRequest(StrictModel):
    operation: str
    material: str
    toolMaterial: str | None = None
    toolDiameterIn: float = Field(gt=0)
    fluteCount: int = Field(ge=1)
    targetSurfaceSpeedSfm: float = Field(gt=0)
    targetChipLoadIn: float = Field(gt=0)
    radialEngagementPercent: float | None = Field(default=None, gt=0, le=100)
    axialDepthIn: float | None = Field(default=None, gt=0)
    machineLimits: MachineLimits


class CuttingResponse(StrictModel):
    spindleRpm: float
    feedIpm: float
    formulas: list[str]
    cappedByMachineLimit: bool
    warnings: list[str]


class OffsetRequest(StrictModel):
    units: Literal["inch", "millimeter"]
    closedPolyline: list[Point2D] = Field(min_length=3)
    toolDiameter: float = Field(gt=0)
    side: Literal["inside", "outside"]
    cornerJoin: Literal["round", "miter", "bevel"] = "round"

    @model_validator(mode="after")
    def at_least_three_distinct_points(self) -> "OffsetRequest":
        if len({(p.x, p.y) for p in self.closedPolyline}) < 3:
            raise ValueError("closedPolyline must contain at least three distinct points")
        return self


class OffsetResponse(StrictModel):
    toolCenterPolyline: list[Point2D]
    warnings: list[str]


WORD_RE = re.compile(r"(?<![A-Z])([A-Z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.I)
CODE_RE = re.compile(r"(?<![A-Z])(G|M)\s*0*(\d+(?:\.\d+)?)", re.I)
PLACEHOLDER_RE = re.compile(r"<[^>]+>|\b(?:TBD|TODO|UNKNOWN|CONFIRM(?:ED)?)\b", re.I)
COMMENT_PAREN_RE = re.compile(r"\([^)]*\)")


def normalized_codes(line: str) -> set[str]:
    return {f"{letter.upper()}{float(number):g}" for letter, number in CODE_RE.findall(line)}


def axis_words(line: str) -> dict[str, float]:
    return {letter.upper(): float(value) for letter, value in WORD_RE.findall(line) if letter.upper() in "XYZFS"}


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok", "service": "fagor-8050-m-advisory"}


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy_policy() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Privacy Policy — Fagor 8050 M CNC Advisory API</title></head>
<body><main><h1>Privacy Policy</h1><p>Effective: August 19, 2026</p>
<p>This API receives CNC program text and machining parameters solely to return static advisory checks and calculations.</p>
<p>The service does not include a database and does not intentionally retain request bodies. The hosting provider may temporarily process technical request metadata for security, diagnostics, and service operation.</p>
<p>Do not submit personal information, trade secrets, export-controlled data, or production files you are not authorized to process.</p>
<p>This service does not control machinery and does not guarantee that generated or reviewed CNC programs are safe or correct.</p>
<p>Service owner: ESSharpPicks. Contact through the public GitHub repository for this service.</p>
</main></body></html>"""


@app.post(
    "/v1/programs/validate", response_model=ValidationResponse,
    operation_id="validateProgram", dependencies=[Depends(require_api_key)],
)
def validate_program(req: ValidationRequest) -> ValidationResponse:
    findings: list[Finding] = []
    if "8050" not in req.controller or "fagor" not in req.controller.lower():
        findings.append(Finding(
            severity="warning", code="CONTROLLER_PROFILE",
            message="This validator is tuned for the Fagor 8050 M, but the supplied controller name differs.",
            recommendation="Supply the exact controller and software revision.",
        ))

    absolute = None
    motion = None
    current = {"X": None, "Y": None, "Z": None}
    has_end = False
    has_spindle_stop = False
    active_comp = "G40"
    permitted = {c.upper().replace(" ", "") for c in req.allowedCodes}
    forbidden = {c.upper().replace(" ", "") for c in req.forbiddenCodes}

    for number, raw in enumerate(req.program.splitlines(), 1):
        line = raw.strip().upper()
        if not line:
            continue
        codes = normalized_codes(line)
        words = axis_words(line)

        if PLACEHOLDER_RE.search(line):
            findings.append(Finding(
                severity="error", code="UNRESOLVED_PLACEHOLDER", line=number,
                message="The block contains an unresolved placeholder.",
                recommendation="Replace it with a verified value before prove-out.",
            ))
        if COMMENT_PAREN_RE.search(line) and ";(" not in line:
            findings.append(Finding(
                severity="warning", code="COMMENT_STYLE", line=number,
                message="Parenthesized comment is not prefixed with the requested semicolon style.",
            ))
        if "G20" in codes and req.units != "inch" or "G21" in codes and req.units != "millimeter":
            findings.append(Finding(
                severity="error", code="UNIT_CONFLICT", line=number,
                message="Program unit code conflicts with the request units.",
            ))
        if "G90" in codes:
            absolute = True
        if "G91" in codes:
            absolute = False
        if "G0" in codes:
            motion = "G0"
        if "G1" in codes or "G2" in codes or "G3" in codes:
            motion = next(c for c in ("G1", "G2", "G3") if c in codes)
        if "G41" in codes or "G42" in codes:
            active_comp = "G41" if "G41" in codes else "G42"
        if "G40" in codes:
            active_comp = "G40"
        if "M5" in codes:
            has_spindle_stop = True
        if "M30" in codes:
            has_end = True

        for code in codes:
            if code in forbidden:
                findings.append(Finding(
                    severity="error", code="FORBIDDEN_CODE", line=number,
                    message=f"{code} is forbidden by the supplied machine profile.",
                ))
            if permitted and code not in permitted:
                findings.append(Finding(
                    severity="warning", code="UNLISTED_CODE", line=number,
                    message=f"{code} is not in the supplied allowed-code list.",
                ))

        if "G53" in codes and any(axis in words for axis in "XYZ"):
            findings.append(Finding(
                severity="warning", code="FAGOR_G53_SEMANTICS", line=number,
                message="Do not assume Fanuc-style nonmodal machine-coordinate behavior for G53 on a Fagor 8050 M.",
                recommendation="Verify G53 against the exact 8050 M software manual and a proven machine program.",
            ))
        if "G92" in codes:
            findings.append(Finding(
                severity="warning", code="G92_PRESET", line=number,
                message="G92 changes the coordinate reference/preset and may persist beyond the expected block.",
                recommendation="Verify its persistence and cancellation on this machine.",
            ))
        if "G4" in codes and "K" in line:
            findings.append(Finding(
                severity="info", code="DWELL_CONFIRM", line=number,
                message="Confirm that K dwell units match the installed Fagor software revision.",
            ))

        if any(axis in words for axis in "XYZ"):
            if absolute is None:
                findings.append(Finding(
                    severity="warning", code="DISTANCE_MODE_UNKNOWN", line=number,
                    message="Axis motion occurs before G90/G91 is established.",
                ))
            for axis in "XYZ":
                if axis in words:
                    if absolute is False and current[axis] is not None:
                        current[axis] = current[axis] + words[axis]
                    else:
                        current[axis] = words[axis]

            if motion == "G0" and req.clearanceZ is not None:
                xy_move = "X" in words or "Y" in words
                z_value = current["Z"]
                if xy_move and (z_value is None or z_value < req.clearanceZ):
                    findings.append(Finding(
                        severity="error", code="RAPID_BELOW_CLEARANCE", line=number,
                        message="Rapid XY motion is not proven above the configured clearance Z.",
                        recommendation="Retract to verified clearance before rapid XY movement.",
                    ))

            for axis, value in current.items():
                minimum = getattr(req.machineLimits.travelMin, axis.lower(), None) if req.machineLimits.travelMin else None
                maximum = getattr(req.machineLimits.travelMax, axis.lower(), None) if req.machineLimits.travelMax else None
                if value is not None and ((minimum is not None and value < minimum) or (maximum is not None and value > maximum)):
                    findings.append(Finding(
                        severity="error", code="TRAVEL_LIMIT", line=number,
                        message=f"Calculated {axis} position {value:g} is outside the supplied travel range.",
                    ))

        if "S" in words and words["S"] > req.machineLimits.maxSpindleRpm:
            findings.append(Finding(
                severity="error", code="SPINDLE_LIMIT", line=number,
                message=f"Commanded RPM {words['S']:g} exceeds the configured maximum.",
            ))
        if "F" in words and words["F"] > req.machineLimits.maxFeedIpm:
            findings.append(Finding(
                severity="error", code="FEED_LIMIT", line=number,
                message=f"Commanded feed {words['F']:g} exceeds the configured maximum.",
            ))

    if active_comp != "G40":
        findings.append(Finding(
            severity="warning", code="COMP_NOT_CANCELLED",
            message=f"Cutter compensation remains active ({active_comp}) at program end.",
        ))
    if not has_spindle_stop:
        findings.append(Finding(severity="warning", code="NO_M5", message="No spindle-stop M5 was found."))
    if not has_end:
        findings.append(Finding(severity="error", code="NO_M30", message="No M30 program end was found."))
    if req.requiredWorkOffset and req.requiredWorkOffset.upper() not in req.program.upper():
        findings.append(Finding(
            severity="error", code="WORK_OFFSET_MISSING",
            message=f"Required work offset {req.requiredWorkOffset} was not found.",
        ))

    severities = {item.severity for item in findings}
    status = "errors_found" if "error" in severities else "warnings_found" if "warning" in severities else "no_static_errors_found"
    return ValidationResponse(
        status=status,
        findings=findings,
        disclaimer="Static advisory checks cannot prove machine safety, geometry correctness, or collision freedom.",
    )


@app.post(
    "/v1/cutting-parameters/calculate",
    response_model=CuttingResponse,
    operation_id="calculateCuttingParameters",
    dependencies=[Depends(require_api_key)],
)
def calculate_cutting_parameters(req: CuttingRequest) -> CuttingResponse:
    raw_rpm = (req.targetSurfaceSpeedSfm * 12.0) / (math.pi * req.toolDiameterIn)
    rpm = min(raw_rpm, req.machineLimits.maxSpindleRpm)
    raw_feed = rpm * req.fluteCount * req.targetChipLoadIn
    feed = min(raw_feed, req.machineLimits.maxFeedIpm)
    capped = rpm < raw_rpm or feed < raw_feed
    warnings = [
        "Values are starting points derived from supplied inputs, not manufacturer recommendations.",
        "Verify tool, holder, spindle, engagement, workholding, coolant, rigidity, and material condition.",
    ]
    if capped:
        warnings.append("One or more results were capped by the supplied machine limits.")
    return CuttingResponse(
        spindleRpm=round(rpm, 1),
        feedIpm=round(feed, 3),
        formulas=[
            "RPM = (surface speed SFM × 12) / (π × tool diameter inches)",
            "Feed IPM = RPM × flute count × chip load inches/tooth",
        ],
        cappedByMachineLimit=capped,
        warnings=warnings,
    )


@app.post(
    "/v1/toolpaths/offset", response_model=OffsetResponse,
    operation_id="calculateOffsetToolpath", dependencies=[Depends(require_api_key)],
)
def calculate_offset_toolpath(req: OffsetRequest) -> OffsetResponse:
    scale = 1_000_000.0
    points = [(int(round(p.x * scale)), int(round(p.y * scale))) for p in req.closedPolyline]
    if points[0] == points[-1]:
        points.pop()
    area = pyclipper.Area(points)
    if area == 0:
        raise HTTPException(status_code=400, detail="Polyline has zero area")

    join_map = {
        "round": pyclipper.JT_ROUND,
        "miter": pyclipper.JT_MITER,
        "bevel": pyclipper.JT_SQUARE,
    }
    offsetter = pyclipper.PyclipperOffset(miter_limit=2.0, arc_tolerance=0.0001 * scale)
    offsetter.AddPath(points, join_map[req.cornerJoin], pyclipper.ET_CLOSEDPOLYGON)
    outward_sign = 1.0 if area > 0 else -1.0
    distance = req.toolDiameter / 2.0
    delta = distance * outward_sign if req.side == "outside" else -distance * outward_sign
    results = offsetter.Execute(delta * scale)
    if not results:
        raise HTTPException(status_code=400, detail="Offset eliminated or invalidated the geometry")
    if len(results) > 1:
        raise HTTPException(status_code=400, detail="Offset produced multiple contours; manual geometry review is required")
    output = [Point2D(x=x / scale, y=y / scale) for x, y in results[0]]
    output.append(output[0])
    return OffsetResponse(
        toolCenterPolyline=output,
        warnings=[
            "Geometry only: no lead-in, lead-out, Z motion, tabs, workholding, or collision checks are included.",
            "Confirm contour direction and compare the returned extents with the drawing before programming.",
        ],
    )
