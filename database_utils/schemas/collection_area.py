# schemas/collection_area.py
"""Collection areas (ca1): a polygon per collector, drawn in the back-office."""
import re
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MAX_POLYGON_POINTS",
    "MIN_POLYGON_POINTS",
    "CollectionAreaCreate",
    "CollectionAreaOut",
    "CollectionAreaUpdate",
    "CollectorHomeUpdate",
    "normalize_polygon",
]

COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
MIN_POLYGON_POINTS = 3
MAX_POLYGON_POINTS = 1000


def normalize_polygon(points: List[List[float]]) -> List[List[float]]:
    """One ring of [latitude, longitude] pairs, at least 3 distinct corners.
    A closing point equal to the first is dropped, so every stored ring is open."""
    ring = []
    for point in points:
        if len(point) != 2:
            raise ValueError("each point must be [latitude, longitude]")
        lat, lng = float(point[0]), float(point[1])
        if not -90 <= lat <= 90 or not -180 <= lng <= 180:
            raise ValueError("latitude must be within -90..90 and longitude within -180..180")
        ring.append([lat, lng])
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    if len({tuple(p) for p in ring}) < MIN_POLYGON_POINTS:
        raise ValueError(f"a polygon needs at least {MIN_POLYGON_POINTS} distinct points")
    if len(ring) > MAX_POLYGON_POINTS:
        raise ValueError(f"a polygon may have at most {MAX_POLYGON_POINTS} points")
    return ring


def _check_color(value: Optional[str]) -> Optional[str]:
    if value is not None and not COLOR_PATTERN.match(value):
        raise ValueError("color must be #RRGGBB")
    return value.upper() if value else value


class CollectionAreaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str
    polygon: List[List[float]]
    collector_id: Optional[UUID] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        return _check_color(value)

    @field_validator("polygon")
    @classmethod
    def _polygon(cls, value: List[List[float]]) -> List[List[float]]:
        return normalize_polygon(value)


class CollectionAreaUpdate(BaseModel):
    """Partial update. Send collector_id: null to leave the area without a collector."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    color: Optional[str] = None
    polygon: Optional[List[List[float]]] = None
    collector_id: Optional[UUID] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: Optional[str]) -> Optional[str]:
        return _check_color(value)

    @field_validator("polygon")
    @classmethod
    def _polygon(cls, value):
        return None if value is None else normalize_polygon(value)


class CollectionAreaOut(BaseModel):
    id: UUID
    company_id: UUID
    name: str
    color: str
    polygon: List[List[float]]
    collector_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CollectorHomeUpdate(BaseModel):
    """Where a collector lives. Both coordinates, or both null to clear it."""
    home_latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    home_longitude: Optional[float] = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _both_or_neither(self):
        if (self.home_latitude is None) != (self.home_longitude is None):
            raise ValueError("send both home_latitude and home_longitude, or neither")
        return self
