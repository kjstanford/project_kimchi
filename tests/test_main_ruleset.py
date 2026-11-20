"""The committed ruleset for `main` keeps the protections docs/0006-main-ruleset.md promises.

The file is imported by hand on GitHub (Settings > Rules > Rulesets > New > Import), so a weakened copy here
would be applied without any other check. This test is that check.
"""

import json
from pathlib import Path

RULESET = Path(__file__).resolve().parents[1] / ".github" / "rulesets" / "protect-main.json"


def _ruleset() -> dict:
    return json.loads(RULESET.read_text(encoding="utf-8"))


def _rules() -> dict[str, dict]:
    return {r["type"]: r.get("parameters", {}) for r in _ruleset()["rules"]}


def test_ruleset_is_active_on_main_only():
    rs = _ruleset()
    assert rs["target"] == "branch"
    assert rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"] == {"include": ["refs/heads/main"], "exclude": []}


def test_main_cannot_be_pushed_rewritten_or_deleted():
    rules = _rules()
    assert {"pull_request", "update", "non_fast_forward", "deletion"} <= rules.keys()
    assert rules["update"].get("update_allows_fetch_and_merge") is False
    # 0: a sole owner cannot approve their own pull request, so any higher count would block every merge.
    assert rules["pull_request"]["required_approving_review_count"] == 0


def test_only_the_admin_bypasses_and_only_through_a_pull_request():
    # RepositoryRole 5 is the admin role. "pull_request" mode lets it merge pull requests but not push.
    assert _ruleset()["bypass_actors"] == [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
    ]
