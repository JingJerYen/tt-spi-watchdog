# Verification Plan

A feature-by-feature verification plan for `tt_um_spi_watchdog`, derived from
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
| A1 | Mode 0: MOSI is sampled on the rising edge of SCLK | [spi_regs.v:88](../src/spi_regs.v#L88) | implicit in every SPI test | PASS |
| A2 | Mode 0: MISO updates on the falling edge of SCLK | [spi_regs.v:89](../src/spi_regs.v#L89) | implicit in every read | PASS |
| A3 | Frame is 10 bits, MSB first, `[R/W][ADDR:2][DATA:7]` | [spi_regs.v:56](../src/spi_regs.v#L56) | implicit | PASS |
| A4 | A frame commits only at exactly 10 bits | [spi_regs.v:109](../src/spi_regs.v#L109) | [test_spi.py](../test/test_spi.py) `test_a4_frame_length_enforced` | PASS |
| A5 | The bit counter saturates rather than wrapping | [spi_regs.v:103](../src/spi_regs.v#L103) | folded into A4's length sweep | PASS |
| A6 | On a read, MISO is 0 during the R/W and ADDR bits | [spi_regs.v:144-150](../src/spi_regs.v#L144-L150) | `read()` masks with `& 0x7F`, discarding those bits | TODO |
| A7 | SCLK edges while CS_N is high are ignored | [spi_regs.v:86](../src/spi_regs.v#L86) | — | TODO |
| A8 | SCLK / CS_N are asynchronous and synchronised before use | [spi_regs.v:65-84](../src/spi_regs.v#L65-L84) | driven only at fixed clk-aligned phase | TODO |
| A9 | R/W selects write (0) or read (1) | [spi_regs.v:111](../src/spi_regs.v#L111) | [test.py:98](../test/test.py#L98), [test.py:101](../test/test.py#L101) | PASS |

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

### A6 — a real protocol claim

The datasheet promises MISO is 0 during the first 3 bit positions. A master
that reads the full 10-bit return word sees those bits. `read()` throws them
away, so a design that leaked stale `tx` content there would still pass today.
Assert on the full `rx`, not the masked value.

---

## B. Register map

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| B1 | CTRL (addr 0) reads back `{3'b0, TIMEOUT, IRQ_EN, EN}` | [project.v:92](../src/project.v#L92) | [test.py:155](../test/test.py#L155) | PASS |
| B2 | CTRL bits 6:4 are unimplemented and read as 0 | [project.v:92](../src/project.v#L92) | never written as 1 | TODO |
| B3 | KICK (addr 1) is write-only and reads as 0 | [project.v:99](../src/project.v#L99) | [test.py:174](../test/test.py#L174) | PASS |
| B4 | STATUS (addr 2) reads `{5'b0, ARMED, IRQ_FLAG}` | [project.v:93](../src/project.v#L93) | many | PASS |
| B5 | Address 3 is unallocated: reads 0, writes ignored | [project.v:99](../src/project.v#L99) | [test.py:175](../test/test.py#L175) reads only | PART |
| B6 | STATUS bit 1 (ARMED) is read-only; writes are ignored | [project.v:181](../src/project.v#L181) | — | TODO |

### B2 — check the reserved bits

Write `0x7F` to CTRL and confirm the readback is `0x0F`. A design that
widened the register by accident would be caught here and nowhere else.

### B5 — writes to address 3

A write to address 3 must not disturb any other register. Set up known CTRL
and STATUS values, write to address 3, confirm both are unchanged.

### B6 — ARMED is not writable

Only `wr_data[0]` participates in the W1C path. Writing `0x02` to STATUS
while armed must leave ARMED set; while idle it must leave ARMED clear.

---

## C. Watchdog state machine

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| C1 | IDLE to COUNTING on KICK while EN=1 | [project.v:151-153](../src/project.v#L151-L153) | [test.py:191](../test/test.py#L191) | PASS |
| C2 | KICK in IDLE with EN=0 is ignored | [project.v:141](../src/project.v#L141) | [test.py:185](../test/test.py#L185) | PASS |
| C3 | KICK in COUNTING clears the counter, stays armed | [project.v:151-153](../src/project.v#L151-L153) | [test.py:361](../test/test.py#L361) | PASS |
| C4 | Timeout returns to IDLE and sets IRQ_FLAG | [project.v:154-157](../src/project.v#L154-L157) | [test.py:287](../test/test.py#L287) | PASS |
| C5 | Clearing EN returns to IDLE | [project.v:148-150](../src/project.v#L148-L150) | [test.py:264](../test/test.py#L264) | PASS |
| C6 | `rst_n` returns to IDLE from any state | [project.v:145-147](../src/project.v#L145-L147) | [test.py:145](../test/test.py#L145) checks initial values only | PART |
| C7 | Counter is held at 0 while EN=0 | [project.v:148-150](../src/project.v#L148-L150) | — | TODO |
| C8 | Clearing EN disarms in the same cycle, with no stale ARMED readback | [project.v:137-138](../src/project.v#L137-L138) | — | TODO |

### C6 — reset while counting

Reset is only ever applied from the power-up state. Arm the watchdog, let the
counter run partway, assert `rst_n`, and confirm ARMED, IRQ_FLAG and the
counter all clear — the last one observed indirectly, by checking that the
next window after re-arming is full length.

### C7 — no counting while disarmed

With EN=0, wait well past a full timeout window and confirm IRQ never fires.
Distinct from C2: that one checks arming, this one checks the counter itself
is held.

### C8 — the same-cycle disarm

`en_now` exists specifically so a CTRL write clearing EN disarms on the cycle
it lands rather than one cycle later. The comment at
[project.v:135-136](../src/project.v#L135-L136) records the intent. Hard to
observe over SPI, since the readback is many cycles later — low priority, and
a candidate for waiving if no clean observation exists.

---

## D. Timeout selection

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| D1 | TIMEOUT=00 fires on counter bit `WD_BASE_EXP` | [project.v:128](../src/project.v#L128) | [test.py:268](../test/test.py#L268) | PASS |
| D2 | TIMEOUT=01 fires on counter bit `WD_BASE_EXP + 2` | [project.v:129](../src/project.v#L129) | — | TODO |
| D3 | TIMEOUT=10 fires on counter bit `WD_BASE_EXP + 4` | [project.v:130](../src/project.v#L130) | — | TODO |
| D4 | TIMEOUT=11 fires on counter bit `WD_BASE_EXP + 6` | [project.v:131](../src/project.v#L131) | used at [test.py:235](../test/test.py#L235) to hold armed, never allowed to fire | TODO |

### D2-D4 — the clearest gap in the suite

All four TIMEOUT values are *written and read back* by
`test_ctrl_readback`, and the `case` statement at
[project.v:127-133](../src/project.v#L127-L133) reaches 100% line coverage
because `always @(*)` re-evaluates every branch. But only `sel=0` has ever
been timed. Swapping two branches of that `case` would not fail a single
existing test.

`TIMEOUTS` at [test.py:42](../test/test.py#L42) already carries the expected
exponent per selection; `wait_for_irq` already takes a `timeout_exp`
argument. The parts are in place.

Note the simulation cost: with `WD_BASE_EXP=8` the windows are 2^8, 2^10,
2^12 and 2^14 clocks. All four are cheap. Do not run these against the gate
level build, where `WD_BASE_EXP` is 23.

---

## E. KICK sources

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| E1 | An SPI write of `0x5A` to addr 1 kicks | [project.v:104](../src/project.v#L104) | [test.py:210](../test/test.py#L210) | PASS |
| E2 | Any other value written to KICK is ignored | [project.v:104](../src/project.v#L104) | [test.py:207](../test/test.py#L207) tests `0x12` only | PART |
| E3 | A rising edge on the KICK pin kicks | [project.v:76](../src/project.v#L76) | [test.py:197](../test/test.py#L197) | PASS |
| E4 | KICK is edge triggered, not level triggered | [project.v:76](../src/project.v#L76) | — | TODO |
| E5 | The KICK pin is asynchronous and synchronised | [project.v:64-75](../src/project.v#L64-L75) | driven only at fixed clk-aligned phase | TODO |
| E6 | A KICK after a timeout re-arms without clearing IRQ_FLAG | [project.v:179-182](../src/project.v#L179-L182) | [test.py:304](../test/test.py#L304) | PASS |

### E2 — near-miss values

`0x12` shares no bits with `0x5A`. Values one bit away — `0x5B`, `0x58`,
`0x1A`, `0x7A` — would catch a comparator built from too few bits.

### E4 — the highest-value missing test

If [project.v:76](../src/project.v#L76) were `wire kick_pin_evt = kick_s1;`,
turning the edge detector into a level detector, **every existing test would
still pass** — `pin_kick()` at [test.py:103](../test/test.py#L103) always
returns the pin low. A watchdog that can be silenced by tying one pin high is
a serious failure for the part's actual purpose.

Hold KICK high for more than two full timeout windows and assert that IRQ
fires anyway.

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
| G5 | Clearing IRQ_EN releases the IRQ pin, flag retained | [project.v:186](../src/project.v#L186) | only the 0 to 1 direction is tested | TODO |
| G6 | `rst_n` clears IRQ | [project.v:167](../src/project.v#L167) | [test.py:149](../test/test.py#L149) | PASS |

### G5 — the other direction

`test_irq_en_gates_pin` sets IRQ_EN from 0 to 1 and watches IRQ appear. The
reverse — flag set, IRQ_EN cleared, pin drops but STATUS still reads
IRQ_FLAG=1 — is not tested. Note the ordering constraint from F2: clearing
IRQ_EN requires being in IDLE.

---

## H. PAUSE

| ID | Feature | RTL | Test | Status |
| --- | --- | --- | --- | --- |
| H1 | PAUSE freezes the counter while high in COUNTING | [project.v:157](../src/project.v#L157) |  [test.py:351](../test/test.py#L351) | PASS |
| H2 | PAUSE does not change state or disarm | [project.v:157](../src/project.v#L157) | [test.py:352](../test/test.py#L352) | PASS |
| H3 | PAUSE has no effect in IDLE | [project.v:157](../src/project.v#L157) | — | TODO |
| H4 | KICK takes priority over PAUSE | [project.v:151](../src/project.v#L151) | — | TODO |
| H5 | SPI access is unaffected while PAUSE is high | — | [test.py:352](../test/test.py#L352) reads STATUS under PAUSE | PASS |
| H6 | PAUSE crosses into the clk domain without a synchroniser | [project.v:29](../src/project.v#L29) | — | see note |

### H3 — testing an absence

"No effect" is testable as "leaves no residue". Hold PAUSE through a full
window while in IDLE, confirm nothing arms and IRQ stays low, then release
PAUSE, kick, and confirm the resulting window is full length.

### H4 — a stated priority

The datasheet says a kick during PAUSE clears the counter, which then stays
frozen at 0. Observe by kicking under PAUSE, releasing PAUSE, and timing the
window from the release — it must be a full window, not a partial one.

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
| I1 | `uo_out[7:2]` are driven low | [project.v:188](../src/project.v#L188) | — | TODO |
| I2 | `uio_out` and `uio_oe` are tied to 0 | [project.v:20-21](../src/project.v#L20-L21) | — | WAIVE |
| I3 | `ui_in[7:5]` are unused and affect nothing | [project.v:24](../src/project.v#L24) | — | TODO |

### I1 — cheap to fold in

Assert `int(dut.uo_out.value) >> 2 == 0` alongside an existing check rather
than as a test of its own.

### I2 — waived

Guaranteed by a constant `assign`. Testing it tests the simulator, not the
design. This is also the only gap Verilator's coverage report shows, since
those signals never toggle — the correct behaviour.

### I3 — drive the unused inputs

Set `ui_in[7:5]` high for the duration of an otherwise normal test and
confirm the result is unchanged. Guards against a typo in a pin index.

---

## J. Cross-cutting: CDC phase

| ID | Feature | Covers | Status |
| --- | --- | --- | --- |
| J1 | The design works with SCLK edges at any phase relative to clk | A8, E5 | TODO |

Every SPI edge is currently driven by `ClockCycles`, so SCLK and CS_N
transitions land on exact `clk` boundaries — one single phase relationship
out of the continuum a real master would produce.

Two things this hides:

- **Protocol races.** The extra `_settle()` at
  [test.py:88](../test/test.py#L88), before CS_N rises, exists to keep
  `cs_n_rise` from colliding with the final `sample` pulse. That collision is
  phase-dependent; at a different phase the priority chain at
  [spi_regs.v:99-104](../src/spi_regs.v#L99-L104) clears `cnt` before it
  reaches 10 and the frame is silently dropped.
- **Nothing about metastability.** No simulator models it; a synchroniser and
  a plain wire are indistinguishable in RTL simulation. Phase sweeping finds
  protocol races, not marginal setup/hold.

A `Timer` offset added to `_settle()` breaks the clk alignment. An exhaustive
sweep over `range(0, CLK_NS, 2)` is preferable to a random offset here: the
phase space is small enough to cover completely, so exhaustive is both
cheaper and stronger than random.

---

## Summary

| Status | Count |
| --- | --- |
| PASS | 25 |
| PART | 4 |
| TODO | 18 |
| WAIVE | 1 |
| **Total** | **48** |

Functional coverage is 25/48, about 52%, against 100% line coverage on
`spi_regs.v`. The gap between those two numbers is the point of this
document.

## Suggested order

Ranked by risk caught per unit of effort.

| Rank | ID | Why it goes first |
| --- | --- | --- |
| 1 | E4 | A level-vs-edge bug would defeat the watchdog's purpose and pass every current test |
| 2 | D2-D4 | Three quarters of the timeout settings have never been timed |
| 3 | H4 | An explicitly documented priority rule with no test behind it |
| 4 | A6 | A datasheet promise about MISO that the test helper discards |
| 5 | C6 | Reset is only ever exercised from the power-up state |
| 6 | J1 | Finds protocol races that no fixed-phase test can reach |
| 7 | B2, B5, B6 | Register hygiene; quick to write, low individual risk |
| 8 | H3, C7, G5 | Absence-of-effect cases, each testable via its aftermath |
| 9 | I1, I3, E2 | Fold into existing tests rather than adding new ones |
