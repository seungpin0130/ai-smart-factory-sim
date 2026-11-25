STAGES = ("PHOTO_EUV", "ETCH", "HBM_TSV")
BUFFER_CAPACITY = 100


def update_rul_and_pdm(equipments: dict, maint_logs: list, tick_id: int) -> list:
    """설비별 RUL(잔여 유효 수명) 비선형 감쇠 계산 및 AI 소집 이벤트 트리거 반환"""
    alerts = []
    for eq_id, eq in equipments.items():
        if eq["status"] == "MAINTENANCE":
            eq["maint_remain"] -= 1
            if eq["maint_remain"] <= 0:
                eq["status"] = "RUNNING"
                eq["maint_type"] = None
                eq["rul"] = 100.0
            continue

        excess_heat = max(0.0, eq["chamber_temp"] - eq.get("optimal_temp", 60.0))
        degradation = 0.8 + 1.2 * eq["load_ratio"] ** 2 + 0.15 * excess_heat
        previous_rul = eq["rul"]
        if eq["load_ratio"] > 0:
            eq["rul"] = max(0.0, round(eq["rul"] - degradation, 2))

        if eq["rul"] == 0.0:
            # 돌발 고장(BM) 발생 -> 긴 다운타임 & 막대한 원가 페널티
            eq["status"] = "MAINTENANCE"
            eq["maint_remain"] = 6
            eq["maint_type"] = "BM"
            maint_logs.append((tick_id, eq_id, "BM", previous_rul, 6, 4500.0))
            alerts.append(f"BM_FAILURE:{eq_id}")
        elif eq["rul"] < 20.0:
            # AI 위원회에 예방 정비(PM) 시점 합의 요청
            alerts.append(f"RUL_CRITICAL:{eq_id}(RUL={eq['rul']}%)")

    return alerts


def execute_preventive_maintenance(equipments: dict, eq_id: str, maint_logs: list, tick_id: int):
    """AI 위원회 합의에 따른 선제적 예방 정비(PM) 실행"""
    if eq_id not in equipments:
        return
    eq = equipments[eq_id]
    if eq["status"] != "MAINTENANCE":
        rul_before = eq["rul"]
        eq["status"] = "MAINTENANCE"
        eq["maint_remain"] = 2
        eq["maint_type"] = "PM"
        maint_logs.append((tick_id, eq_id, "PM", rul_before, 2, 1200.0))


def balance_wip_bottlenecks(wip_buffers: dict, equipments: dict, max_wip: int = BUFFER_CAPACITY) -> dict:
    """CONWIP 기반 공정 간 버퍼 임계치 동적 투입 속도(Feed Rate) 조절"""
    adjustments = {}
    for curr_stage, next_stage in zip(STAGES, STAGES[1:]):
        if curr_stage not in equipments or next_stage not in wip_buffers:
            continue

        next_ratio = wip_buffers[next_stage] / max_wip

        if next_ratio >= 0.80:
            # 후속 공정 병목 시 현재 공정 투입량을 40%로 감속하여 에너지 절감
            equipments[curr_stage]["feed_rate"] = 0.4
            adjustments[curr_stage] = "THROTTLED_40%"
        elif next_ratio <= 0.30:
            # 후속 공정 유휴(Starvation) 위험 시 가속
            equipments[curr_stage]["feed_rate"] = 1.2
            adjustments[curr_stage] = "BOOSTED_120%"
        else:
            equipments[curr_stage]["feed_rate"] = 1.0

    return adjustments
