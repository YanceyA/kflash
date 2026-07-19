# Katapult First-Flash (Already-in-Bootloader Skip) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When the flash target is already presenting as a Katapult bootloader USB device (`usb-katapult_*`), skip bootloader entry and flash it directly, so the first flash of a bare-Katapult board succeeds without manual `flashtool.py` invocations.

**Architecture:** Add a one-line prefix classifier `is_katapult_device()` to `kflash/discovery.py` (next to the existing `SUPPORTED_PREFIXES`), and add an `elif already_in_bootloader:` branch to `run_flash_sequence` in `kflash/flash_steps.py`, as a sibling of the existing `bootloader_method == "none"` skip. No changes to `bootloader.py`, verify, or the CAN path.

**Tech Stack:** Python 3.9+ stdlib engine code; pytest with the existing `FakeRunner` / `RecordingSink` / `FakeDecisionProvider` seams from `tests/conftest.py`.

---

## Design decisions (answers to the open questions)

These were resolved by reading the code and the Katapult `flashtool.py` source on the Pi. Do not re-litigate them during execution; they are the spec.

1. **Detection = resolved live device path prefix.** Both `cmd_flash` (`kflash/commands/flash_single.py`) and the batch command resolve `device_path` from a *live* scan with prefix-agnostic matching (`discovery.prefix_variants`), and they error out earlier (`device_not_connected`) when the board is absent. So by the time `run_flash_sequence` runs, `device_path` is a real, currently-present symlink. A `usb-katapult_` basename there is authoritative: the board is in the Katapult bootloader *right now*. This also answers the disambiguation question: a genuinely offline device never reaches the skip — it still fails in Discovery with the existing clear error. Do NOT use the registry's stored `serial_pattern` for detection (it records whichever mode the board was in when registered, not its current state).

2. **The skip lives in `run_flash_sequence`, not in the `_enter_*` handlers.** One site covers both the single and batch flash commands. The `enter_bootloader` retry prompt stays intact for real entry failures (it simply never fires when entry is skipped), the handlers stay pure "perform entry" primitives, and the sequence can emit a proper `step_end` explaining the skip (handlers have no `Emitter`). This mirrors the existing `bootloader_method == "none"` branch exactly.

3. **CAN needs no change.** Verified in `~/katapult/scripts/flashtool.py` on the Pi: `_jump_to_bootloader` (line ~692) is fire-and-forget — it sends one CAN frame and requires no response, so `flashtool.py -r -u <uuid>` exits 0 even when the node is already in Katapult. `_enter_can` in `kflash/bootloader.py` only checks the return code (it never polls for re-enumeration — that poll is the failing step in the USB path), and `flashtool.py -f` connects to a bootloader node directly. A bare-bootloader CAN node already flashes fine.

4. **Skip condition:** USB device (`not is_can`, `device_path` present), basename starts with `usb-katapult_` (case-insensitive), `bootloader_method in ("usb", "serial", "manual")`, and `flash_command != "uf2_mount"`. Rationale:
   - `usb` and `serial` are the broken paths (entry request is a no-op with no running Klipper; `_poll_for_reenumeration` Phase 1 then times out waiting for disappearance).
   - `manual` has the identical failure (user presses Enter, device never disappears), and if the device already presents as Katapult the "put the board in bootloader mode" prompt is pointless — skip it too.
   - `uf2_mount` is excluded: UF2 flashing needs BOOTSEL mass-storage mode, and the manual prompt is precisely what tells the user to enter it. A Katapult serial path is not a flashable state for UF2.
   - `none` already skips; `flash_sdcard` only pairs with `none` (see `validation.FLASH_METHOD_TABLE`), so it is unreachable here.
   - Both `katapult` and `make_flash` flash commands handle a Katapult serial device directly (Klipper's `flash_usb.py` has native CanBoot/Katapult support), so no per-command gating beyond the UF2 exclusion is needed.

5. **Post-flash verify is already correct — no change.** `discovery.wait_for_device` matches via `prefix_variants` and only succeeds on a `usb-klipper_` prefix, treating a still-`katapult_` device as "keep polling", with a distinct "Device in bootloader mode (katapult)" timeout error. The `katapult_` → `Klipper_` transition after a direct flash is handled.

---

### Task 0: Create a feature branch

**Step 1: Branch off main**

```bash
git checkout -b fix/katapult-first-flash
```

Expected: `Switched to a new branch 'fix/katapult-first-flash'`

---

### Task 1: `is_katapult_device()` classifier in discovery

**Files:**
- Modify: `kflash/discovery.py` (add function directly below `is_supported_device`, ~line 60)
- Test: `tests/test_discovery.py`

**Step 1: Write the failing test**

Append to `tests/test_discovery.py` (match the file's existing test style — plain functions, no class required):

```python
def test_is_katapult_device_true_for_katapult_prefix():
    assert discovery.is_katapult_device("usb-katapult_rp2040_45474E621A858C5A-if00")


def test_is_katapult_device_case_insensitive():
    assert discovery.is_katapult_device("usb-Katapult_rp2040_ABC123-if00")


def test_is_katapult_device_false_for_klipper_and_foreign_devices():
    assert not discovery.is_katapult_device("usb-Klipper_rp2040_ABC123-if00")
    assert not discovery.is_katapult_device("usb-Beacon_Beacon_RevH_FC2-if00")
    assert not discovery.is_katapult_device("")
```

Note: check how `tests/test_discovery.py` imports discovery (module vs names) and follow suit; if it imports names, add `is_katapult_device` to that import instead of using `discovery.`.

**Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_discovery.py -q -k katapult_device`
Expected: FAIL / ERROR with `AttributeError: module 'kflash.discovery' has no attribute 'is_katapult_device'`

**Step 3: Write the minimal implementation**

In `kflash/discovery.py`, directly after `is_supported_device` (~line 59):

```python
def is_katapult_device(filename: str) -> bool:
    """Return True if filename is a device presenting the Katapult bootloader."""
    return filename.lower().startswith("usb-katapult_")
```

**Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_discovery.py -q`
Expected: all pass

**Step 5: Commit**

```bash
git add kflash/discovery.py tests/test_discovery.py
git commit -m "feat(discovery): add is_katapult_device() bootloader-prefix classifier"
```

---

### Task 2: Already-in-bootloader skip in `run_flash_sequence`

**Files:**
- Modify: `kflash/flash_steps.py` (import ~line 35; bootloader phase ~lines 519-582)
- Test: `tests/test_flash_steps.py`

**Step 1: Write the failing tests**

Add to `tests/test_flash_steps.py`. Reuse the existing fixtures/helpers (`em`, `toolchain`, `_reappear`, `_usb_entry`, `FakeRunner`, `RecordingSink`, `FakeDecisionProvider`). Add near the existing constants:

```python
KATAPULT_SERIAL = "usb-katapult_stm32h723xx_ABC123-if00"
```

Add `BootloaderResult` to the models import at the top of the file:

```python
from kflash.models import BootloaderResult, DeviceEntry, DiscoveredDevice, GlobalConfig
```

Then the tests (new section at the end of the file):

```python
# ---------------------------------------------------------------------------
# Already-in-bootloader first-flash skip (bare Katapult device)
# ---------------------------------------------------------------------------


def _forbid_enter_bootloader(monkeypatch):
    def _fail(**kwargs):
        raise AssertionError("enter_bootloader must not be called")

    monkeypatch.setattr(flash_steps, "enter_bootloader", _fail)


def test_usb_katapult_device_skips_bootloader_entry(monkeypatch, em, toolchain):
    """A usb-katapult_* target flashes directly: no entry, flash gets the
    katapult path, verify still requires the Klipper prefix to return."""
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    _reappear(monkeypatch)  # reappears as usb-Klipper_* -> verify passes
    _forbid_enter_bootloader(monkeypatch)

    step = run_flash_sequence(
        entry=_usb_entry(bootloader_method="usb"),
        device_path=f"/dev/serial/by-id/{KATAPULT_SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
        verify_timeout=2.0,
    )

    assert step.bootloader_ok
    assert step.success
    # The flash subprocess was pointed at the katapult device path
    flash_calls = [argv for mode, argv in fake.calls if mode == "stream_lines"]
    assert any(KATAPULT_SERIAL in " ".join(argv) for argv in flash_calls)


def test_serial_katapult_device_skips_bootloader_entry(monkeypatch, toolchain):
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    _reappear(monkeypatch)
    _forbid_enter_bootloader(monkeypatch)
    sink = RecordingSink()

    step = run_flash_sequence(
        entry=_usb_entry(bootloader_method="serial"),
        device_path=f"/dev/serial/by-id/{KATAPULT_SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=Emitter(sink),
        decider=HeadlessDecisionProvider(),
        verify_timeout=2.0,
    )

    assert step.success
    assert "Already in Katapult bootloader" in sink.text()


def test_manual_katapult_device_skips_ready_prompt(monkeypatch, em, toolchain):
    """manual + katapult with the board already in the bootloader: no
    'press Enter' gate, flash proceeds directly."""
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    _reappear(monkeypatch)
    decider = FakeDecisionProvider()

    step = run_flash_sequence(
        entry=_usb_entry(bootloader_method="manual"),
        device_path=f"/dev/serial/by-id/{KATAPULT_SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=decider,
        verify_timeout=2.0,
    )

    assert step.success
    assert decider.manual_calls == []


def test_uf2_manual_katapult_device_still_prompts(monkeypatch, em, toolchain):
    """UF2 flashing needs BOOTSEL mass-storage mode, so a katapult serial path
    must NOT short-circuit the manual bootloader prompt."""
    calls = []

    def _fake_enter(**kwargs):
        calls.append(kwargs)
        return BootloaderResult(success=False, error_message="stub entry failure")

    monkeypatch.setattr(flash_steps, "enter_bootloader", _fake_enter)

    entry = _usb_entry(flash_command="uf2_mount", bootloader_method="manual")
    step = run_flash_sequence(
        entry=entry,
        device_path=f"/dev/serial/by-id/{KATAPULT_SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=FakeDecisionProvider(),
        verify_timeout=2.0,
    )

    assert len(calls) == 1  # entry was attempted, not skipped
    assert not step.bootloader_ok


def test_klipper_device_still_enters_bootloader(monkeypatch, em, toolchain):
    """The normal running-Klipper path is unchanged: a usb-Klipper_* target
    still goes through enter_bootloader."""
    fake = FakeRunner(default=CommandResult(0))
    runner.set_runner(fake)
    _reappear(monkeypatch)
    calls = []

    def _fake_enter(**kwargs):
        calls.append(kwargs)
        return BootloaderResult(
            success=True, device_path=f"/dev/serial/by-id/{KATAPULT_SERIAL}"
        )

    monkeypatch.setattr(flash_steps, "enter_bootloader", _fake_enter)

    step = run_flash_sequence(
        entry=_usb_entry(bootloader_method="usb"),
        device_path=f"/dev/serial/by-id/{SERIAL}",
        firmware_path=toolchain["firmware"],
        config=toolchain["config"],
        klipper_dir=toolchain["config"].klipper_dir,
        katapult_dir=toolchain["katapult"],
        em=em,
        decider=HeadlessDecisionProvider(),
        verify_timeout=2.0,
    )

    assert len(calls) == 1
    assert step.success
```

**Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_flash_steps.py -q -k "katapult_device or still_enters or still_prompts"`
Expected: the three skip tests FAIL with `AssertionError: enter_bootloader must not be called` (or missing "Already in Katapult bootloader" text / non-empty `manual_calls`); the two control tests PASS (they exercise current behavior).

**Step 3: Write the implementation**

In `kflash/flash_steps.py`:

(a) Extend the discovery import (~line 35):

```python
from .discovery import is_katapult_device, verify_can_device_after_flash, wait_for_device
```

(b) In `run_flash_sequence`, replace the start of the bootloader phase (the `dkey = entry.key` line through `result.bootloader_ok = True` of the `"none"` branch, ~lines 523-527) with:

```python
    dkey = entry.key
    # First-flash bootstrap: a usb-katapult_* path means the board is already
    # sitting in the Katapult bootloader (no running Klipper to receive an
    # entry request, so entry/re-enumeration would time out) -- flash directly.
    # UF2 is excluded: it needs BOOTSEL mass-storage mode, which the manual
    # prompt is responsible for.
    already_in_bootloader = (
        not is_can
        and device_path is not None
        and entry.bootloader_method in ("usb", "serial", "manual")
        and entry.flash_command != "uf2_mount"
        and is_katapult_device(Path(device_path).name)
    )
    if entry.bootloader_method == "none":
        em.step_end("Bootloader", "Skipped (method: none)", device_key=dkey)
        boot_device_path: Optional[str] = device_path
        result.bootloader_ok = True
    elif already_in_bootloader:
        em.step_end(
            "Bootloader",
            f"Already in Katapult bootloader -- {_short_path(device_path)}",
            device_key=dkey,
        )
        boot_device_path = device_path
        result.bootloader_ok = True
    else:
```

The existing `else:` body (the `em.step_start(...)` batch/CAN branches through the `step_end("Bootloader", "Entered ...")` lines) is unchanged. No stagger delay in the skip branch — nothing rebooted (same as the `"none"` branch).

**Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_flash_steps.py -q`
Expected: all pass (new and pre-existing)

**Step 5: Commit**

```bash
git add kflash/flash_steps.py tests/test_flash_steps.py
git commit -m "fix(flash): flash directly when target is already in Katapult bootloader

A board with Katapult installed but no Klipper app (first flash, or a
previously aborted flash) enumerates as usb-katapult_* and has no
firmware to receive the bootloader-entry request, so _enter_usb's
disappearance poll always timed out. run_flash_sequence now recognizes
the katapult prefix on the resolved live device path and skips entry,
mirroring the bootloader_method=none branch. CAN needs no change:
flashtool -r is fire-and-forget over CAN and _enter_can never polls."
```

---

### Task 3: Documentation

**Files:**
- Modify: `CLAUDE.md` (Flash Workflow section, phase 4 bullet)
- Modify: `README.md` (Flash Workflow section, ~line 91)
- Check: `docs/flashing.md` — if it describes the bootloader-entry step, add the same note there.

**Step 1: CLAUDE.md**

In the `## Flash Workflow (4 Phases)` section, phase 4 currently begins "**[Flash]** -- Stop Klipper, MCU cross-check, flash via Katapult...". After the **CAN flash specifics** paragraph, add:

```markdown
**Already-in-bootloader skip:** if the resolved USB target already presents as `usb-katapult_*` (Katapult installed but no Klipper app -- first flash, or a previously aborted flash), `run_flash_sequence` skips bootloader entry and flashes directly (`discovery.is_katapult_device`). Applies to `usb`/`serial`/`manual` methods, never `uf2_mount` (UF2 needs BOOTSEL mode, which the manual prompt handles). CAN needs no skip: `flashtool.py -r` is fire-and-forget over CAN and `_enter_can` never polls for re-enumeration.
```

**Step 2: README.md**

In `## Flash Workflow`, after the numbered list (line 91), add:

```markdown
A board already sitting in the Katapult bootloader (fresh Katapult install,
no Klipper app yet) is flashed directly — bootloader entry is skipped, so
the first flash works end-to-end.
```

**Step 3: docs/flashing.md**

Read it; if it documents bootloader entry, add an equivalent short note in the matching section. If it doesn't cover entry mechanics, skip.

**Step 4: Commit**

```bash
git add CLAUDE.md README.md docs/flashing.md
git commit -m "docs: document already-in-bootloader first-flash skip"
```

---

### Task 4: Full verification

**Step 1: Full local test suite + linters**

```bash
python -m pytest --ignore=tests/test_worker_thread_signals.py -q
ruff check .
mypy kflash
```

Expected: all pass, no new warnings. (POSIX-only tests auto-skip on Windows; the ignore matches the review matrix in CLAUDE.md.)

**Step 2: Run the suite on the Pi**

```bash
tar czf - --exclude='__pycache__' kflash tests pyproject.toml | ssh yanceya@192.168.50.50 "cd ~/kflash && tar xzf -"
ssh yanceya@192.168.50.50 "cd ~/kflash && .venv/bin/python3 -m pytest tests/ -q"
```

Expected: all pass.

**Step 3: Optional hardware smoke test (user-gated)**

The BTT HBB in bare-Katapult state (`usb-katapult_rp2040_45474E621A858C5A-if00`) is the real reproduction. Do NOT flash hardware autonomously — offer it to the user: run `kflash` on the Pi, flash the HBB, confirm the log shows "Already in Katapult bootloader" and the device verifies as `usb-Klipper_rp2040_*`.

**Step 4: Finish**

Use superpowers:finishing-a-development-branch (merge/PR decision belongs to the user).
