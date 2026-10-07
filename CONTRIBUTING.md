# Contributing

Two suites guard the box: the Python unit suite and the Playwright browser
specs. Both are Linux-only, so on a Mac, or on any machine you do not want to
raise, run them in containers as shown here. CI runs the same commands
(`.github/workflows/ci.yml`, `.github/workflows/browser.yml`).

## Unit suite

The suite refuses root (scheduled-job tests check ownership), needs `jq`,
`tmux`, `ss` and `ps`, and commits in temporary repositories. Copy the
checkout into the container rather than mounting a worktree: a `git worktree`
holds a `.git` file that points at a host path the container cannot see.

```sh
docker run --rm --init -v "$PWD:/repo:ro" python:3.13 sh -c '
  cp -r /repo /work && useradd -m t && chown -R t /work &&
  apt-get -qq update >/dev/null && apt-get -qq install -y jq tmux procps iproute2 >/dev/null &&
  pip install -q -r /work/tests/requirements.txt &&
  su t -c "cd /work && git config --global user.email t@example.test && git config --global user.name t &&
    python -m unittest discover -s tests"'
```

`--init` matters: some tests start serve processes that would otherwise linger
as zombies and hang the run. One file: add `-p test_agent_jobs.py` to the
discover command, and `-k <substring>` for one test.

## Browser specs

`tests/browser/*.spec.js` drive a fixture dashboard at `http://127.0.0.1:3000`
in five mobile and narrow-desktop browser profiles (`playwright.config.js`).
The dashboard reads `/proc` and binds loopback only, so it runs in a container
behind a small forwarder, `tests/browser/forward.py`, that listens on
`0.0.0.0:3000` inside the container and pipes to the dashboard on
`127.0.0.1:4712`; Docker publishes that port to the host's loopback only.

```sh
docker run -d --rm --name fixture -p 127.0.0.1:3000:3000 -v "$PWD:/repo:ro" \
  -e GRAVE_ROOT=/tmp/g -e GRAVEDECAY_PORT=4712 -e GRAVEDECAY_PLATFORM=linux \
  -e GRAVEDECAY_ALLOWED_USERS=browser@example.test \
  python:3.13-slim sh -c 'mkdir -p /tmp/g/config /tmp/g/logs && cd /repo &&
    (python3 tests/browser/forward.py &) && python3 dashboard/gravedecay.py'
curl -sf http://127.0.0.1:3000/healthz
npm ci
npx playwright install chromium webkit
npx playwright test
docker stop fixture
```

`GRAVEDECAY_PLATFORM=linux` keeps the fixture on the appliance code paths;
`GRAVEDECAY_ALLOWED_USERS` makes it treat the `Tailscale-User-Login` header
the specs send as the owner. Nothing in the fixture touches the host.

## Where help is wanted

Three bounded areas. Each ships with the test or doctor line that keeps it
honest; a change without one will not be merged.

- **Prompts** (`prompts/*.txt`). A saved prompt for one kind of repository and
  one kind of night. It must leave the branch reviewable: commit everything,
  never push, merge or deploy, end with a short report. Paste the verdict line
  from a real run into the pull request.
- **Check detection** (`detect_checks` in `libexec/agent-jobs.py`). The runner
  reads the test entry point from the base commit; npm scripts, `pytest` and
  `make test` are covered. A new language needs the rule, a unit test in
  `tests/test_agent_jobs.py` and a sentence in `docs/SCHEDULES.md`.
- **Host profiles** (`profiles/<host>.sh`). A `profile_apply()` with a comment
  that says why, a matching `CHECK_*` flag in `grave.conf` and a `grave doctor`
  line that fails when the quirk returns. `profiles/README.md` has the shape.

Open a bug or a recipe with the issue templates. Secrets never enter git, every
listening port gets a row in `docs/PORTS.md` in the same commit, and a change
in platform behaviour updates the matching doc and a doctor check.
