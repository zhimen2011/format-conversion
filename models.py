"""STAS Airport Data Models — AIRPORT2, RWYU, Obstacle, Intersection (#INT).

Based on STAS User Manual REV C, section 4.5.1.
All distance/height units depend on the airport-level unit settings (F or M).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

HeightUnits = Literal["F", "M"]
DistanceUnits = Literal["F", "M"]
HeightRef = Literal["BR", "LO", "MS"]   # BR=brake-release, LO=liftoff, MS=mean-sea-level
DistanceRef = Literal["BR", "LO"]       # BR=brake-release, LO=liftoff


class RunwayFlag(IntEnum):
    """Semantics of the four declared-distance fields."""
    TORA_TODA_ASDA = 0   # fields are TORA, TODA, ASDA
    TORA_CWY_SWY = 1     # fields are TORA, Clearway, Stopway


class SurfaceType(IntEnum):
    NORMAL = 0
    GRAVEL = 1
    GROOVED_POROUS = 2   # aka Skid-Resistant per AC 150/5320-12C


# ---------------------------------------------------------------------------
# Obstacle
# ---------------------------------------------------------------------------

class Obstacle(BaseModel):
    """A single obstacle along a runway departure path."""

    height: float = Field(..., description="Obstacle height (units per airport setting)")
    distance: float = Field(..., description="Distance from runway start / threshold")
    lateral_offset: float = Field(0.0, description="Lateral offset from centreline")


# ---------------------------------------------------------------------------
# Intersection  (#INT)
# ---------------------------------------------------------------------------

class Intersection(BaseModel):
    """A runway intersection entry.

    Format: #INT <name> <offset> <lineup_angle> <tod_slope> <asd_slope>
    Offset is measured from the runway start (brake-release end).
    """

    name: str = Field(..., description="Intersection name, e.g. 'A2-C2', 'E2', 'H11'")
    offset: float = Field(..., description="Distance from runway start to intersection")
    lineup_angle: float = Field(..., description="Lineup / taxiway entry angle in degrees")
    tod_slope: float = Field(0.0, description="TOD / Flight-Path slope (%)")
    asd_slope: float = Field(0.0, description="ASD slope (%)")


# ---------------------------------------------------------------------------
# Runway
# ---------------------------------------------------------------------------

class Runway(BaseModel):
    """A single runway record (RWYU format — straight-out departures).

    RWYU header line carries optional taxiway-entry angle, missed-approach
    gradient, runway width, and magnetic heading.  The runway-data line carries
    declared distances, slopes, obstacle count, and surface type.  Obstacle
    lines follow immediately.  Intersection (#INT) records may appear after
    the engine-out procedure line.
    """

    # --- RWYU header line --------------------------------------------------
    taxiway_entry_angle: float | None = Field(None, ge=0, le=360)
    missed_approach_gradient: float | None = Field(None, description="Percent")
    runway_width: float | None = Field(None, description="Same unit as distances")
    magnetic_heading: float | None = Field(None)

    # --- Runway data line --------------------------------------------------
    designator: str = Field(..., description="Runway designator, e.g. '15', '16L', '34R'")
    runway_flag: RunwayFlag = Field(..., description="0=TORA/TODA/ASDA, 1=TORA/Clearway/Stopway")
    tora: float = Field(..., description="Take-Off Run Available")
    toda_or_clearway: float = Field(..., description="TODA (flag=0) or Clearway (flag=1)")
    asda_or_stopway: float = Field(..., description="ASDA (flag=0) or Stopway (flag=1)")
    lda: float = Field(..., description="Landing Distance Available")
    tod_slope: float = Field(0.0, description="TOD / Flight-Path slope (%)")
    asd_slope: float = Field(0.0, description="ASD slope (%)")
    landing_slope: float = Field(0.0, description="Landing slope (%) — read, not used by STAS")
    surface_type: SurfaceType = SurfaceType.NORMAL

    # --- Obstacles ---------------------------------------------------------
    obstacles: list[Obstacle] = Field(default_factory=list)

    # --- Engine-out procedure ----------------------------------------------
    engine_out_procedure: str = Field("", description="Up to 130 characters")

    # --- Associated intersections ------------------------------------------
    intersections: list[Intersection] = Field(default_factory=list)

    # --- Comments (lines starting with 'H') --------------------------------
    comments: list[str] = Field(default_factory=list)

    # --- Emergency-turn note -----------------------------------------------
    emergency_turn_note: str | None = Field(None, description="e.g. '*** NO EMERGENCY TURN ***'")


# ---------------------------------------------------------------------------
# Airport
# ---------------------------------------------------------------------------

class Airport(BaseModel):
    """An airport record (AIRPORT2 format — column-position based).

    A single airport line followed by one or more runways.  The obstacle
    reference string (6 chars: height-unit, distance-unit, height-ref ×2,
    distance-ref ×2) is decomposed into its constituent fields for clarity.
    """

    # --- Identity -----------------------------------------------------------
    icao_code: str = Field(..., min_length=4, max_length=4, description="ICAO airport code")
    name: str = Field(..., max_length=18, description="Airport name")
    city: str = Field(..., max_length=18, description="City name")

    # --- Units & reference system ------------------------------------------
    height_units: HeightUnits = Field(..., description="F or M")
    distance_units: DistanceUnits = Field(..., description="F or M")
    height_ref: HeightRef = Field(..., description="BR / LO / MS")
    distance_ref: DistanceRef = Field(..., description="BR / LO")

    # --- Elevation ---------------------------------------------------------
    elevation: float = Field(..., description="Airport pressure-altitude reference")

    # --- Optional IATA code -------------------------------------------------
    iata_code: str | None = Field(None, max_length=3, description="IATA code, e.g. 'SZX'")

    # --- Comments (H lines between airport and first runway) ----------------
    comments: list[str] = Field(default_factory=list)

    # --- Runways ------------------------------------------------------------
    runways: list[Runway] = Field(default_factory=list)
