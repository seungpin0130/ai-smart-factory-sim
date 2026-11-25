"""Validated public data contract. Percentages use 0–100, never fractions."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FactoryConfig(StrictModel):
    conveyor_speed: int = Field(ge=0, le=100)
    cooling_power: int = Field(ge=0, le=100)
    auto_restock: bool


class FactoryState(StrictModel):
    cycle: int = Field(ge=0)
    timestamp: str
    temperature: float = Field(ge=25, le=250)
    output_units: int = Field(ge=0)
    total_output: int = Field(ge=0)
    defect_rate: float = Field(ge=0, le=100)
    raw_material: int = Field(ge=0)
    status: Literal["NORMAL", "WARNING", "CRITICAL"]
    last_event: str


class Decision(StrictModel):
    new_config: FactoryConfig
    action_reason: str = Field(min_length=1)
    create_issue_alert: bool


class Audit(StrictModel):
    recommended_config: FactoryConfig
    risk_analysis: str = Field(min_length=1)
    approves_proposal: bool


def classify(temperature: float, defects: float, material: int) -> str:
    if temperature >= 82 or defects >= 8 or material < 80:
        return "CRITICAL"
    if temperature >= 70 or defects >= 4 or material < 200:
        return "WARNING"
    return "NORMAL"
