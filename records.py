"""Shared persistence for standalone council and full simulation runner."""
import json
from pathlib import Path
from storage import read_json, write_json


def save_record(root: Path, record):
    path = root / "logs/history.json"
    history = read_json(path) if path.exists() else []
    # Re-running the council for one cycle updates that cycle instead of duplicating it.
    history = [r for r in history if r["cycle"] != record["cycle"]]
    history = (history + [record])[-120:]
    write_json(root / "config.json", record["applied_config"])
    write_json(root / f"logs/cycles/{record['cycle']:08d}.json", record)
    write_json(root / "logs/last_decision.json", record)
    write_json(path, history)
    write_json(root / "dashboard.json", {"state": record["state"],
        "config": record["applied_config"], "history": history})
    markdown = ["# 가상 반도체 팹 듀얼 AI 회의록\n",
                "최근 120회. 전체 JSON 원장은 `logs/cycles/`에 보존됩니다.\n"]
    for item in reversed(history):
        s = item["state"]
        markdown += [f"\n## Cycle {item['cycle']} · {item['mode']} · {item['timestamp']}\n",
            f"상태 {s['status']} / 챔버 {s['chamber_temp']}°C / EDS {s['eds_yield']}% / "
            f"청정도 {s['cleanroom_class']} / 펠리클 {s['pellicle_health']}% / 재고 {s['foup_wafer_stock']}매\n"]
        for turn in item["transcript"]:
            markdown += [f"\n### {turn['role']}\n", "```json\n" +
                json.dumps(turn["payload"], ensure_ascii=False, indent=2) + "\n```\n"]
        markdown += [f"\n실제 적용: `{item['applied_config']}`\n",
            f"사유: {item['action_reason']}\n", f"SOP 보정: {', '.join(item['overrides']) or '없음'}\n"]
        if item["error_type"]:
            markdown.append(f"API/검증 오류 유형: {item['error_type']} (규칙 기반 대체 제어)\n")
    (root / "logs/council_history.md").write_text("\n".join(markdown), encoding="utf-8")
    return history
