# Contributing to Plum-Audio

Plum-Audio is developed by a solo maintainer with AI assistance, on a four-unit Raspberry Pi rig.
Issues and pull requests are welcome; please read this first, because a few of the conventions here
exist to stop specific failures recurring.

## Before you change anything

Read **[docs/HARD-WON-LESSONS.md](docs/HARD-WON-LESSONS.md)**. Much of this codebase looks like it
could be simplified, and several of those simplifications have already been tried and reverted on
hardware. The file records what broke and why the current shape is what it is. A PR that removes
something listed there needs to explain what changed about the underlying constraint.

The governing principle is **audio reliability first**. A change that makes the code nicer and the
pipeline less predictable is not an improvement.

## Branches

| Branch | Purpose |
|---|---|
| `main` | Release branch. Protected. Every merge cuts a patch release. |
| `dev` | Integration branch. Builds `:dev` images. Target your PRs here. |
| `feature/*`, `bugfix/*`, `docs/*`, `refactor/*` | Your work. Branch from `dev`. |

Flow is `feature/*` → `dev` → `main`. Nothing lands on `main` except from `dev`.

Always `git pull --rebase`. Never force-push `main` or `dev`.

## Commits

**[Conventional Commits](https://www.conventionalcommits.org/)**, and atomic.

```
<type>(<scope>): <subject in the imperative>

<body: why this change, not what the diff already shows>
```

Types in use: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `perf`, `build`, `ci`.

Three rules that matter more than the format:

1. **One commit, one change.** The median commit in this repo touches two files. If your subject
   line needs an "and", it is probably two commits.
2. **The body explains *why*.** The diff already shows what. Commit messages here are load-bearing
   documentation — `docs/HARD-WON-LESSONS.md`, `docs/PHASE-HISTORY.md` and `CLAUDE.md` cite specific
   commit hashes, and `git log` is how the reasoning behind a subsystem is recovered.
3. **No `wip:` commits reach `dev`.** Squash or reword them on your branch first.

## Versioning and releases

Semantic versioning, with the increments split by who decides them:

- **Patch** — automatic. Every merge to `main` bumps the patch component, tags it, builds the image
  and cuts a GitHub Release. `1.0.0` → `1.0.1` → `1.0.2`. CI never touches major or minor.
- **Minor / major** — manual and deliberate. Push a `vX.Y.0` tag yourself; CI builds and releases it.

This deliberately differs from `semantic-release`-style tooling, which would bump minor on every
`feat:` commit. Feature work accumulates on `dev` and ships as patch releases until the maintainer
decides a version is a minor.

### Image tags

| Event | Tags pushed to `ghcr.io` |
|---|---|
| merge to `dev` | `:dev`, `:dev-<short-sha>` |
| merge to `main` | `:<X.Y.Z>`, `:latest` |
| `vX.Y.0` tag | `:<X.Y.Z>`, `:latest` |

Images are `linux/arm64` only. See the README for the amd64 caveat.

## Code style

- **Python** — `ruff` + `black`, 4-space, 120 columns. `snake_case` modules and functions,
  `PascalCase` classes. PEP 8 wins over any house convention.
- **TypeScript** — ESLint + Prettier, 2-space, 120 columns. `PascalCase.tsx` components,
  `camelCase.ts` services.
- **Constants and environment variables** — `UPPER_SNAKE_CASE`, prefixed `PLUM_`.

Run before pushing:

```bash
ruff check backend && black --check backend
python -m pytest tests/Unit -q
cd frontend && npm run test:run && npx tsc --noEmit
```

CI runs exactly these.

## Tests

Two tiers, and they are not interchangeable:

- **`tests/Unit/`** — pytest, no hardware, runs in CI. New backend logic needs coverage here.
- **`tests/Integration/`** — bash against real Pis. **Cannot run in CI**; there is no Pi rig on a
  GitHub runner. Run them yourself and say so in the PR. They require `PLUM_TEST_PW` in the
  environment or in `docker/.deploy.env`, and they leave the rig as they found it.

Some behaviour in this project is only observable on hardware — audio glitching, mDNS, PortAudio
device enumeration, Bluetooth. If your change touches the audio path, the mesh, or device
selection, **test it on hardware and say what you observed**. "The unit tests pass" is not
sufficient evidence for those subsystems, and several entries in HARD-WON-LESSONS.md exist because
something passed its tests and failed in a room.

## Local setup

```bash
cp docker/units.conf.example docker/units.conf   # then edit for your units
echo 'PLUM_TEST_PW=<your rig password>' > docker/.deploy.env
```

Both are gitignored. No credential and no real unit address belongs in a commit — please check
`git diff` before pushing if you have touched deploy or provisioning scripts.

## Reporting bugs

Include the unit's logs (`docker logs plum-audio`, and `/config/supervisord.log` inside the
container), the image tag, whether the unit has a local speaker or is running headless, and which
source was playing. For anything mesh-related, `curl http://<unit>:5001/api/mesh/view` from another
unit is usually the fastest thing to attach.

## License

By contributing you agree that your contributions are licensed under the
[GNU GPL v3.0](LICENSE).
