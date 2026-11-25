"""Build a validated GitHub contents PUT body. No network or credentials required."""
import argparse
import base64
import json
import re
from pathlib import Path
from models import FactoryConfig
from storage import read_json


def make_body(config, sha, message="fab: update operating parameters"):
    parsed = FactoryConfig.model_validate(config)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected the current config.json blob SHA")
    payload = (json.dumps(parsed.model_dump(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return {"message": message, "content": base64.b64encode(payload).decode("ascii"),
            "sha": sha, "branch": "main"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--message", default="fab: update operating parameters")
    args = parser.parse_args()
    print(json.dumps(make_body(read_json(args.config), args.sha, args.message), ensure_ascii=False, indent=2))
