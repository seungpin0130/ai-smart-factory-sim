"""Preferred entry point: simulate, deliberate, persist a coherent dashboard."""
import argparse
import os
import random
from pathlib import Path
from ai_council import deliberate
from factory_sim import simulate
from models import FactoryConfig, FactoryState
from storage import read_json, write_json


def run(data_dir: Path, mode="mock", seed=None, provider=None):
    lock = data_dir / ".cycle.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        state = FactoryState.model_validate(read_json(data_dir / "state.json"))
        config = FactoryConfig.model_validate(read_json(data_dir / "config.json"))
        history_file = data_dir / "logs/history.json"
        history = read_json(history_file) if history_file.exists() else []
        new_state, config = simulate(state, config, random.Random(seed))
        recent = [{"cycle": item["cycle"], "state": item["state"],
                   "applied_config": item["applied_config"]} for item in history[-3:]]
        record = deliberate(new_state, config, mode, provider, recent)
        history = (history + [record])[-120:]
        write_json(data_dir / "state.json", new_state.model_dump())
        write_json(data_dir / "config.json", record["applied_config"])
        write_json(data_dir / f"logs/cycles/{new_state.cycle:08d}.json", record)
        write_json(data_dir / "logs/last_decision.json", record)
        write_json(history_file, history)
        # Browser reads one snapshot, so sensors, controls and logs match each other.
        write_json(data_dir / "dashboard.json", {"state": new_state.model_dump(),
            "config": record["applied_config"], "history": history})
        markdown = ["# 듀얼 AI 운영 회의록\n", "최근 120회. 전체 기록은 `logs/cycles/`에 보존됩니다.\n"]
        for item in reversed(history):
            markdown += [f"\n## Cycle {item['cycle']} · {item['mode']} · {item['timestamp']}\n",
                f"상태: {item['state']['status']} · 온도: {item['state']['temperature']}°C\n"]
            for turn in item["transcript"]:
                markdown += [f"\n### {turn['role']}\n", "```json\n" +
                    __import__("json").dumps(turn["payload"], ensure_ascii=False, indent=2) + "\n```\n"]
            markdown += [f"\n적용: `{item['applied_config']}`\n", f"사유: {item['action_reason']}\n",
                         f"규칙 보정: {', '.join(item['overrides']) or '없음'}\n"]
        (data_dir / "logs/council_history.md").write_text("\n".join(markdown), encoding="utf-8")
        print(f"Cycle {new_state.cycle}: {new_state.status}, council={record['mode']}")
        return record
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    run(args.data_dir, args.mode, args.seed)
