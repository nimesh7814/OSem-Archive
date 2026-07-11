import os
import re
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import List, Literal, Optional, TypeAlias
from datetime import datetime, date

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "1826").replace(",", ""))

MeasurementAggregate: TypeAlias = Literal["raw", "hourly", "daily", "monthly", "yearly"]
ExportFormat: TypeAlias = Literal["csv", "geojson"]


class CommonMeasurementFilters(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    tags: Optional[List[str]] = None
    phenomena: Optional[List[str]] = None
    exposure: Optional[List[str]] = None
    aggregate: MeasurementAggregate = "daily"

    @field_validator("from_date", "to_date", mode="before")
    @classmethod
    def validate_date_format(cls, value):
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not DATE_ONLY_PATTERN.fullmatch(value):
            raise ValueError("Date must be in YYYY-MM-DD format.")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Date must be a valid calendar date in YYYY-MM-DD format.") from exc

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.to_date < self.from_date:
            raise ValueError("to must be on or after from.")
        if (self.to_date - self.from_date).days > DAYS_LIMIT:
            raise ValueError(f"Date range cannot exceed {DAYS_LIMIT} days.")
        return self


class Summary(BaseModel):
    stations: int
    sensors: int
    readings: int
    countries: int
    updated_at: datetime


class Tag(BaseModel):
    sensor_type: str


class Phenomenon(BaseModel):
    title: str


class Exposure(BaseModel):
    exposure: str
