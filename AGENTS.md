# qa-cli agent instructions

See `README.md` for what the tool does, install and the test command; this file carries the
rules every session in this repo follows.

## Heavy work runs on Runpod

This workstation is shared by many agent sessions, the review queue and the owner's own
desktop; a full suite here starves all of them (it froze the machine twice on 2026-10-10).
**Rule: locally, a single test file at most; the full suite goes to a Runpod pod** via the
shared `runpod-offload` skill (`skill://runpod-offload`; script
`~/.agents/skills/runpod-offload/runpod-run`, on PATH as `runpod-run`).

Heavy here: `uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q` (the
whole suite; the browser tests run against a stub driver, never Chromium, but several run
probe JS under `node`, so the pod needs `nodejs`).

```bash
runpod-run --apt nodejs -- uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q
                                        # full suite on a 4-vCPU CPU pod ($0.16/h), pod deleted when done
runpod-run --ttl 30 --apt nodejs -- uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests -q
                                        # same, keeping the pod 30 idle minutes between runs
uv run --no-project --with 'pytest>=8,<9' --with qrcode pytest tests/test_<one>.py -q
                                        # local: one file, that's the limit
```

`runpod-run --dry-run -- …` prints the plan and price. `runpod-run --status` lists every pod
on the account; bare `runpod-run --stop` terminates only this repo's pods, `runpod-run --stop
all` every pod created under this hostname — leave none behind.
