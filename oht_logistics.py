"""Estimate congestion, transit time and power for the virtual OHT routes."""

# 가상 팹 베이(Bay) 간 모델 거리 (미터) 및 기본 OHT 사양
BAY_DISTANCES = {
    ("PHOTO_EUV", "ETCH"): 140.0,
    ("ETCH", "HBM_TSV"): 185.0,
}

OHT_NOMINAL_SPEED_MPS = 3.2   # 평균 주행 속도 (m/s)
HOIST_HANDLING_SEC = 18.0     # FOUP 픽업/드롭 승강 시간 (초)
TOTAL_OHT_VEHICLES = 48
RESERVE_VEHICLES = 6
CARRIER_POWER_KW = 1.85


def simulate_oht_logistics(wip_buffers: dict, equipments: dict) -> dict:
    """
    공정 간 WIP 물동량과 투입 속도(feed_rate)를 기반으로
    OHT 천장 반송 대차 혼잡도, 반송 지연 시간, 반송 전력 소모를 계산합니다.
    """
    route_metrics = {}
    total_active_carriers = 0
    alerts = []

    route_capacity = (TOTAL_OHT_VEHICLES - RESERVE_VEHICLES) / len(BAY_DISTANCES)
    for (src, dst), distance in BAY_DISTANCES.items():
        src_feed = equipments.get(src, {}).get("feed_rate", 1.0)
        src_running = 1.0 if equipments.get(src, {}).get("status") == "RUNNING" else 0.0
        dst_wip = wip_buffers.get(dst, 50)

        # 활성 반송 대차 수 추정 (출발지 가동률 + 목적지 버퍼 적체로 인한 대기 대차)
        moving_vehicles = int(10 * src_feed * src_running + 0.5)
        queuing_vehicles = int(max(0, dst_wip - 65) * 0.25 + 0.5)
        active_vehicles = moving_vehicles + queuing_vehicles
        total_active_carriers += active_vehicles

        # 혼잡도 (0.0 ~ 1.0+)
        congestion = round(min(1.5, active_vehicles / route_capacity), 3)

        # 혼잡도에 따른 비선형 주행 지연 (BPR 교통량-지연 함수 응용)
        base_travel_sec = (distance / OHT_NOMINAL_SPEED_MPS) + HOIST_HANDLING_SEC
        delay_factor = 1.0 + 0.65 * congestion ** 2.2
        actual_transfer_sec = round(base_travel_sec * delay_factor, 1)

        route_key = f"{src}_TO_{dst}"
        route_metrics[route_key] = {
            "active_carriers": active_vehicles,
            "congestion_ratio": congestion,
            "transfer_time_sec": actual_transfer_sec,
            "oht_power_kw": round(active_vehicles * CARRIER_POWER_KW, 1),
            "status": "CONGESTED" if congestion > 0.85 else "SMOOTH"
        }

        if congestion > 0.85:
            alerts.append(f"OHT_CONGESTION:{route_key}({int(congestion * 100)}%)")

    utilization = round(min(1.0, total_active_carriers / TOTAL_OHT_VEHICLES), 3)
    oht_power_kw = round(total_active_carriers * CARRIER_POWER_KW, 1)  # 대차당 평균 1.85kW

    return {
        "total_vehicles": TOTAL_OHT_VEHICLES,
        "active_vehicles": total_active_carriers,
        "utilization": utilization,
        "oht_power_kw": oht_power_kw,
        "routes": route_metrics,
        "alerts": alerts
    }
