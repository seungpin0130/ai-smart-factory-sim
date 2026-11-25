"""Verify both live council chair paths without persisting a factory cycle."""

import os
from pathlib import Path

from ai_council import deliberate
from models import FactoryConfig, FactoryState
from storage import read_json


def main():
    missing = [
        name for name in ("OPENAI_API_KEY", "GEMINI_API_KEY")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        return 1

    data_dir = Path(__file__).resolve().parent
    state = FactoryState.model_validate(read_json(data_dir / "state.json"))
    config = FactoryConfig.model_validate(read_json(data_dir / "config.json"))
    first_cycle = state.cycle + 1
    failed = False
    for cycle in (first_cycle, first_cycle + 1):
        sample = state.model_copy(update={"cycle": cycle})
        record = deliberate(sample, config, mode="live", recent=[sample.model_dump()], force=True)
        chair = "Gemini" if cycle % 2 else "ChatGPT"
        if record["mode"] == "live" and len(record["transcript"]) == 3 and record["chair"] == chair:
            print(f"Cycle {cycle}, {chair} chair: passed (3 validated responses)", flush=True)
        else:
            failed = True
            print(
                f"Cycle {cycle}, {chair} chair: failed "
                f"(mode={record['mode']}, completed_turns={len(record['transcript'])}, "
                f"error_type={record['error_type']})",
                flush=True,
            )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
