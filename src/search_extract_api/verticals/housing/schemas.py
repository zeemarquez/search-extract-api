from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ...schemas.common import BaseQuery, Result

Operation = Literal["sale", "rent"]
PropertyType = Literal["homes", "rooms", "offices", "premises", "garages", "land"]


class HousingResult(Result):
    """Real-estate listing."""

    operation: Operation | None = Field(default=None, description="sale | rent")
    property_type: str | None = None
    price: float | None = Field(default=None, description="Asking price (monthly for rent).")
    currency: str | None = Field(default=None, description="ISO 4217, e.g. EUR")
    area_m2: float | None = Field(default=None, description="Surface in square meters.")
    rooms: int | None = Field(default=None, description="Bedrooms.")
    bathrooms: int | None = None
    location: str | None = None


class HousingQuery(BaseQuery):
    q: str | None = Field(
        default=None, max_length=300, description="Free text (e.g. 'ático terraza')."
    )
    operation: Operation = "sale"
    property_type: PropertyType = "homes"
    location: str | None = Field(
        default=None,
        max_length=200,
        description="City / area name, or the portal's location slug (e.g. 'madrid-madrid').",
    )
    location_id: str | None = Field(default=None, description="Portal location id (Idealista API).")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_m: int | None = Field(default=None, ge=100, le=50_000)
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    min_area: float | None = Field(default=None, ge=0)
    max_area: float | None = Field(default=None, ge=0)
    min_rooms: int | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="after")
    def _need_location(self) -> HousingQuery:
        if not (
            self.location
            or self.location_id
            or (self.latitude is not None and self.longitude is not None)
        ):
            raise ValueError("provide 'location', 'location_id' or 'latitude'+'longitude'")
        return self
