# Verification Plan

A feature-by-feature verification plan for `tt_um_jjy_spi_watchdog`, derived from
[docs/info.md](info.md) and the RTL in [src/](../src/).

## How to read this

Every row is one testable claim taken from the datasheet or from an explicit
design decision in the RTL. The **Status** column tracks whether the existing
cocotb suite in [test/test.py](../test/test.py) actually exercises that claim:

| Mark | Meaning |
| --- | --- |
| PASS | Covered by a test that would fail if the feature broke |
| PART | Touched, but the assertion does not pin the feature down |
| TODO | Not covered — no test would fail if this broke |
| WAIVE | Deliberately not tested; justification given |

`PART` and `TODO` rows are the work list.

**Line coverage is not the metric here.** `spi_regs.v` already reaches 100%
line/toggle coverage under Verilator while roughly half the rows below are
unverified — a line executing once says nothing about whether the scenarios
that matter were reached. Functional coverage is what this document measures.

## Two levels

[test.py](../test/test.py) drives the whole chip through its package pins, so
the same tests also run against the gate level netlist. It can only infer a
register write from its side effects.

[test_spi.py](../test/test_spi.py) instantiates `spi_regs` alone via
[tb_spi.v](../test/tb_spi.v). That exposes `wr_en` / `wr_addr` / `wr_data`
directly and lets a test drive `rd_data` with any pattern, which is what makes
A4, A6 and A9 checkable at all. There is no netlist for a submodule, so it is
RTL only. Group A lives here; groups B through I need the full chip.

## Running the suite

```bash
make                          # Icarus, functional run
make -f Makefile.cov          # Verilator, collects coverage.dat
make -f Makefile.cov cov-report   # annotate and list uncovered points
```

---

## A. SPI protocol

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| A1 | Mode 0: MOSI is sampled on the rising edge of SCLK | [spi_regs.v:88](../src/spi_regs.v#L88) | [test_spi.py](../test/test_spi.py) `test_a1_mosi_sampled_on_rising_edge` | PASS |
| A2 | Mode 0: MISO updates on the falling edge of SCLK | [spi_regs.v:89](../src/spi_regs.v#L89) | [test_spi.py](../test/test_spi.py) `test_a2_miso_updates_on_falling_edge` | PASS |
| A3 | Frame is 10 bits, MSB first, `[R/W][ADDR:2][DATA:7]` | [spi_regs.v:56](../src/spi_regs.v#L56) | [test_spi.py](../test/test_spi.py) `test_a3_frame_layout_and_direction` | PASS |
| A4 | A frame commits only at exactly 10 bits | [spi_regs.v:109](../src/spi_regs.v#L109) | [test_spi.py](../test/test_spi.py) `test_a4_frame_length_enforced` | PASS |
| A5 | The bit counter saturates rather than wrapping | [spi_regs.v:103](../src/spi_regs.v#L103) | folded into A4's length sweep | PASS |
| A6 | On a read, MISO is 0 during the R/W and ADDR bits | [spi_regs.v:144-150](../src/spi_regs.v#L144-L150) | [test_spi.py](../test/test_spi.py) `test_a6_miso_zero_during_rw_and_addr` | PASS |
| A7 | SCLK edges while CS_N is high are ignored | [spi_regs.v:86](../src/spi_regs.v#L86) | [test_spi.py](../test/test_spi.py) `test_a7_sclk_ignored_while_cs_high` | PASS |
| A8 | SCLK / CS_N are asynchronous and synchronised before use | [spi_regs.v:65-84](../src/spi_regs.v#L65-L84) | not simulatable — see J1 | WAIVE |
| A9 | R/W selects write (0) or read (1) | [spi_regs.v:111](../src/spi_regs.v#L111) | folded into A3 | PASS |
| A10 | rd_addr carries exactly the ADDR field, valid before the DATA bits shift out | [spi_regs.v:123-128](../src/spi_regs.v#L123-L128) | [test_spi.py](../test/test_spi.py) `test_a10_rd_addr_follows_addr_field` | PASS |

### A4 and A5 — one length sweep

A5 is not a separate scenario. `cnt` stops at `FRAME_BITS + 1` instead of
wrapping; without that guard a frame of `FRAME_BITS + 2^CNT_W` bits aliases
back onto a legal count and commits a garbage write. That is one more entry in
A4's list of illegal lengths, so the two are covered by a single test.

`test_a4_frame_length_enforced` in [test_spi.py](../test/test_spi.py) sweeps
0, 1, `FRAME_BITS-1`, `FRAME_BITS+1` and the aliasing length, asserting no
`wr_en` pulse at any address — a stronger claim than
`test_frame_length_enforced` in [test.py:214](../test/test.py#L214), which can
only observe that CTRL did not change and so stays silent if a write lands on
the wrong address.

The aliasing length is computed from `AW`/`DW` rather than hard-coded, so it
still lands on the wrap point in the sweep builds: 26 bits at the default
geometry, 28 at `AW=3 DW=8`.

The sweep ends with a correct-length frame as a positive control. Without it,
a helper that silently drove nothing at all would satisfy every preceding
assertion.

Verified by mutation: removing the saturating guard from
[spi_regs.v:103](../src/spi_regs.v#L103) makes the 26-bit case commit
`(0, 85)` and the test fail.

### A3 and A9 — one test

A9's two claims -- a write raises `wr_en`, a read raises none while returning
`rd_data` -- are exactly what A3's write and read halves already assert, so
they share a test. One corner was genuinely uncovered and is now checked
there: a *write* frame must not return data either.

### A6 — a real protocol claim

The datasheet promises MISO is 0 during the first `1 + AW` bit positions. A
master reading the full return word sees them. `read()` in
[test.py](../test/test.py) masks them off, and the watchdog can never return
an all-ones value anyway, so a design leaking stale `tx` content there would
pass at chip level. `read_raw()` at the unit level drives `rd_data` all-ones
and asserts on the whole word.

Verified by mutation: changing
[spi_regs.v:148](../src/spi_regs.v#L148) to ignore `rd_req` makes a write
frame return `0x07f` and the A3/A9 test fail.

---

## B. Register map

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| B1 | CTRL (addr 0) reads back `{2'b0, TIMEOUT, IRQ_EN, EN}` | [project.v:92](../src/project.v#L92) | [test.py:155](../test/test.py#L155) | PASS |
| B2 | CTRL bits 6:5 are unimplemented and read as 0 | [project.v:103](../src/project.v#L103) | constant `2'b00` in the readback | WAIVE |
| B3 | KICK (addr 1) is write-only and reads as 0 | [project.v:99](../src/project.v#L99) | [test.py:174](../test/test.py#L174) | PASS |
| B4 | STATUS (addr 2) reads `{5'b0, ARMED, IRQ_FLAG}` | [project.v:93](../src/project.v#L93) | many | PASS |
| B5 | Address 3 is unallocated: reads 0, writes ignored | [project.v:99](../src/project.v#L99) | read half covered at [test.py:175](../test/test.py#L175); write half undecodable | WAIVE |
| B6 | STATUS bit 1 (ARMED) is read-only; writes are ignored | [project.v:181](../src/project.v#L181) | [test.py](../test/test.py) `test_status_armed_is_read_only` | PASS |

### B2 — waived

`ctrl_rd` is `{2'b00, timeout_sel, irq_en, en}`, a literal constant in the
top two positions. There is no state behind those bits to get wrong.

### B5 — waived

The read half is covered at [test.py:175](../test/test.py#L175). The write
half cannot be observed: `spi_regs` raises `wr_en` for address 3 like any
other, and "ignored" only means [project.v:80-82](../src/project.v#L80-L82)
never decodes it. With no register there, a write leaves nothing to read back
and no side effect to detect. The unit-level tests already confirm the address
reaches `wr_addr` intact, which is the part that is actually checkable.

### B6 — ARMED is not writable

Only `wr_data[0]` participates in the W1C path, so writing `0x02` to STATUS
must leave ARMED alone in both directions: it cannot arm from IDLE, nor
disarm while counting. `test_status_armed_is_read_only` checks both, plus
that IRQ_FLAG beside it was not disturbed.

Verified by mutation: adding a `wr_status && wr_data[1]` branch that sets
`armed` makes the test fail with "writing ARMED armed it".

---

## C. Watchdog state machine

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| C1 | IDLE to COUNTING on KICK while EN=1 | [project.v:151-153](../src/project.v#L151-L153) | [test.py:191](../test/test.py#L191) | PASS |
| C2 | KICK in IDLE with EN=0 is ignored | [project.v:139](../src/project.v#L139) | [test.py:185](../test/test.py#L185) | PASS |
| C3 | KICK in COUNTING clears the counter, stays armed | [project.v:151-153](../src/project.v#L151-L153) | [test.py:361](../test/test.py#L361) | PASS |
| C4 | Timeout returns to IDLE and sets IRQ_FLAG | [project.v:154-157](../src/project.v#L154-L157) | [test.py:287](../test/test.py#L287) | PASS |
| C5 | Clearing EN returns to IDLE | [project.v:148-150](../src/project.v#L148-L150) | [test.py:264](../test/test.py#L264) | PASS |
| C6 | `rst_n` returns to IDLE from any state | [project.v:145-147](../src/project.v#L145-L147) | [test.py](../test/test.py) `test_reset_from_any_state` | PASS |
| C7 | Counter is held at 0 while EN=0 | [project.v:148-150](../src/project.v#L148-L150) | implied by C2 and C5 | WAIVE |
| C8 | Clearing EN disarms in the same cycle, with no stale ARMED readback | [project.v:137-138](../src/project.v#L137-L138) | not observable over SPI | WAIVE |

### C6 — reset from every state

`test_reset_from_any_state` applies reset from IDLE, from COUNTING, and from
IDLE with IRQ_FLAG already sticky. The third is the only path that exercises
reset clearing the flag, since a W1C write is otherwise the only way to clear
it.

Two things this test has to get right. Reset must be held for more than one
clk and allowed to settle after release — a single cycle does not propagate.
And reads must come *after* the release: while `rst_n` is low `spi_regs`
holds `rx` and `cnt` cleared, so no frame is received and `read()` returns 0
regardless of what the registers hold, making every assertion vacuously true.

Verified by mutation: changing
[project.v:167](../src/project.v#L167) so `irq_flag` survives reset makes the
third case fail.

### C7 — waived

Subsumed by C2 and C5. `armed` gates the counter's increment at
[project.v:157](../src/project.v#L157), and both those tests confirm `armed`
is low when EN is clear. A separate long wait would re-test the same gate.

### C8 — waived

`en_now` makes a CTRL write clearing EN disarm on the cycle it lands rather
than one later. There is no way to see this over SPI: the earliest possible
readback is a whole frame later, by which point both a same-cycle and a
next-cycle disarm read identically. Not testable through the pins, and the
pin-level constraint is deliberate — see [Two levels](#two-levels).

---

## D. Timeout selection

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| D1 | TIMEOUT=000 fires on counter bit `WD_BASE_EXP` | [project.v:138](../src/project.v#L138) | [test.py:268](../test/test.py#L268), [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D2 | TIMEOUT=001 fires on counter bit `WD_BASE_EXP + 1` | [project.v:139](../src/project.v#L139) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D3 | TIMEOUT=010 fires on counter bit `WD_BASE_EXP + 2` | [project.v:140](../src/project.v#L140) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D4 | TIMEOUT=011 fires on counter bit `WD_BASE_EXP + 3` | [project.v:141](../src/project.v#L141) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D5 | TIMEOUT=100 fires on counter bit `WD_BASE_EXP + 4` | [project.v:142](../src/project.v#L142) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D6 | TIMEOUT=101 fires on counter bit `WD_BASE_EXP + 6` | [project.v:143](../src/project.v#L143) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D7 | TIMEOUT=110 fires on counter bit `WD_BASE_EXP + 8` | [project.v:144](../src/project.v#L144) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |
| D8 | TIMEOUT=111 fires on counter bit `WD_BASE_EXP + 10` | [project.v:145](../src/project.v#L145) | [test.py](../test/test.py) `test_all_timeout_selections` | PASS |

### D1-D8 — every selection, timed

All eight TIMEOUT values were already *written and read back* by
`test_ctrl_readback`, and the `case` at
[project.v:136-146](../src/project.v#L136-L146) reaches full line coverage
because `always @(*)` re-evaluates every branch. Only `sel=0` had ever been
timed.

`test_all_timeout_selections` measures each window from the kick to the IRQ
and asserts it lands within 10% of `2**(WD_BASE_EXP + SEL_OFFSETS[sel])`.
The offsets are `0,1,2,3,4,6,8,10`, not a straight `0..7`: the top three
selections step by two so the range reaches ~5 s in silicon. Measured:
257, 513, 1025, 2049, 4097, 16385, 65537 and 262145 clocks against 256, 512,
1024, 2048, 4096, 16384, 65536 and 262144 — the extra cycle is the kick's own
latency.

`SEL_OFFSETS` in [test.py](../test/test.py) must stay in step with the `case`
in project.v. It is written out rather than computed, so a change to one
without the other shows up as a failed measurement instead of a silent pass.

The half-window check before each measurement is what separates one selection
from its neighbour; without it a window that fired early would still be inside
the tolerance of a longer one.

Verified by mutation: swapping the `3'd1` and `3'd2` branches fails this test,
while `test_ctrl_readback` and `test_timeout_fires` both still pass — which is
exactly the gap this row existed to close.

This is RTL only. Against the gate level build, where `WD_BASE_EXP` is 18,
TIMEOUT=111 would be 2^28 clocks and never finish.

## E. KICK sources

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| E1 | An SPI write of `0x5A` to addr 1 kicks | [project.v:104](../src/project.v#L104) | [test.py:210](../test/test.py#L210) | PASS |
| E2 | Any other value written to KICK is ignored | [project.v:104](../src/project.v#L104) | [test.py:207](../test/test.py#L207) tests `0x12`; exhaustive sweep not worth it | WAIVE |
| E3 | A rising edge on the KICK pin kicks | [project.v:76](../src/project.v#L76) | [test.py:197](../test/test.py#L197) | PASS |
| E4 | KICK is edge triggered, not level triggered | [project.v:76](../src/project.v#L76) | [test.py](../test/test.py) `test_kick_is_not_level_trigger` | PASS |
| E5 | The KICK pin is asynchronous and synchronised | [project.v:64-75](../src/project.v#L64-L75) | not simulatable — see J1 | WAIVE |
| E6 | A KICK after a timeout re-arms without clearing IRQ_FLAG | [project.v:179-182](../src/project.v#L179-L182) | [test.py:304](../test/test.py#L304) | PASS |

### E2 — waived

`0x12` is the only non-magic value tested. Near-miss values (`0x5B`, `0x58`,
`0x1A`) would catch a comparator built from too few bits, but
[project.v:104](../src/project.v#L104) is a full-width `==` against a
literal — there is no partial-decode structure for a sweep to find.

### E4 — the one that mattered most

If [project.v:76](../src/project.v#L76) were `wire kick_pin_evt = kick_s1;`,
turning the edge detector into a level detector, every other test would still
pass — `pin_kick()` at [test.py:103](../test/test.py#L103) always returns the
pin low, so level and edge behave identically. A watchdog that can be silenced
by tying one pin high is a serious failure for the part's purpose.

`test_kick_is_not_level_trigger` raises KICK and leaves it there: the first
edge arms the dog, and the timeout must fire anyway while the pin is held.

---

## F. Configuration locking

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| F1 | In IDLE the whole CTRL register is writable | [project.v:172-175](../src/project.v#L172-L175) | [test.py:251](../test/test.py#L251) | PASS |
| F2 | In COUNTING only EN lands; other bits are discarded | [project.v:171-176](../src/project.v#L171-L176) | [test.py:242](../test/test.py#L242) | PASS |
| F3 | Reconfiguring takes EN=0, then new CTRL, then KICK | — | [test.py:165](../test/test.py#L165) | PASS |

---

## G. IRQ

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| G1 | `IRQ = IRQ_FLAG AND IRQ_EN`, combinational | [project.v:186](../src/project.v#L186) | [test.py:320](../test/test.py#L320) | PASS |
| G2 | IRQ_FLAG is sticky; a KICK does not clear it | [project.v:179-182](../src/project.v#L179-L182) | [test.py:304](../test/test.py#L304) | PASS |
| G3 | Writing 1 to STATUS bit 0 clears IRQ_FLAG | [project.v:181-182](../src/project.v#L181-L182) | [test.py:315](../test/test.py#L315) | PASS |
| G4 | Writing 0 to STATUS does not clear IRQ_FLAG | [project.v:181](../src/project.v#L181) | [test.py:310](../test/test.py#L310) | PASS |
| G5 | Clearing IRQ_EN releases the IRQ pin, flag retained | [project.v:186](../src/project.v#L186) | [test.py](../test/test.py) `test_irq_en_release_keeps_flag` | PASS |
| G6 | `rst_n` clears IRQ | [project.v:167](../src/project.v#L167) | [test.py:149](../test/test.py#L149) | PASS |

### G5 — the other direction

`test_irq_en_gates_pin` only goes from IRQ_EN=0 to 1.
`test_irq_en_release_keeps_flag` covers the reverse: with the flag set,
clearing IRQ_EN drops the pin while STATUS still reads IRQ_FLAG=1, and
re-enabling surfaces the same pending interrupt. Note the ordering constraint
from F2 — CTRL is locked while counting, so IRQ_EN can only be changed after
the timeout has returned the machine to IDLE.

Verified by mutation: changing
[project.v:186](../src/project.v#L186) to `irq = irq_flag` makes it fail.

---

## H. PAUSE

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| H1 | PAUSE freezes the counter while high in COUNTING | [project.v:157](../src/project.v#L157) |  [test.py:351](../test/test.py#L351) | PASS |
| H2 | PAUSE does not change state or disarm | [project.v:157](../src/project.v#L157) | [test.py:352](../test/test.py#L352) | PASS |
| H3 | PAUSE has no effect in IDLE | [project.v:157](../src/project.v#L157) | `armed` already gates the counter | WAIVE |
| H4 | KICK takes priority over PAUSE | [project.v:151](../src/project.v#L151) | [test.py](../test/test.py) `test_kick_is_prior_to_pause` | PASS |
| H5 | SPI access is unaffected while PAUSE is high | — | [test.py:352](../test/test.py#L352) reads STATUS under PAUSE | PASS |
| H6 | PAUSE crosses into the clk domain without a synchroniser | [project.v:29](../src/project.v#L29) | — | see note |

### H3 — waived

The counter's increment is gated by `armed && !pause` at
[project.v:157](../src/project.v#L157). In IDLE `armed` is low, so the branch
is already dead regardless of PAUSE. Nothing PAUSE does there can leave
residue, because the only state it could touch is held at 0 by C5.

### H4 — a stated priority

The datasheet gives KICK priority over PAUSE: a kick during PAUSE clears the
counter, which then stays frozen at 0. `test_kick_is_prior_to_pause` observes
this through the aftermath — after releasing PAUSE, *half* a window must not
be enough to fire, because a full one remains.

That negative assertion is the whole test. Both a correct and an inverted
priority eventually fire; they differ only in when. Asserting "it fires
eventually" passes either way.

Verified by mutation: gating `do_kick` with `~pause` makes it fail with
"fired after half a window: the kick did not reset the counter".

### H6 — an RTL observation, not a test item

`pause` is taken straight from `ui_in[3]` at
[project.v:29](../src/project.v#L29) and used in the synchronous block at
[project.v:157](../src/project.v#L157) with no synchroniser, while the other
three asynchronous inputs — SCLK and CS_N at
[spi_regs.v:65-84](../src/spi_regs.v#L65-L84), KICK at
[project.v:64-75](../src/project.v#L64-L75) — all have three-stage ones.

The functional risk is small: a metastable sample costs at most one count on
a 2^23 window. But a static CDC checker would flag it as an unsynchronised
crossing, and LibreLane runs no such check (all 70 steps of the flow are
place-and-route and sign-off; CDC analysis is absent from the open-source
tool chain). No simulation will find this either — RTL flops have no
setup/hold window, so a synchroniser and a bare wire behave identically.

This is a design decision to make, not a test to write. Adding three flops
mirroring the KICK path would make the treatment of asynchronous inputs
uniform.

---

## I. Pins and outputs

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| I1 | `uo_out[7:2]` are driven low | [project.v:188](../src/project.v#L188) | [test.py](../test/test.py) `test_unused_pins` | PASS |
| I2 | `uio_out` and `uio_oe` are tied to 0 | [project.v:20-21](../src/project.v#L20-L21) | — | WAIVE |
| I3 | `ui_in[7:5]` are unused and affect nothing | [project.v:24](../src/project.v#L24) | [test.py](../test/test.py) `test_unused_pins` | PASS |

### I1 and I3 — one test

Neither needs a scenario of its own, only ordinary traffic to observe, so
`test_unused_pins` drives `ui_in[7:5]` high for the whole of a normal
arm-and-fire cycle and samples `uo_out[7:2]` at four points along it —
including while IRQ is asserted, which proves the spare bits are not merely a
stuck-low bus.

Verified by mutation: widening the output concatenation to duplicate `irq`
into a spare bit fails I1; repointing `pause` at `ui_in[5]` fails I3.

### I2 — waived

Guaranteed by a constant `assign`. Testing it tests the simulator, not the
design. This is also the only gap Verilator's coverage report shows, since
those signals never toggle — the correct behaviour.


---

## J. Cross-cutting: CDC phase

| ID | Feature | Covers | Status |
| --- | --- | --- | --- |
| J1 | The design works with SCLK edges at any phase relative to clk | A8, E5 | WAIVE |

**Waived: the risk this addresses is not simulatable, and the residue is not
worth its cost here.**

A phase sweep was written, run and then removed. What follows is why, so the
idea is not reinvented later.

### What cannot be tested at all

Metastability. An RTL flop copies `d` to `q` at the clock edge with no
setup/hold window, so a three-stage synchroniser and a bare wire simulate
identically. Gate level does not rescue this either: the TT flow runs without
SDF, and even with it, a testbench that drives SCLK from `ClockCycles` never
places an edge inside a timing window. A metastable flop settles to a random
value after an indeterminate delay; `X` propagation models "unknown", which is
a different thing.

### What could be tested, and why it finds little here

Protocol races — a phase at which `cs_n_rise` collides with the final `sample`
pulse, so `cnt` is cleared before it reaches `FRAME_BITS` and the frame is
dropped.

The architecture largely rules this out. SCLK passes through three
synchronising flops before anything looks at it, so by the time `sclk_s1` and
`sclk_s2` exist, the edge has been quantised onto a clk boundary. Everything
downstream — `sample`, `shift_out`, `cs_n_rise`, `cnt` — lives in a single
clock domain. Changing the input phase only changes *which clk cycle* the
synchroniser first sees the edge in, which is exactly the variation the design
is built to absorb.

Designs that do need phase sweeping have several clock domains, handshakes, or
gray-coded counters crossing between them. This one deliberately has none:
`sclk` is sampled data, not a clock.

### What the experiment showed

Two mutations were run against a 10-phase sweep over `range(0, CLK_NS, 2)`:

| Mutation | Phase sweep | Fixed phase |
| --- | --- | --- |
| Remove the guard `_settle()` before CS_N rises | PASS | PASS |
| Shorten `_settle()` from 4 clk to 3, then 2 | FAIL at phase 0 | FAIL |

Neither isolated a phase-dependent failure. The first shows that guard is
margin rather than a necessity at a 4-clk settle. The second fails at phase 0,
so the fixed-phase tests catch it just as well — and it is out-of-spec anyway,
since [info.md](info.md) requires each SCLK level to be held for at least two
clk periods.

These mutations do not prove no phase-dependent bug exists; a fairer test
would mutate the DUT's own synchroniser depth. But combined with the
architectural argument above, the expected yield is low.

### Cost

The sweep ran 10 phases over both directions: 211 µs of simulation, about 70%
of the whole unit suite's runtime, to re-test at 10 phases what the
synchronisers quantise back to one.

### If this is revisited

Reintroduce the sweep if the synchroniser depth in
[spi_regs.v:65-84](../src/spi_regs.v#L65-L84) is ever reduced, if a second
clock domain appears, or if silicon shows frames being dropped. A `phase_ns`
offset on `SpiMaster._settle()`, swept exhaustively rather than randomly, is
all it takes — the phase space is one clk period wide.

## Summary

| Status | Count |
| --- | --- |
| PASS | 41 |
| PART | 0 |
| TODO | 0 |
| WAIVE | 10 |
| **Total** | **51** |

Every row is now either covered or explicitly waived: 41/51 tested,
10 waived with a reason recorded above. Nothing is left
outstanding.

The waived rows are the useful part of that number. Each one names why the
behaviour cannot be observed through the pins, or why the structure behind it
has nothing to get wrong — B5's undecodable write, C8's same-cycle disarm,
J1's unsimulatable metastability. A count of tests passed says little on its
own; a count of claims deliberately not tested, each with its reason, is what
makes the coverage legible.

Line coverage remains 100% on `spi_regs.v` and told us none of this.

## Gate level scope

Eleven tests are marked `rtl_only` in [test.py](../test/test.py) and skip when
`GATES=yes`. Every one of them waits out a real timeout window, and the
netlist carries the silicon exponent: one window is 2^23 clocks, and
TIMEOUT=111 is 2^28. At gate level speeds that is hours for the shortest and
weeks for the longest, well past any CI limit. Left in, the `gl_test` job
would burn its six-hour timeout and fail without telling anyone anything.

### What that leaves unverified, and why it is acceptable

The skipped tests exercise the counter's high bits and the state machine's
timing. Nothing else covers *those specific bits* at gate level. The argument
that this is safe has three parts.

**The logic is proven equivalent by other means.** A miswired 4:1 mux on
`timeout_bit`, a mis-synthesised counter, an inverted priority — these are
structural, and the flow checks them structurally: `netgen` LVS at step 64,
plus the Yosys synthesis checks at steps 7 and 8. None of it needs
simulation. What the open-source flow lacks is RTL-to-netlist logic
equivalence checking (Conformal LEC, Formality), which in a commercial flow
would close this gap outright and make gate level functional simulation
largely redundant.

**The data path is still exercised.** The ten tests that do run at gate level
arm the watchdog and let the counter run; `test_kick_arms` and
`test_kick_is_prior_to_pause`'s fast half both toggle it. Only the carry into
bit 23 goes unobserved — and that carry is the same circuit as the carry into
bit 6, which is exercised thoroughly. There is no mechanism by which only the
high bits would fail.

**Timing is closed with margin.** The remaining risk is a timing violation
making the counter drop a beat, which simulation would not reliably catch
anyway. Post-PnR STA reports zero violations across all nine corners, worst
setup slack 11.31 ns against a 20 ns period and worst hold slack 0.1065 ns:

| Corner | Hold slack | Setup slack | Violations |
| --- | --- | --- | --- |
| Overall | 0.1065 | 11.3113 | 0 |
| `nom_ss_100C_1v60` | 0.8746 | 11.3460 | 0 |
| `min_ff_n40C_1v95` | 0.1065 | 14.5292 | 0 |

### What still confirms the absolute value

Nothing in simulation proves 23 is 23 — only that the structure is right.
That is checked on silicon during bring-up, by measuring the real 168 ms with
a scope. This is the normal division of labour for a scaled parameter:
simulation verifies structure, silicon verifies the constant.

### The stronger alternative, not taken

The clean fix is to make `WD_BASE_EXP` a module parameter rather than a
`define`, synthesise a second netlist with it set to 8, and run the full suite
against that. Both netlists go through the same flow, so it proves synthesis
does not break the design while staying simulatable. It is not done here
because the TinyTapeout CI hardens once; a second run would mean a parallel
flow for verification only.

## Verification method

Tests asserting that something *does not* happen — no write commits, the pin
stays low, the flag survives — pass just as readily when the test itself is
broken. Several rows here were checked by mutation: break the RTL in the
specific way the row describes, confirm the test fails, restore.

| Row | Mutation | Caught |
| --- | --- | --- |
| A4/A5 | Remove the saturating guard on `cnt` | yes |
| A6/A9 | `tx <= rd_data`, ignoring `rd_req` | yes |
| A10 | Capture rd_addr on `cnt < AW` (R/W bit and A1 instead of the ADDR field) | yes |
| B6 | Let a STATUS write set `armed` | yes |
| C6 | `irq_flag` survives reset | yes |
| G5 | `irq = irq_flag`, ignoring `irq_en` | yes |
| H4 | Gate `do_kick` with `~pause` | yes |
| I1 | Duplicate `irq` into a spare output bit | yes |
| I3 | Repoint `pause` at `ui_in[5]` | yes |
| D2-D8 | Swap two branches of the `timeout_bit` case | yes |

Two mutations that were *not* caught are recorded under J1: neither isolated a
phase-dependent failure, which is part of why that row is waived.
