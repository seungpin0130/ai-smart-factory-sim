from __future__ import annotations
import copy
import math
import random
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


class SemiE10State(str, Enum):
    PRD = "PRD"  # Productive
    SBY = "SBY"  # Standby
    ENG = "ENG"  # Engineering
    SDT = "SDT"  # Scheduled Downtime (PM / Dry Clean)
    UDT = "UDT"  # Unscheduled Downtime (BM)


@dataclass
class ProcessStep:
    step_seq: int
    step_name: str
    tool_group: str
    layer: int
    std_process_min: float
    q_time_limit_min: Optional[float] = None
    target_cd_nm: float = 14.0


M16_HBM3E_ROUTE: List[ProcessStep] = [
    ProcessStep(10, "WET_CLEAN_L1",    "CLEAN",   1, 12.0, q_time_limit_min=90.0),
    ProcessStep(20, "EUV_LITHO_L1",    "EUV",     1, 18.0, q_time_limit_min=120.0, target_cd_nm=13.8),
    ProcessStep(30, "DRY_ETCH_L1",     "ETCH",    1, 24.0, q_time_limit_min=180.0, target_cd_nm=13.5),
    ProcessStep(40, "ALD_HKMG_L1",     "DEPO",    1, 22.0, q_time_limit_min=120.0),
    ProcessStep(50, "CMP_PLANAR_L1",   "CMP",     1, 16.0, q_time_limit_min=240.0),
    ProcessStep(60, "INLINE_CD_SEM",   "METRO",   1,  8.0, q_time_limit_min=None),
    ProcessStep(70, "EUV_LITHO_L2",    "EUV",     2, 20.0, q_time_limit_min=120.0, target_cd_nm=12.5),
    ProcessStep(80, "HARC_ETCH_L2",    "ETCH",    2, 32.0, q_time_limit_min=150.0, target_cd_nm=12.2),
    ProcessStep(90, "GAPFILL_CVD_L2",  "DEPO",    2, 25.0, q_time_limit_min=120.0),
    ProcessStep(100, "TSV_ETCH_CU",    "HBM_TSV", 3, 28.0, q_time_limit_min=180.0),
    ProcessStep(110, "HYBRID_BOND",    "HBM_TSV", 3, 26.0, q_time_limit_min=120.0),
    ProcessStep(120, "EDS_PROBE_TEST", "EDS",     3, 15.0, q_time_limit_min=None),
]


@dataclass
class FoupLot:
    lot_id: str
    foup_id: str
    product: str = "HBM3E-16Hi"
    wafer_count: int = 25
    priority: str = "NORMAL"
    route_idx: int = 0
    wait_min_at_step: float = 0.0
    total_cycle_min: float = 0.0
    q_time_violations: int = 0
    cumulative_cd_error_nm: float = 0.0
    defect_density_d0: float = 0.042
    location: str = "STOCKER_A"

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

    rf_on_hours: float = 120.0
    max_rf_hours: float = 500.0
    wafers_since_clean: int = 40
    clean_interval_wafers: int = 250

    chamber_temp_c: float = 60.0
    opt_temp_c: float = 60.0
    chamber_pressure_mtorr: float = 15.0
    rf_power_w: float = 1200.0
    load_ratio: float = 0.82
    feed_rate: float = 1.0

    apc_bias_offset: float = 0.0
    fdc_t2_score: float = 1.15
    vm_predicted_cd_nm: float = 13.8

    time_prd: float = 0.0
    time_sby: float = 0.0
    time_eng: float = 0.0
    time_sdt: float = 0.0
    time_udt: float = 0.0
    processed_wafers: int = 0
    active_lot: Optional[FoupLot] = None
    proc_remain_min: float = 0.0

    @property
    def rul_pct(self) -> float:
        wear_ratio = min(1.0, self.rf_on_hours / self.max_rf_hours)
        temp_penalty = max(0.0, (self.chamber_temp_c - self.opt_temp_c) * 0.4)
        return round(max(0.0, (1.0 - wear_ratio) * 100.0 - temp_penalty), 1)

    @property
    def availability_pct(self) -> float:
        total = self.time_prd + self.time_sby + self.time_eng + self.time_sdt + self.time_udt
        if total <= 0:
            return 100.0
        uptime = self.time_prd + self.time_sby + self.time_eng
        return round((uptime / total) * 100.0, 1)


class M16SemiconFabSimulator:
    def __init__(self, seed: int = 2026):
        self.seed = seed
        self.rng = random.Random(seed)
        self.tick = 100
        self.tick_minutes = 15.0
        self.lot_seq = 1
        self.total_q_time_violations = 0
        self.total_udt_events = 0
        self.extra_maint_cost_usd = 0.0

        self.tools: Dict[str, EquipmentTool] = {
            "CLEAN_01":   EquipmentTool("CLEAN_01",   "CLEAN",   "BAY_01", 2, chamber_temp_c=23.0, opt_temp_c=23.0, rf_on_hours=60,  max_rf_hours=600),
            "EUV_01":     EquipmentTool("EUV_01",     "EUV",     "BAY_02", 1, chamber_temp_c=22.0, opt_temp_c=22.0, rf_on_hours=95,  max_rf_hours=450, chamber_pressure_mtorr=0.01, rf_power_w=350),
            "ETCH_01":    EquipmentTool("ETCH_01",    "ETCH",    "BAY_03", 4, chamber_temp_c=65.2, opt_temp_c=62.0, rf_on_hours=325, max_rf_hours=400, chamber_pressure_mtorr=18.5, rf_power_w=1850),
            "DEPO_01":    EquipmentTool("DEPO_01",    "DEPO",    "BAY_04", 4, chamber_temp_c=380.0,opt_temp_c=380.0,rf_on_hours=140, max_rf_hours=500, chamber_pressure_mtorr=240.0,rf_power_w=950),
            "CMP_01":     EquipmentTool("CMP_01",     "CMP",     "BAY_05", 3, chamber_temp_c=28.0, opt_temp_c=28.0, rf_on_hours=110, max_rf_hours=450, chamber_pressure_mtorr=760.0,rf_power_w=600),
            "METRO_01":   EquipmentTool("METRO_01",   "METRO",   "BAY_02", 2, chamber_temp_c=22.0, opt_temp_c=22.0, rf_on_hours=40,  max_rf_hours=800, chamber_pressure_mtorr=0.001,rf_power_w=200),
            "HBM_TSV_01": EquipmentTool("HBM_TSV_01", "HBM_TSV", "BAY_06", 2, chamber_temp_c=42.0, opt_temp_c=42.0, rf_on_hours=130, max_rf_hours=480, chamber_pressure_mtorr=5.0,  rf_power_w=1400),
            "EDS_01":     EquipmentTool("EDS_01",     "EDS",     "BAY_07", 4, chamber_temp_c=25.0, opt_temp_c=25.0, rf_on_hours=50,  max_rf_hours=900, chamber_pressure_mtorr=760.0,rf_power_w=450),
        }

        self.wip_lots: List[FoupLot] = []
        self.completed_lots: List[FoupLot] = []
        self.oht_vehicles_total = 32
        self.oht_active_transfers = 14
        self.oht_congestion_pct = 43.8
        self.oht_avg_delivery_sec = 55.7

        for i in range(18):
            lot = self._create_new_lot(priority="HOT_LOT" if i % 5 == 0 else "NORMAL")
            lot.route_idx = i % (len(M16_HBM3E_ROUTE) - 1)
            self.wip_lots.append(lot)

    def _create_new_lot(self, priority: str = "NORMAL") -> FoupLot:
        lot = FoupLot(
            lot_id=f"L2611-HBM3E-{self.lot_seq:03d}",
            foup_id=f"FOUP-{1000 + self.lot_seq}",
            priority=priority,
        )
        self.lot_seq += 1
        return lot

    def apply_tool_control(
        self,
        tool_id: str,
        feed_rate: Optional[float] = None,
        load_ratio: Optional[float] = None,
        chamber_temp_c: Optional[float] = None,
        rul_pct: Optional[float] = None,
        action: Optional[str] = None,
    ) -> Dict:
        if tool_id not in self.tools:
            raise ValueError(f"Unknown tool_id: {tool_id}")
        tool = self.tools[tool_id]

        if feed_rate is not None:
            tool.feed_rate = max(0.2, min(1.5, float(feed_rate)))
        if load_ratio is not None:
            tool.load_ratio = max(0.3, min(1.0, float(load_ratio)))
        if chamber_temp_c is not None:
            tool.chamber_temp_c = float(chamber_temp_c)
        if rul_pct is not None:
            clamped = max(0.0, min(100.0, float(rul_pct)))
            tool.rf_on_hours = round(tool.max_rf_hours * (1.0 - clamped / 100.0), 1)
            if clamped > 0 and tool.state in (SemiE10State.SDT, SemiE10State.UDT):
                tool.state = SemiE10State.SBY
                tool.downtime_reason = None

        if action == "PM":
            tool.state = SemiE10State.SDT
            tool.downtime_reason = "SCHEDULED_PM_RECOVERY"
            tool.downtime_remain_min = 30.0
            tool.rf_on_hours = 0.0
            tool.chamber_temp_c = tool.opt_temp_c
            tool.fdc_t2_score = 1.05
            self.extra_maint_cost_usd += 1200.0
            if tool.active_lot:
                tool.active_lot.location = f"STOCKER_{tool.bay_id}"
                tool.active_lot = None
        elif action == "BM":
            tool.state = SemiE10State.UDT
            tool.downtime_reason = "INJECTED_RF_ARCING_BM"
            tool.downtime_remain_min = 90.0
            tool.rf_on_hours = tool.max_rf_hours
            self.total_udt_events += 1
            self.extra_maint_cost_usd += 4800.0
            if tool.active_lot:
                tool.active_lot.defect_density_d0 += 0.028
                tool.active_lot.location = f"STOCKER_{tool.bay_id}"
                tool.active_lot = None

        return self.snapshot()

    def _dispatch_oht_and_lots(self) -> int:
        transfers = 0
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
                chosen.location = f"LOADPORT_{tool.tool_id}"
                tool.active_lot = chosen
                tool.state = SemiE10State.PRD
                step = chosen.current_step
                speed_factor = max(0.5, tool.load_ratio * (1.0 + 0.08 * (tool.chamber_count - 1)))
                tool.proc_remain_min = round(step.std_process_min / speed_factor, 1)
                transfers += 1

        self.oht_active_transfers = min(self.oht_vehicles_total, 10 + transfers * 3 + len(self.wip_lots) // 3)
        self.oht_congestion_pct = round(min(98.0, (self.oht_active_transfers / self.oht_vehicles_total) * 100.0), 1)
        congestion_penalty = max(0.0, (self.oht_congestion_pct - 75.0) * 1.8)
        self.oht_avg_delivery_sec = round(48.0 + self.oht_active_transfers * 0.55 + congestion_penalty, 1)
        return transfers

    def _update_fdc_and_apc(self, tool: EquipmentTool, step: ProcessStep, lot: FoupLot) -> None:
        wear = tool.rf_on_hours / tool.max_rf_hours
        temp_drift = abs(tool.chamber_temp_c - tool.opt_temp_c)
        noise = self.rng.gauss(0.0, 0.16)
        tool.fdc_t2_score = round(max(0.4, 1.0 + wear * 2.5 + temp_drift * 0.35 + noise), 2)

        raw_cd_drift = (wear * 0.85) + ((tool.chamber_temp_c - tool.opt_temp_c) * 0.09) + self.rng.gauss(0.0, 0.05)
        net_cd_error = raw_cd_drift - tool.apc_bias_offset
        tool.vm_predicted_cd_nm = round(step.target_cd_nm + net_cd_error, 3)

        lambda_ewma = 0.35
        tool.apc_bias_offset = round(tool.apc_bias_offset + lambda_ewma * net_cd_error, 4)

        lot.cumulative_cd_error_nm += abs(net_cd_error)
        if tool.fdc_t2_score > 3.2:
            lot.defect_density_d0 += 0.008 * (tool.fdc_t2_score - 3.0)

    def _compute_eds_yield(self, lot: FoupLot) -> float:
        die_area_cm2 = 1.15
        random_defect_yield = math.exp(-die_area_cm2 * lot.defect_density_d0) * 100.0
        parametric_loss = min(3.5, lot.cumulative_cd_error_nm * 0.45)
        q_time_loss = lot.q_time_violations * 0.85
        return round(max(84.0, min(99.5, random_defect_yield - parametric_loss - q_time_loss)), 2)

    def step(self) -> Dict:
        self.tick += 1
        dt = self.tick_minutes

        euv_feed = self.tools["EUV_01"].feed_rate
        if len(self.wip_lots) < int(26 * euv_feed) and self.rng.random() < (0.68 * euv_feed):
            self.wip_lots.append(self._create_new_lot(priority="HOT_LOT" if self.tick % 6 == 0 else "NORMAL"))

        for lot in self.wip_lots:
            if lot.location.startswith("STOCKER"):
                lot.wait_min_at_step += dt
                lot.total_cycle_min += dt
                step = lot.current_step
                if step and step.q_time_limit_min and lot.wait_min_at_step > step.q_time_limit_min:
                    lot.q_time_violations += 1
                    self.total_q_time_violations += 1
                    lot.defect_density_d0 += 0.006
                    lot.wait_min_at_step = 0.0

        self._dispatch_oht_and_lots()

        total_power_kw = 380.0 + self.oht_active_transfers * 3.8
        tick_completed_yields: List[float] = []

        for tool in self.tools.values():
            if tool.state in (SemiE10State.SDT, SemiE10State.UDT):
                if tool.state == SemiE10State.SDT:
                    tool.time_sdt += dt
                else:
                    tool.time_udt += dt
                tool.downtime_remain_min -= dt
                tool.chamber_temp_c = round(tool.chamber_temp_c + (tool.opt_temp_c - tool.chamber_temp_c) * 0.25, 1)
                total_power_kw += 45.0
                if tool.downtime_remain_min <= 0:
                    tool.state = SemiE10State.SBY
                    tool.downtime_reason = None
                    if tool.rf_on_hours >= tool.max_rf_hours * 0.85:
                        tool.rf_on_hours = 0.0
                    tool.wafers_since_clean = 0
                continue

            if tool.state == SemiE10State.PRD and tool.active_lot is not None:
                tool.time_prd += dt
                tool.proc_remain_min -= dt
                tool.active_lot.total_cycle_min += dt

                tool.rf_on_hours += (dt / 60.0) * tool.load_ratio * 3.2
                tool.chamber_temp_c = round(
                    tool.chamber_temp_c + (tool.load_ratio - 0.78) * 1.45 + self.rng.gauss(0, 0.14), 1
                )
                total_power_kw += (tool.rf_power_w / 1000.0) * tool.chamber_count * 42.0 * tool.load_ratio

                if tool.proc_remain_min <= 0:
                    lot = tool.active_lot
                    step = lot.current_step
                    if step:
                        self._update_fdc_and_apc(tool, step, lot)

                    tool.processed_wafers += lot.wafer_count
                    tool.wafers_since_clean += lot.wafer_count
                    tool.active_lot = None
                    tool.state = SemiE10State.SBY

                    lot.route_idx += 1
                    lot.wait_min_at_step = 0.0
                    if lot.route_idx >= len(M16_HBM3E_ROUTE):
                        lot.location = "COMPLETED"
                        eds_y = self._compute_eds_yield(lot)
                        tick_completed_yields.append(eds_y)
                        self.wip_lots.remove(lot)
                        self.completed_lots.append(lot)
                    else:
                        lot.location = f"STOCKER_{tool.bay_id}"

                    if tool.wafers_since_clean >= tool.clean_interval_wafers:
                        tool.state = SemiE10State.SDT
                        tool.downtime_reason = "CHAMBER_DRY_CLEAN_SEASONING"
                        tool.downtime_remain_min = 30.0
            else:
                tool.state = SemiE10State.SBY
                tool.time_sby += dt
                total_power_kw += 65.0

            if tool.rul_pct < 16.0 and self.rng.random() < 0.22:
                tool.state = SemiE10State.UDT
                tool.downtime_reason = "RF_GENERATOR_ARCING_FAULT"
                tool.downtime_remain_min = 90.0
                self.total_udt_events += 1
                self.extra_maint_cost_usd += 4800.0
                if tool.active_lot:
                    tool.active_lot.defect_density_d0 += 0.025
                    tool.active_lot.location = "STOCKER_RECOVERY"
                    tool.active_lot = None

        return self.snapshot(
            override_power_kw=round(total_power_kw, 1),
            tick_completed_yields=tick_completed_yields,
        )

    def snapshot(
        self,
        override_power_kw: Optional[float] = None,
        tick_completed_yields: Optional[List[float]] = None,
    ) -> Dict:
        if tick_completed_yields:
            avg_yield = round(sum(tick_completed_yields) / len(tick_completed_yields), 2)
        elif self.completed_lots:
            recent = self.completed_lots[-3:]
            avg_yield = round(sum(self._compute_eds_yield(l) for l in recent) / len(recent), 2)
        else:
            avg_yield = 98.45

        avg_avail = round(sum(t.availability_pct for t in self.tools.values()) / len(self.tools), 1)
        perf_pct = round(min(100.0, (len(self.wip_lots) / 22.0) * 94.0), 1)
        oee_pct = round((avg_avail / 100.0) * (perf_pct / 100.0) * (avg_yield / 100.0) * 100.0, 1)
        power_kw = override_power_kw if override_power_kw is not None else 1185.0
        completed_wafers = max(25, len(self.completed_lots) * 25)
        unit_cost = round(
            1090.0 + (power_kw / 18.0) + (100.0 - avg_yield) * 32.0 + (self.extra_maint_cost_usd / completed_wafers),
            1,
        )

        return {
            "tick": self.tick,
            "kpi": {
                "eds_yield_pct": avg_yield,
                "oee_pct": oee_pct,
                "availability_pct": avg_avail,
                "power_kw": power_kw,
                "unit_cost_usd": unit_cost,
                "wip_foup_count": len(self.wip_lots),
                "wip_wafer_count": len(self.wip_lots) * 25,
                "completed_wafers": len(self.completed_lots) * 25,
                "q_time_violations": self.total_q_time_violations,
                "udt_events": self.total_udt_events,
            },
            "amhs_oht": {
                "active_vehicles": self.oht_active_transfers,
                "total_vehicles": self.oht_vehicles_total,
                "congestion_pct": self.oht_congestion_pct,
                "avg_delivery_sec": self.oht_avg_delivery_sec,
            },
            "tools": {
                tid: {
                    "tool_id": t.tool_id,
                    "tool_group": t.tool_group,
                    "bay_id": t.bay_id,
                    "state": t.state.value,
                    "downtime_reason": t.downtime_reason,
                    "rul_pct": t.rul_pct,
                    "chamber_temp_c": t.chamber_temp_c,
                    "opt_temp_c": t.opt_temp_c,
                    "load_ratio": t.load_ratio,
                    "feed_rate": t.feed_rate,
                    "fdc_t2_score": t.fdc_t2_score,
                    "vm_predicted_cd_nm": t.vm_predicted_cd_nm,
                    "apc_bias_offset": t.apc_bias_offset,
                    "availability_pct": t.availability_pct,
                    "active_lot": t.active_lot.lot_id if t.active_lot else None,
                    "active_step": (
                        t.active_lot.current_step.step_name
                        if (t.active_lot and t.active_lot.current_step)
                        else None
                    ),
                }
                for tid, t in self.tools.items()
            },
        }
