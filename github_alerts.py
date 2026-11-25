"""Idempotent incident lifecycle. Uses recorded council turns, no extra API calls."""
import json
import os
import re
from pathlib import Path
from models import classify
from storage import read_json

MARKER = "<!-- dual-ai-factory:managed-incident:v1 -->"


class GitHub:
    def __init__(self):
        import requests
        self.repo = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("Invalid repository")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})

    def request(self, method, path, **kwargs):
        response = self.session.request(method,
            f"https://api.github.com/repos/{self.repo}/{path}", timeout=30, **kwargs)
        # Avoid including response/request bodies or authorization in exceptions.
        if not response.ok:
            raise RuntimeError(f"GitHubHTTP{response.status_code}")
        return response.json()

    def pages(self, path, params=None):
        page = 1
        while True:
            batch = self.request("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            yield from batch
            if len(batch) < 100:
                break
            page += 1


def sync(record, github):
    # Mock runs, including CI, never create or close incidents.
    if record["requested_mode"] != "live":
        return
    issues = [i for i in github.pages("issues", {"state": "open"})
        if "pull_request" not in i and MARKER in (i.get("body") or "")
        and i.get("user", {}).get("login") == "github-actions[bot]"]
    status = classify(record["state"]["temperature"], record["state"]["defect_rate"],
                      record["state"]["raw_material"])
    if record["create_issue_alert"]:
        issue = issues[0] if issues else github.request("POST", "issues", json={
            "title": "🚨 [CRITICAL] 가상 공장 장애" if status == "CRITICAL" else "⚠️ AI 공장 점검 요청",
            "body": MARKER + "\n\n가상 공장 자동 감지. 복구되면 이 Issue를 자동 종료합니다.\n\n" +
                "```json\n" + json.dumps(record["state"], ensure_ascii=False, indent=2) + "\n```"})
        add_cycle_comments(record, github, issue["number"])
    elif status == "NORMAL":
        for issue in issues:
            number = issue["number"]
            marker = f"<!-- factory-recovery:{record['cycle']} -->"
            existing = list(github.pages(f"issues/{number}/comments"))
            if not any(marker in (c.get("body") or "") for c in existing):
                github.request("POST", f"issues/{number}/comments", json={"body": marker +
                    f"\nCycle {record['cycle']}: NORMAL 복구 확인. 적용 설정: `{record['applied_config']}`"})
            github.request("PATCH", f"issues/{number}", json={"state": "closed", "state_reason": "completed"})


def add_cycle_comments(record, github, number):
    comments = list(github.pages(f"issues/{number}/comments"))
    turns = record["transcript"] or [{"role": "규칙 기반 대체 제어", "payload": {
        "reason": record["action_reason"], "config": record["applied_config"]}}]
    for index, turn in enumerate(turns):
        marker = f"<!-- factory-turn:{record['cycle']}:{index} -->"
        if any(marker in (c.get("body") or "") for c in comments):
            continue
        github.request("POST", f"issues/{number}/comments", json={"body": marker +
            f"\n### Cycle {record['cycle']} · {turn['role']}\n" + "```json\n" +
            json.dumps(turn["payload"], ensure_ascii=False, indent=2) + "\n```"})


if __name__ == "__main__":
    sync(read_json(Path(__file__).parent / "logs/last_decision.json"), GitHub())
