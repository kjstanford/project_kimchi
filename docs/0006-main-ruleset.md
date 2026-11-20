# 0006: A ruleset protects `main`

**Decision.** `main` on GitHub is protected by the ruleset in `.github/rulesets/protect-main.json`. The owner
imports it by hand (Settings > Rules > Rulesets > New ruleset > Import a ruleset). `make ruleset-status` shows what
GitHub enforces on `main`, and `tests/test_main_ruleset.py` fails if the committed file is weakened.

| Rule | Effect |
|---|---|
| Require a pull request, 0 approvals | No direct push to `main`, by anyone, the owner included. 0 approvals, because a sole owner cannot approve their own pull request. |
| Restrict updates | Only a bypass actor may change `main`, so no one else can merge. |
| Block force pushes, restrict deletions | `main` cannot be rewritten or deleted. |
| Bypass: Repository admin, *for pull requests only* | The owner merges pull requests; a direct push is refused for the owner as well. |

**Why.**
- AGENTS.md says `main` changes only through a pull request the owner reviews and merges. The ruleset makes
  GitHub enforce that rule, rather than relying on every person and agent to follow it.
- It is imported by hand so that `GH_TOKEN` never needs *Administration: write*. A token that can edit rulesets
  could also remove them.

**What it cannot do.**
- It cannot stop anyone from *opening* a pull request; anyone can fork a public repository. Opening one is limited
  by an interaction limit instead (Settings > Moderation options > Interaction limits > Limit to repository
  collaborators). That limit lasts at most six months and then has to be renewed. It also blocks issues and
  comments from anyone outside the repository.
- It cannot tell the owner from the owner's token. `GH_TOKEN` (Pull requests: write) could merge a pull request
  as the owner; the guard is that no Makefile target merges (docs/0005).

**Cost.** Two manual steps on GitHub, and renewing the interaction limit every six months.
