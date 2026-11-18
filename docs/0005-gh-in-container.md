# 0005: The GitHub CLI in its own container

**Decision.** `gh` runs in a `gh` stage of `docker/Dockerfile` and a `gh` service in `docker/compose.yaml`
(profile `tools`), reached only through `make pr` and `make pr-status`.
- The stage installs a pinned release of `gh`, from the release tarball, checked against the release's
  sha256 file.
- The token is `GH_TOKEN` in `.env`.
- The repository is mounted read-only.

**Why.**
- Pull requests were opened by hand from a compare link, because `gh` is not on the host and nothing may be
  installed there (AGENTS.md).
- The app image is left alone, because the app does not need `gh`. A separate stage keeps the app image to what
  the app needs, the same reasoning as the `e2e` stage.

**Rules.**
- No merge target, and no generic `make gh ARGS=...`. `main` changes only through a pull request the owner
  reviews and merges, and powerful commands run only inside named targets.
- `make pr` refuses `BRANCH=main`, an empty title, a missing body file and a missing token. It does not push;
  pushing stays a separate `git push`.
- Git's `safe.directory` is set for the container only, through `GIT_CONFIG_*` variables. No git configuration
  is written.
- The token is read only from `.env` and is never printed. `make reviewer-check` drops it from the clone's
  environment.

**Cost.** One more image (Debian slim with git and gh). The token is a secret on disk in `.env`.
