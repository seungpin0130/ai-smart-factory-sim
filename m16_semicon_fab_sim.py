"""Synthetic 300 mm fab model using a subset of SEMI E10 state categories.

The route, recipes, sensor baseline and transport rates are educational
parameters. The die-yield baseline uses a Poisson defect model.
"""

from __future__ import annotations
import math
import random
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Tuple


class SemiE10State(str, Enum):
    PRD = "PRD"  # Productive (Processing Wafers)
    SBY = "SBY"  # Standby (Idle / Waiting for FOUP or OHT)
    ENG = "ENG"  # Engineering (Monitor / Qualification Wafer)
    SDT = "SDT"  # Scheduled Downtime (PM, Chamber Dry Clean, Seasoning)
    UDT = "UDT"  # Unscheduled Downtime (BM: RF Arcing, Vacuum Leak, Chuck Fault)


@dataclass
class ProcessStep:
    step_seq: int
    step_name: str
    tool_group: str
    layer: int
    std_process_min: float
    q_time_limit_min: Optional[float] = None  # Max allowed wait time from previous step
    target_cd_nm: float = 14.0


# Re-entrant DRAM / HBM3E Core Process Route (12 Steps across 2 Litho-Etch-Depo Loops + HBM TSV)
M16_HBM3E_ROUTE: List[ProcessStep] = [
    ProcessStep(10, "WET_CLEAN_L1",   "CLEAN",   1, 12.0, q_time_limit_min=90.0),
    ProcessStep(20, "EUV_LITHO_L1",   "EUV",     1, 18.0, q_time_limit_min=120.0, target_cd_nm=13.8),
    ProcessStep(30, "DRY_ETCH_L1",    "ETCH",    1, 24.0, q_time_limit_min=180.0, target_cd_nm=13.5),
    ProcessStep(40, "ALD_HKMG_L1",    "DEPO",    1, 22.0, q_time_limit_min=120.0),
    ProcessStep(50, "CMP_PLANAR_L1",  "CMP",     1, 16.0, q_time_limit_min=240.0),
    ProcessStep(60, "INLINE_CD_SEM",  "METRO",   1,  8.0, q_time_limit_min=None),
    # Re-entrant Layer 2 (Bitline / Capacitor High-Aspect-Ratio)
    ProcessStep(70, "EUV_LITHO_L2",   "EUV",     2, 20.0, q_time_limit_min=120.0, target_cd_nm=12.5),
    ProcessStep(80, "HARC_ETCH_L2",   "ETCH",    2, 32.0, q_time_limit_min=150.0, target_cd_nm=12.2),
    ProcessStep(90, "GAPFILL_CVD_L2", "DEPO",    2, 25.0, q_time_limit_min=120.0),
    # HBM3E 3D Packaging & Final Wafer Test
    ProcessStep(100, "TSV_ETCH_CU",   "HBM_TSV", 3, 28.0, q_time_limit_min=180.0),
    ProcessStep(110, "HYBRID_BOND",   "HBM_TSV", 3, 26.0, q_time_limit_min=120.0),
    ProcessStep(120, "EDS_PROBE_TEST","EDS",     3, 15.0, q_time_limit_min=None),
]


@dataclass
class FoupLot:
    lot_id: str
    foup_id: str
    product: str = "HBM3E-16Hi"
    wafer_count: int = 25
    priority: str = "NORMAL"  # "HOT_LOT" or "NORMAL"
    route_idx: int = 0
    wait_min_at_step: float = 0.0
    total_cycle_min: float = 0.0
    q_time_violations: int = 0
    violated_steps: List[int] = field(default_factory=list)
    cumulative_cd_error_nm: float = 0.0
    defect_density_d0: float = 0.045  # defects / cm^2
    location: str = "STOCKER_A"       # STOCKER_A, OHT_IN_TRANSIT, LOADPORT_x, COMPLETED

    @property
    def current_step(self) -> Optional[ProcessStep]:
        if 0 <= self.route_idx < len(M16_HBM3E_ROUTE):
            return M16_HBM3E_ROUTE[self.route_idx]
        return None


@dataclass
class EquipmentTool:
    tool_id: str
    tool_group: str
    bay_id: str
    chamber_count: int
    state: SemiE10State = SemiE10State.SBY
    downtime_reason: Optional[str] = None
    downtime_remain_min: float = 0.0

    # Consumable / Hardware Wear & Weibull Reliability
    rf_on_hours: float = 120.0
    max_rf_hours: float = 500.0
    wafers_since_clean: int = 40
    clean_interval_wafers: int = 250  # Dry clean / seasoning cycle

    # Real-time Recipe & Sensor Telemetry
    chamber_temp_c: float = 60.0
    opt_temp_c: float = 60.0
    chamber_pressure_mtorr: float = 15.0
    rf_power_w: float = 1200.0
    load_ratio: float = 0.82
    feed_rate: float = 1.0

    # APC (Run-to-Run EWMA) & FDC Health Metrics
    temperature_setpoint_c: Optional[float] = None
    apc_bias_offset: float = 0.0      # Run-to-Run feedback compensation
    fdc_t2_score: Optional[float] = None        # Hotelling's T^2 (< 3.0 Normal, >= 3.0 Warning, >= 5.0 Fault)
    vm_predicted_cd_nm: Optional[float] = None

    # SEMI E10 Time Accumulators (minutes)
    time_prd: float = 0.0
    time_sby: float = 0.0
    time_eng: float = 0.0
    time_sdt: float = 0.0
    time_udt: float = 0.0
    processed_wafers: int = 0
    ideal_process_min: float = 0.0
    active_lot: Optional[FoupLot] = None
    proc_remain_min: float = 0.0

    @property
    def rul_pct(self) -> float:
        wear_ratio = min(1.0, self.rf_on_hours / self.max_rf_hours)
        temp_penalty = max(0.0, (self.chamber_temp_c - self.opt_temp_c) * 0.4)
        return round(max(0.0, (1.0 - wear_ratio) * 100.0 - temp_penalty), 3)

    @property
    def availability_pct(self) -> float:
        total = self.time_prd + self.time_sby + self.time_eng + self.time_sdt + self.time_udt
        if total <= 0:
            return 100.0
        uptime = self.time_prd + self.time_sby + self.time_eng
        return round((uptime / total) * 100.0, 1)


class M16SemiconFabSimulator:
    """
    Tick-driven (1 Tick = 15 minutes of Fab time) Discrete-Event & Physics Hybrid Engine.
    """

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.tick = 100
        self.tick_minutes = 15.0
        self.lot_seq = 1

        self.tools: Dict[str, EquipmentTool] = {
            "CLEAN_01":   EquipmentTool("CLEAN_01",   "CLEAN",   "BAY_01", 2, chamber_temp_c=23.0, opt_temp_c=23.0, rf_on_hours=60,  max_rf_hours=600),
            "EUV_01":     EquipmentTool("EUV_01",     "EUV",     "BAY_02", 1, chamber_temp_c=22.0, opt_temp_c=22.0, rf_on_hours=95,  max_rf_hours=450, chamber_pressure_mtorr=0.01, rf_power_w=350),
            "ETCH_01":    EquipmentTool("ETCH_01",    "ETCH",    "BAY_03", 4, chamber_temp_c=64.5, opt_temp_c=62.0, rf_on_hours=310, max_rf_hours=400, chamber_pressure_mtorr=18.5, rf_power_w=1850),
            "DEPO_01":    EquipmentTool("DEPO_01",    "DEPO",    "BAY_04", 4, chamber_temp_c=380.0,opt_temp_c=380.0,rf_on_hours=140, max_rf_hours=500, chamber_pressure_mtorr=240.0,rf_power_w=950),
            "CMP_01":     EquipmentTool("CMP_01",     "CMP",     "BAY_05", 3, chamber_temp_c=28.0, opt_temp_c=28.0, rf_on_hours=110, max_rf_hours=450, chamber_pressure_mtorr=760.0,rf_power_w=600),
            "METRO_01":   EquipmentTool("METRO_01",   "METRO",   "BAY_02", 2, chamber_temp_c=22.0, opt_temp_c=22.0, rf_on_hours=40,  max_rf_hours=800, chamber_pressure_mtorr=0.001,rf_power_w=200),
            "HBM_TSV_01": EquipmentTool("HBM_TSV_01", "HBM_TSV", "BAY_06", 2, chamber_temp_c=42.0, opt_temp_c=42.0, rf_on_hours=130, max_rf_hours=480, chamber_pressure_mtorr=5.0,  rf_power_w=1400),
            "EDS_01":     EquipmentTool("EDS_01",     "EDS",     "BAY_07", 4, chamber_temp_c=25.0, opt_temp_c=25.0, rf_on_hours=50,  max_rf_hours=900, chamber_pressure_mtorr=760.0,rf_power_w=450),
        }

        self.wip_lots: List[FoupLot] = []
        self.completed_lots: List[FoupLot] = []
        self.oht_vehicles_total = 32
        self.oht_active_transfers = 0
        self.oht_congestion_pct = 0.0
        self.oht_avg_delivery_sec = 0.0
        self.oht_routes: dict = {}
        self.last_power_kw = 380.0
        self.maintenance_events: list = []

        # Seed initial WIP FOUPs across route stages so line is warm
        for i in range(18):
            lot = self._create_new_lot(priority="HOT_LOT" if i % 5 == 0 else "NORMAL")
            lot.route_idx = i % (len(M16_HBM3E_ROUTE) - 1)
            self.wip_lots.append(lot)

    def checkpoint(self):
        fields = dict(vars(self))
        fields["rng"] = self.rng.getstate()
        fields["tools"] = {}
        for name, tool in self.tools.items():
            fields["tools"][name] = {
                **vars(tool), "state": tool.state.value,
                "active_lot": tool.active_lot.lot_id if tool.active_lot else None,
            }
        fields["wip_lots"] = [asdict(lot) for lot in self.wip_lots]
        fields["completed_lots"] = [asdict(lot) for lot in self.completed_lots]
        return fields

    @classmethod
    def restore(cls, fields):
        engine = cls.__new__(cls)
        engine.__dict__.update(fields)
        engine.rng = random.Random()
        version, state, gaussian = fields["rng"]
        engine.rng.setstate((version, tuple(state), gaussian))
        engine.wip_lots = [FoupLot(**lot) for lot in fields["wip_lots"]]
        engine.completed_lots = [FoupLot(**lot) for lot in fields["completed_lots"]]
        by_id = {lot.lot_id: lot for lot in engine.wip_lots}
        engine.tools = {}
        for name, raw in fields["tools"].items():
            engine.tools[name] = EquipmentTool(**{
                **raw, "state": SemiE10State(raw["state"]), "active_lot": by_id.get(raw["active_lot"]),
            })
        return engine

    def _create_new_lot(self, priority: str = "NORMAL") -> FoupLot:
        lot = FoupLot(
            lot_id=f"L2611-HBM3E-{self.lot_seq:03d}",
            foup_id=f"FOUP-{1000 + self.lot_seq}",
            priority=priority,
        )
        self.lot_seq += 1
        return lot

    def _dispatch_oht_and_lots(self) -> int:
        """
        Modeled real-time dispatching to an available load port:
        Prioritizes: 1) Q-Time critical lots, 2) HOT_LOT, 3) FIFO.
        """
        transfers = 0
        lanes = {}
        for tool in self.tools.values():
            if tool.state == SemiE10State.SBY and tool.active_lot is None:
                candidates = [
                    l for l in self.wip_lots
                    if l.current_step and l.current_step.tool_group == tool.tool_group
                    and l.location.startswith("STOCKER")
                ]
                if not candidates:
                    continue

                def dispatch_score(lot: FoupLot) -> Tuple[int, float]:
                    step = lot.current_step
                    q_urgency = 0
                    if step and step.q_time_limit_min:
                        remain = step.q_time_limit_min - lot.wait_min_at_step
                        if remain < 45.0:
                            q_urgency = 2
                    hot = 1 if lot.priority == "HOT_LOT" else 0
                    return (q_urgency * 10 + hot, lot.wait_min_at_step)

                candidates.sort(key=dispatch_score, reverse=True)
                chosen = candidates[0]
                origin = chosen.location.removeprefix("STOCKER_")
                lane = f"{origin}_TO_{tool.bay_id}"
                lanes[lane] = lanes.get(lane, 0) + 1
                chosen.location = f"LOADPORT_{tool.tool_id}"
                tool.active_lot = chosen
                tool.state = SemiE10State.PRD
                # Effective process time adjusted by tool load_ratio & chamber count
                step = chosen.current_step
                speed_factor = max(0.05, tool.load_ratio * tool.feed_rate * (1.0 + 0.08 * (tool.chamber_count - 1)))
                tool.proc_remain_min = round(step.std_process_min / speed_factor, 1)
                transfers += 1

        self.oht_active_transfers = min(self.oht_vehicles_total, 10 + transfers * 3 + len(self.wip_lots) // 3)
        self.oht_congestion_pct = round(min(98.0, (self.oht_active_transfers / self.oht_vehicles_total) * 100.0), 1)
        # Non-linear traffic delay curve when congestion > 75%
        congestion_penalty = max(0.0, (self.oht_congestion_pct - 75.0) * 1.8)
        self.oht_avg_delivery_sec = round(48.0 + self.oht_active_transfers * 0.55 + congestion_penalty, 1)
        self.oht_routes = {
            name: {"dispatched_lots": count, "congestion_ratio": self.oht_congestion_pct / 100,
                   "transfer_time_sec": self.oht_avg_delivery_sec,
                   "oht_power_kw": round(self.oht_active_transfers * 3.8 * count / transfers, 2)}
            for name, count in lanes.items()
        }
        return transfers

    def _update_fdc_and_apc(self, tool: EquipmentTool, step: ProcessStep, lot: FoupLot) -> None:
        """
        FDC (Hotelling T^2) + Virtual Metrology (VM) + APC Run-to-Run EWMA Compensation
        """
        wear = tool.rf_on_hours / tool.max_rf_hours
        # xᵀΣ⁻¹x for two residuals under a synthetic diagonal sensor baseline.
        rf_residual = (wear - 0.25) / 0.30 + self.rng.gauss(0.0, 0.18)
        thermal_residual = (tool.chamber_temp_c - tool.opt_temp_c) / 4.0
        tool.fdc_t2_score = round(rf_residual ** 2 + thermal_residual ** 2, 2)

        # Raw physical drift before APC compensation
        raw_cd_drift = (wear * 0.85) + ((tool.chamber_temp_c - tool.opt_temp_c) * 0.09) + self.rng.gauss(0.0, 0.06)
        net_cd_error = raw_cd_drift - tool.apc_bias_offset
        tool.vm_predicted_cd_nm = round(step.target_cd_nm + net_cd_error, 3)

        # EWMA Run-to-Run (R2R) APC controller updates bias for next FOUP
        lambda_ewma = 0.35
        tool.apc_bias_offset = round(tool.apc_bias_offset + lambda_ewma * net_cd_error, 4)

        # Accumulate wafer quality impact onto Lot
        lot.cumulative_cd_error_nm += abs(net_cd_error)
        if tool.fdc_t2_score > 3.2:
            lot.defect_density_d0 += 0.008 * (tool.fdc_t2_score - 3.0)

    def _compute_eds_yield(self, lot: FoupLot) -> float:
        """
        Approximate Poisson die yield with modeled CD and Q-Time penalties.
        """
        die_area_cm2 = 1.15  # Large HBM3E base/core die
        random_defect_yield = math.exp(-die_area_cm2 * lot.defect_density_d0) * 100.0
        parametric_loss = min(3.5, lot.cumulative_cd_error_nm * 0.45)
        q_time_loss = lot.q_time_violations * 0.85
        eds_yield = max(84.0, min(99.5, random_defect_yield - parametric_loss - q_time_loss))
        return round(eds_yield, 2)

    def start_maintenance(self, tool, kind, minutes, reason):
        self.maintenance_events.append(
            (self.tick, tool.tool_id, kind, tool.rul_pct, math.ceil(minutes / self.tick_minutes),
             1200.0 if kind == "PM" else 4500.0)
        )
        if tool.active_lot:
            tool.active_lot.location = "STOCKER_RECOVERY"
            if kind == "BM":
                tool.active_lot.defect_density_d0 += 0.025
            tool.active_lot = None
        tool.proc_remain_min = 0
        tool.state = SemiE10State.SDT if kind == "PM" else SemiE10State.UDT
        tool.downtime_reason = reason
        tool.downtime_remain_min = minutes

    @property
    def q_time_violations(self):
        return sum(lot.q_time_violations for lot in self.wip_lots + self.completed_lots)

    def snapshot(self, yield_pct=None, completed_wafers=0):
        if yield_pct is None:
            yield_pct = self._compute_eds_yield(self.completed_lots[-1]) if self.completed_lots else 98.45
        scheduled = sum(t.time_prd + t.time_sby + t.time_eng + t.time_sdt + t.time_udt
                        for t in self.tools.values())
        uptime = sum(t.time_prd + t.time_sby + t.time_eng for t in self.tools.values())
        availability = uptime / scheduled if scheduled else 1.0
        performance = min(1.0, sum(t.ideal_process_min for t in self.tools.values()) / uptime) if uptime else 0.0
        quality = (sum(self._compute_eds_yield(lot) * lot.wafer_count for lot in self.completed_lots)
                   / sum(lot.wafer_count for lot in self.completed_lots) / 100) if self.completed_lots else 0.0
        return {
            "tick": self.tick,
            "tick_minutes": self.tick_minutes,
            "kpi": {
                "eds_yield_pct": yield_pct,
                "oee_pct": round(availability * performance * quality * 100, 2),
                "availability_pct": round(availability * 100, 2),
                "performance_pct": round(performance * 100, 2),
                "quality_pct": round(quality * 100, 2),
                "power_kw": round(self.last_power_kw, 1),
                "wip_foup_count": len(self.wip_lots),
                "wip_wafer_count": sum(lot.wafer_count for lot in self.wip_lots),
                "completed_wafers": sum(lot.wafer_count for lot in self.completed_lots),
                "completed_wafers_this_tick": completed_wafers,
                "q_time_violations": self.q_time_violations,
            },
            "amhs_oht": {
                "active_vehicles": self.oht_active_transfers,
                "total_vehicles": self.oht_vehicles_total,
                "congestion_pct": self.oht_congestion_pct,
                "avg_delivery_sec": self.oht_avg_delivery_sec,
                "oht_power_kw": round(self.oht_active_transfers * 3.8, 1),
                "routes": self.oht_routes.copy(),
            },
            "tools": {
                tid: {
                    "state": t.state.value,
                    "downtime_reason": t.downtime_reason,
                    "rul_pct": t.rul_pct,
                    "chamber_temp_c": t.chamber_temp_c,
                    "fdc_t2_score": t.fdc_t2_score,
                    "vm_predicted_cd_nm": t.vm_predicted_cd_nm,
                    "apc_bias_offset": t.apc_bias_offset,
                    "availability_pct": t.availability_pct,
                    "active_lot": t.active_lot.lot_id if t.active_lot else None,
                    "active_foup": t.active_lot.foup_id if t.active_lot else None,
                    "active_step": t.active_lot.current_step.step_name if t.active_lot else None,
                    "time_min": {state: getattr(t, f"time_{state.lower()}")
                                 for state in ("PRD", "SBY", "ENG", "SDT", "UDT")},
                }
                for tid, t in self.tools.items()
            },
            "lots": [
                {**asdict(lot), "step_name": lot.current_step.step_name if lot.current_step else None}
                for lot in self.wip_lots
            ],
        }

    def step(self) -> Dict:
        self.tick += 1
        dt = self.tick_minutes

        # 1. New FOUP release based on EUV feed_rate
        euv_feed = self.tools["EUV_01"].feed_rate
        if len(self.wip_lots) < int(26 * euv_feed) and self.rng.random() < (0.65 * euv_feed):
            self.wip_lots.append(self._create_new_lot(priority="HOT_LOT" if self.tick % 6 == 0 else "NORMAL"))

        self._dispatch_oht_and_lots()
        for lot in self.wip_lots:
            if lot.location.startswith("STOCKER"):
                lot.wait_min_at_step += dt
                lot.total_cycle_min += dt
                step = lot.current_step
                if (step and step.q_time_limit_min is not None
                        and lot.wait_min_at_step > step.q_time_limit_min
                        and step.step_seq not in lot.violated_steps):
                    lot.q_time_violations += 1
                    lot.violated_steps.append(step.step_seq)
                    lot.defect_density_d0 += 0.006

        # 4. Advance Equipment Physics & SEMI E10 State Machine
        total_power_kw = 380.0 + self.oht_active_transfers * 3.8  # Base facility + OHT power
        tick_completed_yields: List[float] = []
        completed_wafers = 0

        for tool in self.tools.values():
            if tool.state in (SemiE10State.SDT, SemiE10State.UDT):
                if tool.state == SemiE10State.SDT:
                    tool.time_sdt += dt
                else:
                    tool.time_udt += dt
                tool.downtime_remain_min -= dt
                tool.chamber_temp_c += (tool.opt_temp_c - tool.chamber_temp_c) * 0.25
                total_power_kw += 45.0
                if tool.downtime_remain_min <= 0:
                    tool.state = SemiE10State.ENG
                    if (tool.downtime_reason in {"MANUAL_PM", "INJECTED_BM", "RF_GENERATOR_ARCING_FAULT"}
                            or tool.rf_on_hours >= tool.max_rf_hours * 0.85):
                        tool.rf_on_hours = 0.0
                    tool.wafers_since_clean = 0
                    tool.downtime_reason = None
                continue

            if tool.state == SemiE10State.ENG:
                tool.time_eng += dt
                tool.state = SemiE10State.SBY
                total_power_kw += 65.0
                continue

            if tool.state == SemiE10State.PRD and tool.active_lot is not None:
                tool.time_prd += dt
                tool.proc_remain_min -= dt
                tool.active_lot.total_cycle_min += dt

                # Thermal & RF wear physics
                tool.rf_on_hours += (dt / 60.0) * tool.load_ratio
                if tool.temperature_setpoint_c is not None:
                    tool.chamber_temp_c = tool.temperature_setpoint_c
                else:
                    tool.chamber_temp_c = round(
                        tool.chamber_temp_c + (tool.load_ratio - 0.78) * 1.4 + self.rng.gauss(0, 0.15), 1
                    )
                total_power_kw += (tool.rf_power_w / 1000.0) * tool.chamber_count * 42.0 * tool.load_ratio

                if tool.proc_remain_min <= 0:
                    lot = tool.active_lot
                    step = lot.current_step
                    if step:
                        self._update_fdc_and_apc(tool, step, lot)
                        tool.ideal_process_min += step.std_process_min

                    tool.processed_wafers += lot.wafer_count
                    tool.wafers_since_clean += lot.wafer_count
                    tool.active_lot = None
                    tool.state = SemiE10State.SBY

                    # Advance Lot to next step in route
                    lot.route_idx += 1
                    lot.wait_min_at_step = 0.0
                    if lot.route_idx >= len(M16_HBM3E_ROUTE):
                        lot.location = "COMPLETED"
                        eds_y = self._compute_eds_yield(lot)
                        tick_completed_yields.append(eds_y)
                        completed_wafers += lot.wafer_count
                        self.wip_lots.remove(lot)
                        self.completed_lots.append(lot)
                    else:
                        lot.location = f"STOCKER_{tool.bay_id}"

                    # Trigger Chamber Dry Clean / Seasoning if wafer counter exceeded
                    if tool.wafers_since_clean >= tool.clean_interval_wafers:
                        self.start_maintenance(tool, "PM", 30.0, "CHAMBER_DRY_CLEAN_SEASONING")
            else:
                tool.state = SemiE10State.SBY
                tool.time_sby += dt
                total_power_kw += 65.0

            if tool.state == SemiE10State.PRD and tool.rul_pct < 15 and self.rng.random() < 0.18:
                self.start_maintenance(tool, "BM", 90.0, "RF_GENERATOR_ARCING_FAULT")

        self.last_power_kw = total_power_kw
        avg_yield = round(sum(tick_completed_yields) / len(tick_completed_yields), 2) if tick_completed_yields else None
        return self.snapshot(avg_yield, completed_wafers)


if __name__ == "__main__":
    sim = M16SemiconFabSimulator(seed=2026)
    for _ in range(16):
        snap = sim.step()
    import json
    print(json.dumps(snap, indent=2, ensure_ascii=False))
