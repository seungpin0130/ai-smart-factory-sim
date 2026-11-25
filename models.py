"""Validated public data contract. Percentages use 0–100, never fractions."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class FactoryConfig(StrictModel):
    euv_throughput: int = Field(ge=0, le=100)
    chiller_power: int = Field(ge=0, le=100)
    auto_foup_order: bool
    schedule_pm: bool = False


class FactoryState(StrictModel):
    cycle: int = Field(ge=0)
    timestamp: str
    chamber_temp: float = Field(ge=20, le=150)
    wafer_output: int = Field(ge=0)
    total_wafer_output: int = Field(ge=0)
    eds_yield: float = Field(ge=0, le=100)
    foup_wafer_stock: int = Field(ge=0)
    hbm_wip_buffer: int = Field(default=40, ge=0)
    cleanroom_class: int = Field(ge=1, le=100)
    pellicle_health: float = Field(ge=0, le=100)
    power_kwh: float = Field(default=420.0, ge=0)
    unit_cost_index: float = Field(default=100.0, ge=0)
    ramp_up_remaining: int = Field(default=0, ge=0, le=2)
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


class EquipmentAction(StrictModel):
    PHOTO_EUV_feed_rate: Literal[0.4, 1.0, 1.2]
    ETCH_feed_rate: Literal[0.4, 1.0, 1.2]
    PHOTO_EUV_action: Literal["KEEP_RUNNING", "SCHEDULE_PM_NOW"]
    ETCH_action: Literal["KEEP_RUNNING", "SCHEDULE_PM_NOW"]
    HBM_TSV_action: Literal["KEEP_RUNNING", "SCHEDULE_PM_NOW"]
    target_temp: float = Field(ge=45, le=64)


class FabDecision(Decision):
    equipment_action: EquipmentAction


class FabAudit(Audit):
    equipment_action: EquipmentAction


def classify(chamber_temp: float, eds_yield: float, foup_wafer_stock: int,
             cleanroom_class: int = 10, pellicle_health: float = 100,
             hbm_wip_buffer: int = 0) -> str:
    if chamber_temp >= 68 or eds_yield < 88 or foup_wafer_stock < 50 or cleanroom_class >= 45:
        return "CRITICAL"
    if chamber_temp >= 62 or eds_yield < 94 or foup_wafer_stock < 120 or cleanroom_class > 30 or pellicle_health < 35 or hbm_wip_buffer > 85:
        return "WARNING"
    return "NORMAL"


def classify_state(state: FactoryState) -> str:
    return classify(state.chamber_temp, state.eds_yield, state.foup_wafer_stock,
                    state.cleanroom_class, state.pellicle_health, state.hbm_wip_buffer)
