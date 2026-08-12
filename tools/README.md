# Tools

Things a **human** runs to see what is happening. Nothing here asserts, and
nothing here runs in CI.

That is the whole distinction, and it is the one that was missing:

| If it… | it lives in | because |
| --- | --- | --- |
| **asserts** — passes or fails | `protocol/` | it is part of the contract, and CI runs it |
| **prints** — a person reads it | `tools/` | it answers "what is going on", which has no pass/fail |
| **demonstrates** — for someone consuming the wire format | `examples/` | the audience is a consumer of the core, not its developer |
| **builds** something app-specific | `apps/<app>/scripts/` | it is part of that app's build, not the core's |

These used to be spread across `protocol/`, `apps/Raneen/scripts/` and
`crates/raneen-core/scripts/`, sorted by who happened to write them rather than
what they do. That cost something real: `try-live.sh` reached for
`scripts/conform.py`, the harness moved to `protocol/`, and the script stayed
broken for a day because **a script nobody runs in CI cannot fail visibly.**
`ARCHITECTURE.md` carried the same dead path. Both are fixed.

---

## What is here

| | |
| --- | --- |
| `zmq-watch.py` | Watch what a running core publishes on ZeroMQ. The everyday one |
| `which-core.sh` | Which core is the *running* app actually using — Rust or Python, and is it signed |
| `try-live.sh` | Record from your real microphone, then run it through the core in always-on mode |
| `live.py` | The microphone streamed continuously, transcripts printed as they arrive |
| `drive-helper.py` | Drive the helper's JSON protocol by hand. **Probably obsolete** — see below |
| `fetch-wakeword-models.sh` | Download openWakeWord models into `~/.cache/raneen/wakeword` |

Every one resolves paths from the repo root, so they work from any directory.

### `zmq-watch.py`

```bash
.venv/bin/python tools/zmq-watch.py              # events + audio summaries
.venv/bin/python tools/zmq-watch.py --audio off  # events only — best for watching behaviour
```

Needs `pyzmq`, which is why it wants `.venv/bin/python` rather than `python3`.

It replaced `examples/zmq_listener.py`, which did the same job but predated the
`utterance` field and could not group frames into recordings. One monitor is
better than two that disagree.

### `which-core.sh`

```bash
./tools/which-core.sh                       # the running app
./tools/which-core.sh /Applications/Raneen.app
```

Reads the **live process tree** first — a bundle can say one thing and the
running process another. Falls back to inspecting the bundle when nothing is
running.

### `fetch-wakeword-models.sh`

```bash
./tools/fetch-wakeword-models.sh              # feature models + alexa
./tools/fetch-wakeword-models.sh hey_jarvis   # + another wake word
```

Two of the three are the **shared** feature models, identical for every wake
word; only the classifier differs. Fetched rather than committed, for the same
reason the whisper weights are. The conformance suite skips its wake-word case
loudly until these exist.

### `try-live.sh` and `live.py`

Every fixture is synthesised speech over digital silence, which is the
friendliest possible input. These are for the input that actually matters: a
real room, a real voice, and whatever your fan is doing.

```bash
./tools/try-live.sh 12 silero    # record 12s, then transcribe it
.venv/bin/python tools/live.py   # continuous, until Ctrl-C
```

### `drive-helper.py` — deletion candidate

Its docstring justifies it by a *frozen Python* build taking ~18 s to reach
`ready`, which made the obvious shell one-liner useless. The Rust core reaches
`ready` in 0.05 s, and `protocol/conform.py` drives the same protocol with
assertions on top. Kept only because deleting it was not the task; flag it and
it goes.
