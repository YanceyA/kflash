# First-Flash Follow-ups Implementation Plan (3 PRs)

> **For Claude (orchestrator):** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development, one PR at a time — fresh implementer subagent per task, spec review then code-quality review after each task, final whole-branch review before each PR. All design decisions below are settled; do not re-litigate them.

**Goal:** Close the gaps found in the post-fix review of the "fresh Katapult install → first Klipper flash" journey: the Flash All seeded-config review bypass, the misleading safety-gate message when Klippy isn't ready, the dashboard's silence about bootloader-mode devices, a non-prefix-agnostic MCU-name matcher, CAN "connected" semantics, and a docs gap about what kflash can target.

**Architecture:** Three independent PRs, ordered by priority. PR-1 and PR-2 branch off `main` and are engine-only. PR-3 touches only the dashboard UI and **requires PR #3 (`fix/katapult-first-flash`) to be merged first** — it imports `discovery.is_katapult_device` from that PR.

**Tech Stack:** Python 3.9+ stdlib engine; Textual UI; pytest (+ `pytest-textual-snapshot` for `tests/ui/`, installed on the Pi but not in the local Windows venv — UI snapshot errors locally are expected and ignorable; the Pi run is authoritative).

---

## Implementation status

| PR | Status | Branch | PR |
|----|--------|--------|----|
| **PR-1** — Flash All seeded-config gate | ✅ **Done** | `claude/first-flash-followups-pr1-uvm6rs` | [#4](https://github.com/YanceyA/kflash/pull/4) |
| **PR-2** — First-flash UX (safety gate + prefix-agnostic match + docs) | ✅ **Done** | `feat/first-flash-ux` | [#5](https://github.com/YanceyA/kflash/pull/5) |
| **PR-3** — Dashboard bootloader / CAN states | ⏳ **Ready to start once its dependency merges** | `feat/dashboard-bootloader-status` (not yet created) | — |

- PR-1 and PR-2 are implemented, verified, pushed, and opened. Each was verified with `ruff check` clean, `mypy kflash` clean (only the pre-existing Python-3.9 notice), and the **full pytest suite green including all 12 `tests/ui` snapshots** (the snapshot deps were installed and the UI tests actually run, so the "12 env errors" caveat did not apply). Verification ran in a Linux environment rather than the Windows venv / Pi.
- **PR-3 is blocked only on its stated dependency:** it imports `discovery.is_katapult_device`, which lands with PR #3 (`fix/katapult-first-flash`, currently open as [#3](https://github.com/YanceyA/kflash/pull/3)). As soon as #3 merges to `main`, PR-3 is ready to start — branch off updated `main`, then follow §PR-3 below unchanged.
- Note: PR-1 shipped on branch `claude/first-flash-followups-pr1-uvm6rs` rather than the `fix/flash-all-seeded-gate` name suggested below; the code and behavior match §PR-1 exactly.

---

## Orchestrator ground rules

- Repo: `C:\dev_projects\kflash` (Windows). Local venv: `./.venv/Scripts/python.exe` (has pytest, ruff, mypy; does NOT have textual snapshot deps — 12 `tests/ui` errors locally are environmental).
- Verification matrix per PR (all must pass before opening the PR):
  ```bash
  ./.venv/Scripts/python.exe -m pytest --ignore=tests/test_worker_thread_signals.py -q   # local (ignore the 12 tests/ui env errors)
  ./.venv/Scripts/python.exe -m ruff check .
  ./.venv/Scripts/python.exe -m mypy kflash    # the "python_version 3.9" config notice is pre-existing; ignore it
  tar czf - --exclude='__pycache__' kflash tests pyproject.toml | ssh yanceya@192.168.50.50 "cd ~/kflash && tar xzf -"
  ssh yanceya@192.168.50.50 "cd ~/kflash && .venv/bin/python3 -m pytest tests/ -q"       # authoritative: must be all-pass
  ```
- `gh` is NOT installed. To open each PR: push the branch, then adapt the working script pattern from this session — `git credential fill` for the token + a `urllib.request` POST to `https://api.github.com/repos/YanceyA/kflash/pulls` (see the PR-body templates in each PR section). Never print the token.
- Commit-message style: conventional commits (`fix:`/`feat:`/`test:`/`docs:`), matching recent history.
- `CLAUDE.md` is **gitignored** in this repo. Steps that touch it are on-disk-only edits (they will not appear in any commit); do them anyway so the local project instructions stay accurate.
- Base all statements about current line numbers against `main` after PR #3 merges; small drift is expected — anchor edits on the quoted code, not the line numbers.

---

# PR-1: Flash All must not flash a seeded, unreviewed config

> ✅ **Done** — shipped on `claude/first-flash-followups-pr1-uvm6rs`, opened as [#4](https://github.com/YanceyA/kflash/pull/4).

**Branch:** `fix/flash-all-seeded-gate` (off `main`)
**Priority:** highest — this is a hole in a safety invariant CLAUDE.md explicitly claims ("a seeded config cannot reach build/flash even when `menuconfig_before_flash` is off").

**The bug:** `cmd_flash_all` Stage 1 (`kflash/commands/flash_batch.py:158-235`) validates only `config_mgr.cache_path.exists()` and the MCU match. `ConfigManager.is_seeded()` (`kflash/config.py:198`) is never consulted, so a freshly seeded, never-reviewed config passes validation and gets built/flashed. The single-flash path gates this in two places (`ui/screens/dashboard.py:_menuconfig_gate` via `menuconfig.needs_review`, and `flash_steps.load_and_validate_config` which forces a menuconfig on seeded configs); Flash All has no equivalent and cannot run menuconfig (batch, no ncurses).

**Settled behavior:** skip seeded devices with a warning and continue with the rest (mirrors the existing blocked-device skip at `flash_batch.py:180-182`), rather than hard-failing the whole batch the way missing configs do. If the skip leaves zero devices, error out (rc 1). Side effect (acceptable, arguably an improvement): a device with an orphaned `.seeded` marker and no cache is now skipped-with-warning instead of hard-failing the entire batch via the missing-configs check — the marker fails safe either way.

### Task 1.1: Seeded-skip in Stage 1

**Files:**
- Modify: `kflash/commands/flash_batch.py` (Stage 1, between the blocked-device filter and the missing-configs check, ~line 188)
- Test: `tests/test_command_flash_batch.py`

**Step 1: Write the failing tests**

`tests/test_command_flash_batch.py` already has the pieces: `_FakeConfigManager` (~line 99), `_FakeRegistry`, `_reach_version_stage(monkeypatch, *, outdated)` (~line 121) which monkeypatches `flash_batch.ConfigManager = _FakeConfigManager`, and `_registry_one_usb()`. Extend them:

(a) Add `is_seeded` to `_FakeConfigManager`, driven by a class attribute so tests can flip it per-key:

```python
class _FakeConfigManager:
    seeded_keys: set = set()  # tests mutate this per-case; reset in each test

    def __init__(self, key, klipper_dir):
        self.key = key
        self.cache_path = types.SimpleNamespace(exists=lambda: True)

    def is_seeded(self):
        return self.key in self.seeded_keys

    def load_cached_config(self):
        return True

    def validate_mcu(self, mcu):
        return (True, mcu)

    def get_cache_age_display(self):
        return ""
```

(b) New tests (new section at the end of the file). Import `RecordingSink` from conftest (extend the existing `from conftest import FakeDecisionProvider` line):

```python
# ---------------------------------------------------------------------------
# Seeded-but-unreviewed configs are skipped by Flash All (review gate parity)
# ---------------------------------------------------------------------------


def _registry_two_usb():
    a = DeviceEntry(key="octo", name="Octopus", mcu="stm32h723",
                    serial_pattern="usb-Klipper_x*")
    b = DeviceEntry(key="nite", name="Nitehawk", mcu="rp2040",
                    serial_pattern="usb-Klipper_y*")
    data = RegistryData(
        global_config=GlobalConfig(klipper_dir="/tmp/k", katapult_dir="/tmp/kt"),
        devices={"octo": a, "nite": b},
    )
    return _FakeRegistry(data)


def test_flash_all_skips_seeded_device_and_continues(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", {"nite"})
    sink = RecordingSink()
    em = Emitter(sink)
    # Cancel at the "Flash N device(s)?" confirm -- Stage 1 is what's under test.
    decider = FakeDecisionProvider(confirms={"flash_batch": False})

    rc = cmd_flash_all(_registry_two_usb(), em, decider)

    assert rc == 0  # cancelled at the batch confirm, not an error
    text = sink.text()
    assert "Skipping Nitehawk" in text
    assert "not reviewed" in text
    # The surviving device count excludes the seeded one.
    assert "1 device(s) validated" in text


def test_flash_all_all_seeded_errors(monkeypatch):
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", {"octo"})
    sink = RecordingSink()
    em = Emitter(sink)

    rc = cmd_flash_all(_registry_one_usb(), em, FakeDecisionProvider())

    assert rc == 1
    assert "Skipping Octopus" in sink.text()


def test_flash_all_unseeded_devices_unaffected(monkeypatch):
    # Regression guard: with no seeded keys, Stage 1 validates both devices.
    _reach_version_stage(monkeypatch, outdated=True)
    monkeypatch.setattr(_FakeConfigManager, "seeded_keys", set())
    sink = RecordingSink()
    em = Emitter(sink)
    decider = FakeDecisionProvider(confirms={"flash_batch": False})

    rc = cmd_flash_all(_registry_two_usb(), em, decider)

    assert rc == 0
    assert "Skipping" not in sink.text()
    assert "2 device(s) validated" in sink.text()
```

Note for the implementer: `_reach_version_stage` sets `outdated=True` here so the version stage never adds an extra confirm before `flash_batch`; check the helper's actual behavior and adjust the scripted confirms if a different prompt fires first. Anchor assertions on emitted text via `RecordingSink.text()`, matching the file's existing style.

**Step 2: Run to verify the new tests fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_command_flash_batch.py -q`
Expected: the two seeded tests FAIL (no "Skipping … not reviewed" is emitted; count says 2); `test_flash_all_unseeded_devices_unaffected` passes (current behavior). Note: adding `is_seeded` to `_FakeConfigManager` must not break the pre-existing tests.

**Step 3: Implement**

In `kflash/commands/flash_batch.py`, directly after the blocked-device filtering block ends (`flashable_devices = unblocked_devices`, ~line 188) and before the "Check cached configs exist" block:

```python
    # Seeded-but-unreviewed configs never reach a batch build: Flash All cannot
    # run menuconfig, so the mandatory one-time review of an auto-seeded config
    # (see menuconfig.needs_review) must happen on the single-flash path first.
    seeded_devices: list = []
    reviewed_devices: list = []
    for entry in flashable_devices:
        config_mgr = ConfigManager(entry.key, klipper_dir)
        if config_mgr.is_seeded():
            seeded_devices.append(entry)
        else:
            reviewed_devices.append(entry)

    for entry in seeded_devices:
        em.warn(
            f"Skipping {entry.name}: config was auto-seeded and not reviewed"
            " -- open menuconfig (M) or flash it individually first"
        )

    if not reviewed_devices:
        em.error("All devices need a config review. Nothing to flash.")
        return 1

    flashable_devices = reviewed_devices
```

**Step 4: Run to verify all pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_command_flash_batch.py -q`
Expected: all pass.

**Step 5: Commit**

```bash
git add kflash/commands/flash_batch.py tests/test_command_flash_batch.py
git commit -m "fix(flash-all): skip seeded-but-unreviewed configs in batch validation

Flash All validated only cache existence + MCU match, so an auto-seeded
config that never got its mandatory menuconfig review could build and
flash via B, bypassing the review gate the single-flash path enforces.
Seeded devices are now skipped with a warning (batch cannot host
menuconfig); an all-seeded batch errors out."
```

### Task 1.2: Docs

**Files:**
- Modify: `docs/flashing.md` — in the Flash All section, add one sentence: devices whose config was auto-seeded but never reviewed in menuconfig are skipped; review via `M` or a single flash first.
- Modify (on-disk only, gitignored): `CLAUDE.md` — in the "`.seeded` marker semantics" section, extend the "Forced review gate" bullet: the gate also applies to Flash All, which skips seeded devices during Stage-1 validation.

Commit (docs/flashing.md only): `docs: note Flash All skips unreviewed seeded configs`

### Task 1.3: Verify + PR

Run the full verification matrix (see ground rules). Then dispatch a final whole-branch code review (BASE = merge-base with main, HEAD = branch tip). Then push and open the PR:

- Title: `fix: Flash All no longer flashes seeded, unreviewed configs`
- Body: summary of the bypass + the skip behavior; test plan listing the matrix results.

---

# PR-2: First-flash UX — honest safety gate + prefix-agnostic MCU-name match + docs

> ✅ **Done** — shipped on `feat/first-flash-ux`, opened as [#5](https://github.com/YanceyA/kflash/pull/5).

**Branch:** `feat/first-flash-ux` (off `main`; independent of PR #3 and PR-1)

**Context:** In the standard first-flash setup order the new board is already referenced in printer.cfg, so Klippy sits in startup/error and Moonraker's `print_stats` query fails. `get_print_status()` (`kflash/moonraker.py:45-66`) returns `None`, and `moonraker_safety_gate` (`kflash/flash_steps.py:63-120`) then claims "Moonraker unreachable" with a default-No confirm — misleading at exactly the moment the first-flash skip is meant to shine. Separately, `match_serial_to_mcu_name` (`kflash/moonraker.py:247-264`) is the only matcher in the codebase that is not prefix-agnostic, so a device registered while in Katapult mode (pattern `usb-katapult_…*`) never auto-matches printer.cfg's `usb-Klipper_…` serial.

**Settled decisions:**
- New `moonraker.get_klippy_state()` queries `GET {MOONRAKER_URL}/server/info` and returns `data["result"]["klippy_state"]` (Moonraker reports `"ready" | "startup" | "shutdown" | "error" | "disconnected"`), or `None` on any error — same exception envelope and `TIMEOUT` as `get_print_status`.
- In the gate's `print_status is None` branch: if `get_klippy_state()` returns a state, Moonraker is up but Klippy isn't ready → new message + confirm id `"klippy_not_ready"` with **default=True** (no print can be running when Klippy isn't ready, so proceeding is safe; the prompt stays visible). If it returns `None`, Moonraker is genuinely unreachable → existing `"no_moonraker"` prompt, default False, unchanged.
- `match_serial_to_mcu_name` runs the pattern through `discovery.prefix_variants`. Import `from .discovery import prefix_variants` at the top of `moonraker.py` — no cycle (`discovery` does not import `moonraker`).

### Task 2.1: `get_klippy_state()`

**Files:**
- Modify: `kflash/moonraker.py` (new function directly after `get_print_status`, ~line 67)
- Test: `tests/test_moonraker.py`

**Step 1: Failing test** — follow `tests/test_moonraker.py`'s existing style for stubbing HTTP (inspect the file first; if it only tests pure parsers, monkeypatch `moonraker.urlopen` with a `io.BytesIO`-backed context manager):

```python
def test_get_klippy_state_parses_server_info(monkeypatch):
    payload = json.dumps({"result": {"klippy_state": "startup"}}).encode()

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def read(self):
            return payload

    monkeypatch.setattr(moonraker, "urlopen", lambda url, timeout: _Resp())
    assert moonraker.get_klippy_state() == "startup"


def test_get_klippy_state_none_when_unreachable(monkeypatch):
    def _raise(url, timeout):
        raise URLError("down")
    monkeypatch.setattr(moonraker, "urlopen", _raise)
    assert moonraker.get_klippy_state() is None
```

**Step 2:** run, expect `AttributeError` (no such function).

**Step 3: Implement**

```python
def get_klippy_state() -> Optional[str]:
    """Query Moonraker for the Klippy host state.

    Returns "ready"/"startup"/"shutdown"/"error"/"disconnected", or None if
    Moonraker itself is unreachable. Distinguishes "Moonraker down" from
    "Moonraker up but Klippy not ready" (e.g. a board awaiting its first
    flash referenced in printer.cfg).
    """
    try:
        url = f"{MOONRAKER_URL}/server/info"
        with urlopen(url, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        state = data["result"].get("klippy_state")
        return str(state) if state else None
    except (URLError, HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        return None
```

**Step 4:** run test file, all pass. **Step 5:** commit `feat(moonraker): add get_klippy_state() server-info query`.

### Task 2.2: Klippy-state-aware safety gate

**Files:**
- Modify: `kflash/flash_steps.py` (import ~line 40; `moonraker_safety_gate` `print_status is None` branch, ~lines 74-85)
- Test: `tests/test_safety_gate.py`

**Step 1: Failing tests.** IMPORTANT: the existing `_patch_status` helper (`test_safety_gate.py:37`) must also patch the new collaborator or every existing "moonraker down" test would hit the network. Update it:

```python
def _patch_status(monkeypatch, status, klippy_state=None):
    monkeypatch.setattr(flash_steps, "get_print_status", lambda: status)
    monkeypatch.setattr(flash_steps, "get_klippy_state", lambda: klippy_state)
```

New tests:

```python
@pytest.mark.parametrize(
    "proceed,expected",
    [(True, SafetyGate.PROCEED), (False, SafetyGate.CANCELLED)],
)
def test_klippy_not_ready_prompts_with_yes_default(monkeypatch, em, proceed, expected):
    _patch_status(monkeypatch, None, klippy_state="startup")
    decider = _Confirmer(proceed)
    result = moonraker_safety_gate(em=em, decider=decider, label="Flash")
    assert result is expected
    assert decider.seen == ["klippy_not_ready"]


def test_klippy_not_ready_confirm_default_is_true(monkeypatch, em):
    _patch_status(monkeypatch, None, klippy_state="error")

    captured = []

    class _Recorder:
        def confirm(self, req):
            captured.append(req)
            return req.default

    result = moonraker_safety_gate(em=em, decider=_Recorder(), label="Flash")
    assert result is SafetyGate.PROCEED  # default True proceeds
    assert captured[0].id == "klippy_not_ready"
    assert captured[0].default is True


def test_moonraker_truly_down_keeps_no_moonraker_prompt(monkeypatch, em):
    _patch_status(monkeypatch, None, klippy_state=None)
    decider = _Confirmer(False)
    result = moonraker_safety_gate(em=em, decider=decider, label="Flash")
    assert result is SafetyGate.CANCELLED
    assert decider.seen == ["no_moonraker"]
```

**Step 2:** run; new tests fail (`get_klippy_state` not imported / `no_moonraker` asked instead).

**Step 3: Implement.** Extend the moonraker import in `flash_steps.py`:

```python
from .moonraker import detect_firmware_flavor, get_klippy_state, get_print_status
```

Replace the `print_status is None` branch body in `moonraker_safety_gate`:

```python
    if print_status is None:
        klippy_state = get_klippy_state()
        if klippy_state is not None:
            # Moonraker is up; Klippy just isn't ready. No print can be
            # running in this state, so the default flips to proceed --
            # this is the normal state when a board referenced in
            # printer.cfg is awaiting its first flash.
            em.warn(
                f"Klipper reports state '{klippy_state}' - print status and"
                " version check unavailable (normal if a board is awaiting"
                " its first flash)"
            )
            if not decider.confirm(
                ConfirmDecision(
                    id="klippy_not_ready",
                    message="Continue with flash?",
                    default=True,
                )
            ):
                em.phase(label, "Cancelled")
                return SafetyGate.CANCELLED
            return SafetyGate.PROCEED
        em.warn("Moonraker unreachable - print status and version check unavailable")
        if not decider.confirm(
            ConfirmDecision(
                id="no_moonraker",
                message="Continue without safety checks?",
                default=False,
            )
        ):
            em.phase(label, "Cancelled")
            return SafetyGate.CANCELLED
        return SafetyGate.PROCEED
```

**Step 4:** `./.venv/Scripts/python.exe -m pytest tests/test_safety_gate.py tests/test_flash_steps.py tests/test_command_flash_batch.py -q` — all pass (the gate is shared by single and batch; batch tests stub the whole gate, but run them anyway).

**Step 5:** commit `feat(safety): distinguish 'Klippy not ready' from 'Moonraker unreachable' in flash gate`.

### Task 2.3: Prefix-agnostic `match_serial_to_mcu_name`

**Files:**
- Modify: `kflash/moonraker.py` (~lines 247-264 + import)
- Test: `tests/test_moonraker.py`

**Step 1: Failing tests:**

```python
def test_match_serial_to_mcu_name_matches_across_prefixes():
    # Device registered while in Katapult mode: pattern has the katapult
    # prefix, printer.cfg records the Klipper serial path.
    mcu_serials = {
        "mcu": "/dev/serial/by-id/usb-Klipper_rp2040_45474E621A858C5A-if00",
    }
    assert (
        moonraker.match_serial_to_mcu_name("usb-katapult_rp2040_45474E621A858C5A*", mcu_serials)
        == "mcu"
    )


def test_match_serial_to_mcu_name_reverse_prefix_direction():
    mcu_serials = {
        "mcu hbb": "/dev/serial/by-id/usb-katapult_rp2040_ABC-if00",
    }
    assert (
        moonraker.match_serial_to_mcu_name("usb-Klipper_rp2040_ABC*", mcu_serials)
        == "mcu hbb"
    )
```

**Step 2:** run, both fail (returns None).

**Step 3: Implement.** Add `from .discovery import prefix_variants` to `moonraker.py` imports, and change the match loop:

```python
    variants = prefix_variants(pattern)
    for mcu_name, serial_path in mcu_serials.items():
        if not serial_path:
            continue
        filename = serial_path.rsplit("/", 1)[-1] if "/" in serial_path else serial_path
        if any(fnmatch.fnmatchcase(filename, v) for v in variants):
            return mcu_name
    return None
```

**Step 4:** run `tests/test_moonraker.py` + `tests/test_command_device_add.py` (the wizard consumes this helper) — all pass.

**Step 5:** commit `fix(moonraker): make MCU-name serial match prefix-agnostic`.

### Task 2.4: Docs — what kflash can target

**Files:**
- Modify: `README.md` — in `## Requirements` (or directly under `## Flash Workflow`, implementer's judgment on best fit): one short paragraph — kflash flashes boards that enumerate in `/dev/serial/by-id/` as `Klipper_`/`katapult_` USB devices or as Katapult CAN nodes; a board already sitting in the Katapult bootloader is flashed directly, but *installing Katapult itself the first time* (or recovering a board with neither firmware, e.g. raw DFU/BOOTSEL) is a manual step outside kflash.
- Modify: `docs/flashing.md` — equivalent note in its matching section, in that file's tone.
- Modify (on-disk only, gitignored): `CLAUDE.md` — add "Installing Katapult / raw-DFU recovery" to the `## Out of Scope` list.

Commit: `docs: state kflash's target scope (Klipper/Katapult devices; Katapult install is manual)`

### Task 2.5: Verify + PR

Full verification matrix, final whole-branch review, push, open PR:
- Title: `feat: first-flash UX — honest safety gate, prefix-agnostic MCU-name match, scope docs`
- Body: three bullets (gate message + default, prefix fix, docs); test plan with matrix results.

---

# PR-3: Dashboard — surface bootloader state and CAN "not in printer.cfg"

> ⏳ **Ready to start once its dependency merges.** Not started — it imports `discovery.is_katapult_device` from PR #3 (`fix/katapult-first-flash`, open as [#3](https://github.com/YanceyA/kflash/pull/3)), which is not yet on `main`. Once #3 merges, branch off updated `main` and follow this section unchanged.

**Branch:** `feat/dashboard-bootloader-status` (off `main` **after PR #3 `fix/katapult-first-flash` is merged** — this PR imports `discovery.is_katapult_device` from it. Verify with `git log main --oneline | head` that commit "fix(flash): flash directly when target is already in Katapult bootloader" is present before starting.)

**Context:** A registered board sitting in the Katapult bootloader renders as a green `connected` with version `-` (`kflash/ui/screens/dashboard.py:_row_cells`, ~line 793) — nothing tells the user it's in the bootloader and ready for its first flash. And for CAN devices, "connected" actually means "canbus_uuid present in Moonraker's printer.cfg map" (`build_dashboard_devices`, ~line 339), so a first-flash CAN node that isn't in printer.cfg yet shows a misleading `offline`.

**Settled decisions:**
- New `DeviceRow` fields: `in_bootloader: bool = False` (USB rows: live matched filename is katapult-prefixed) and `can_not_in_config: bool = False` (CAN rows: Moonraker reachable but UUID absent from the map).
- Conn cell precedence: `new` → `offline` → `excluded` → **`katapult`** (orange, for `in_bootloader`) → `connected`. The label is `katapult`, NOT "bootloader" — the Conn column is 9 chars wide (`_COLUMNS` ~line 565) and "bootloader" would ellipsize; "katapult" fits and matches the terminology in device names.
- CAN rows with `can_not_in_config`: Conn cell shows `no cfg` (orange); the row stays `connected=False` so Flash All's connected-only count is **unchanged** (a node Klipper can't see shouldn't batch-flash by default; single-device F already works engine-side).
- Flash-All count and all engine behavior untouched — this PR is display-only.

### Task 3.1: `DeviceRow` fields + row builder

**Files:**
- Modify: `kflash/ui/screens/dashboard.py` — `DeviceRow` dataclass (~line 190), `build_dashboard_devices` USB loop (~lines 300-333) and CAN loop (~lines 336-367), the `from ...discovery import (...)` block (~line 46: add `is_katapult_device`)
- Test: `tests/ui/test_dashboard.py`

**Step 1: Failing tests.** Follow the file's conventions: `_write_registry(tmp_path)`, `_fake_usb()`, the autouse `_stub_engine_reads` fixture, `app.run_test()` + `pilot.pause()`, assertions on `screen._rows`. The registry fixture's `octopus` pattern is `usb-Klipper_stm32h723xx_ABC*`.

```python
def test_bootloader_mode_device_row_flagged_and_labeled(tmp_path, monkeypatch) -> None:
    """A registered device currently enumerated as usb-katapult_* is connected
    but flagged in_bootloader, and its Conn cell renders 'katapult'."""
    registry = _write_registry(tmp_path)
    monkeypatch.setattr(
        dash,
        "scan_serial_devices",
        lambda: [
            DiscoveredDevice(
                path="/dev/serial/by-id/usb-katapult_stm32h723xx_ABC123-if00",
                filename="usb-katapult_stm32h723xx_ABC123-if00",
            )
        ],
    )

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            octopus = next(r for r in screen._rows if r.key == "octopus")
            assert octopus.connected is True
            assert octopus.in_bootloader is True
            cells = screen._row_cells(octopus, None)
            assert str(cells[3]) == "katapult"

    _run(go())


def test_klipper_mode_device_row_not_flagged(tmp_path) -> None:
    registry = _write_registry(tmp_path)

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            octopus = next(r for r in screen._rows if r.key == "octopus")
            assert octopus.in_bootloader is False
            cells = screen._row_cells(octopus, None)
            assert str(cells[3]) == "connected"

    _run(go())
```

For the CAN case, add a CAN device to a copy of the registry dict and drive `get_mcu_canbus_map`:

```python
def test_can_device_not_in_printer_cfg_shows_no_cfg(tmp_path, monkeypatch) -> None:
    reg = json.loads(json.dumps(_REGISTRY))
    reg["devices"]["nhk"] = {
        "name": "Nitehawk", "mcu": "rp2040", "serial_pattern": None,
        "bootloader_method": "can", "flash_command": "katapult_can",
        "canbus_uuid": "aabbccddeeff", "canbus_interface": "can0",
        "flashable": True,
    }
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    registry = Registry(str(path))
    # Moonraker reachable, but this UUID is not in printer.cfg.
    monkeypatch.setattr(dash, "get_mcu_canbus_map", lambda: {"otheruuid0000": "mcu x"})

    async def go() -> None:
        app = KflashApp(registry)
        async with app.run_test(size=_SIZE) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app._dashboard
            nhk = next(r for r in screen._rows if r.key == "nhk")
            assert nhk.connected is False
            assert nhk.can_not_in_config is True
            cells = screen._row_cells(nhk, None)
            assert str(cells[3]) == "no cfg"

    _run(go())
```

(Check `Registry`'s registry-dict key requirements against `_REGISTRY` — device dicts there omit `key`; mirror exactly what the existing fixture does. If `str(cells[3])` doesn't yield the plain text of a Rich `Text`, use `cells[3].plain`.)

**Step 2:** run `python -m pytest tests/ui/test_dashboard.py -q` **on the Pi** (local venv lacks snapshot deps): new tests fail (`in_bootloader` attribute missing).

**Step 3: Implement.**

(a) `DeviceRow`: add after `seed_source`:

```python
    in_bootloader: bool = False  # USB rows: live filename is usb-katapult_*
    can_not_in_config: bool = False  # CAN rows: reachable map lacks this UUID
```

(b) Import: add `is_katapult_device` to the `from ...discovery import (...)` block.

(c) USB loop in `build_dashboard_devices` — after `connected = len(matches) > 0`:

```python
        in_bootloader = connected and is_katapult_device(matches[0].filename)
```

and pass `in_bootloader=in_bootloader` in the `DeviceRow(...)` construction.

(d) CAN loop — where `connected` is computed from `can_status_map`:

```python
        if can_status_map is not None:
            connected = entry.canbus_uuid in can_status_map
            can_not_in_config = not connected
        else:
            connected = True  # Moonraker unreachable: graceful default
            can_not_in_config = False
```

and pass `can_not_in_config=can_not_in_config` in that `DeviceRow(...)`.

(e) `_row_cells` Conn precedence — replace the current chain:

```python
        if row.group == "new":
            conn = cell("new", COLORS["orange"])
        elif not row.connected:
            if row.can_not_in_config:
                conn = cell("no cfg", COLORS["orange"])
            else:
                conn = cell("offline", COLORS["subtle"])
        elif not row.flashable:
            conn = cell("excluded", COLORS["orange"])
        elif row.in_bootloader:
            conn = cell("katapult", COLORS["orange"])
        else:
            conn = cell("connected", COLORS["green"])
```

**Step 4:** Pi run of `tests/ui/` + full suite. Snapshot tests may produce diffs ONLY if a snapshot scenario includes a katapult-prefixed or unconfigured-CAN device (none do today) — if any snapshot fails, inspect `snapshot_report.html` before accepting anything.

**Step 5:** commit `feat(dashboard): show 'katapult' bootloader state and CAN 'no cfg' in Conn column`.

### Task 3.2: Docs

**Files:**
- Modify: `docs/flashing.md` — CAN section: note that dashboard CAN "connected" reflects presence in printer.cfg via Moonraker; a registered node not yet in printer.cfg shows `no cfg` and is excluded from Flash All, but single-device flash works.
- Modify (on-disk only, gitignored): `CLAUDE.md` — TUI (Dashboard) section: document the `katapult` and `no cfg` Conn states.

Commit (docs/flashing.md only): `docs: document dashboard katapult / no-cfg connection states`

### Task 3.3: Verify + PR

Full verification matrix (Pi run is mandatory here — this PR is UI). Final whole-branch review. Push, open PR:
- Title: `feat: dashboard surfaces bootloader-mode and CAN not-in-config states`
- Body: the two display changes + explicit "engine behavior and Flash All selection unchanged"; test plan with matrix results.

---

## Sequencing summary for the orchestrator

1. ✅ **PR-1** (shipped on `claude/first-flash-followups-pr1-uvm6rs`, [#4](https://github.com/YanceyA/kflash/pull/4)) — done, off `main`. Highest priority.
2. ✅ **PR-2** (`feat/first-flash-ux`, [#5](https://github.com/YanceyA/kflash/pull/5)) — done, off `main`, independent.
3. ⏳ **PR-3** (`feat/dashboard-bootloader-status`) — **remaining.** Start only after PR #3 (`fix/katapult-first-flash`, [#3](https://github.com/YanceyA/kflash/pull/3)) is merged to `main`; rebase/branch from updated `main`.

Each PR: subagent-driven-development (implementer → spec review → quality review per task), full verification matrix, final whole-branch review, then push + API-based PR creation. Hardware smoke tests (bare-Katapult HBB flash; CAN behaviors) remain user-gated — offer, never run unprompted.
