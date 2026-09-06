# Formal verification: a minimal SymbiYosys example

## What is this, and how does it differ from a testbench?

|  | Simulation (cocotb / iverilog) | Formal (SymbiYosys) |
| --- | --- | --- |
| Where the inputs come from | you write them in a testbench | the solver enumerates all of them |
| What gets checked | the cases you thought of | your properties, under every case |
| What a failure gives you | a log | a counterexample waveform: the shortest input sequence that breaks the property |
| Blind spot | a case you never wrote is never tested | only a bounded number of cycles, so long behaviour is out of reach |

In one line: simulation asks "does it work on the paths I tried", formal asks
"does it work on every path". The price is that the circuit has to be small
and the interesting behaviour has to happen within a few dozen cycles, which
is why this example shrinks `WD_BASE_EXP` from 18 to 3.

## Files

- `wdt_formal.sv` wraps the DUT. All the properties live here, and nothing in
  `src/` had to change.
- `wdt.sby` the SymbiYosys config: which files to read, how deep to run, which
  solver to use.
- `Makefile` the entry point for everything below.

## Three keywords

```systemverilog
assume(cond);  // constrain the inputs: "the outside world only does this"
assert(cond);  // this must hold under every input sequence, or show me a counterexample
cover(cond);   // find one input sequence that makes this true, and show me the waveform
```

## Running it

First put the tools on PATH (once per shell):

```bash
source ~/oss-cad-suite/environment
```

Then, from `formal/`:

```bash
make
```

`make` runs bmc and cover, the quickest useful regression. Other targets:

| Command | What it does |
| --- | --- |
| `make bmc` | bounded model check, checks the asserts |
| `make cover` | produce a waveform for each cover |
| `make proof` | k-induction, proves the asserts hold forever |
| `make irq_demo` | deliberately failing example, shows a counterexample |
| `make all` | all four (`irq_demo` is expected to FAIL) |
| `make wave` | open the most recent waveform in gtkwave |
| `make clean` | remove every directory sby produced |

- `bmc` checks every assert. A final `PASS` means that within the configured
  depth (currently 100 cycles) no input sequence violates any of them.
- `cover` has the solver demonstrate how the dog starts counting and how it
  eventually bites. Waveforms land in `wdt_cover/engine_0/trace*.vcd`.
- `irq_demo` adds one deliberately wrong assert, "IRQ never goes high". The
  solver answers with an SPI frame that proves otherwise, in
  `wdt_irq_demo/engine_0/trace.vcd`.

`make wave` picks the most recently produced waveform:

```bash
make wave
```

For a specific one, call gtkwave directly:

```bash
gtkwave wdt_irq_demo/engine_0/trace.vcd
```

## Reading the result

```
SBY  ... summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:05 (5)
SBY  ... summary: engine_0 (smtbmc yices) returned pass
SBY  ... DONE (PASS, rc=0)
```

- `PASS` means no counterexample within `depth` cycles. In bmc that is a
  statement about the first N cycles, not a proof for all time. Use `proof`
  for that.
- `FAIL` means a counterexample exists. Open `trace.vcd` and work backwards
  from the last cycle to find the input that caused it.
- For cover, `PASS` means every cover found a path, and `FAIL` means one did
  not: either the depth is too shallow or the state is genuinely unreachable.

## Reading a real counterexample: irq_demo

`irq_demo` asserts "IRQ never goes high". The solver finds this path in 27
cycles (one row per cycle, decoded from trace.vcd):

```
step  SCLK MOSI CS_N KICK | state  irq | wr_en addr data
  1     1    0    0    0  | IDLE    0  |
  ...   (CS_N low, SCLK toggling, MOSI shifting in 0 0 0 1 0 0 0 0 1 1)
 20     1    1    1    0  | IDLE    0  |          <- CS_N high, frame ends
 22     0    1    1    0  | IDLE    0  | 1     0   0x43   <- CTRL = 0x43 committed
 21/23             KICK=1 -> 0 -> 1                       <- two rising edges on KICK
 24                       | EARLY   0  |
 26                       | RWAIT   1  |                  <- early_flag set, IRQ high
```

CTRL = 0x43 = `0b1000011`, so EN=1, IRQ_EN=1, WINDOW=2. The solver has never
read the datasheet. Purely by enumerating inputs it discovered that arming the
dog with an early window over SPI and then kicking a second time inside that
window sets EARLY_FLAG and raises IRQ. That is the value of formal: you do not
have to think of the test case.

## Asserting on the DUT's internal registers (lock, en, ...)

There are two standard ways to reach inside a DUT: a hierarchical reference
like `dut.lock`, or a SystemVerilog `bind` that attaches a property module.
Yosys's built-in `read -formal` frontend supports **neither**. A hierarchical
reference is rejected outright, or worse becomes an undriven wire that the
solver fills freely, so the results are garbage. A `bind` is parsed and then
silently discarded, so the properties never enter the design and bmc returns
a meaningless PASS. Where the SBY docs say these are supported, they mean the
commercial Verific frontend.

The open-source answer is the **slang frontend** shipped with the OSS CAD
Suite (`plugin -i slang; read_slang ...`), a complete SystemVerilog
implementation where hierarchical references work. That is why `wdt.sby` uses
`read_slang`, and why the wrapper can write `$past(dut.lock)` without touching
`src/`.

The other common approach is to put the properties in the RTL behind
`` `ifdef FORMAL ``, the long-standing convention in the Yosys community. It
works, but it pads the RTL with verification code.

## Choosing a guard: loose for assert, tight for cover

Formal has no testbench, so at time 0 every register holds an arbitrary value.
`fsm_state`, `lock` and `en` in `project.v` have no initial value (ASIC flops
have no defined power-on state), and the reset is synchronous, so it only
takes effect on the first clock edge. During cycle 0 the DUT is full of
garbage, and every property needs a guard that excludes it.

`$past(x)` is itself just a register that samples `x` on every clock edge, so
it has the same problem one cycle later: at `cyc==1` it holds the pre-reset
garbage from `cyc==0`. Properties that read `$past` have to wait one more
cycle.

The rule is to ask when everything a property reads first becomes meaningful:

| What the property reads | Guard |
| --- | --- |
| only the current cycle | `rst_n` |
| one level of `$past` | `cyc == 2'd2` |
| the reset cycle itself | `cyc == 2'd1` (this is P1) |

**But assert and cover want opposite tightness.**

An `assert` guard should be as **loose** as correctness allows. The more
cycles it is checked over, the less unguarded space the solver has to stage a
fake state in. INV1, P2, P3 and P4 use no `$past`, so they sit under `rst_n`;
P5 and P6 use `$past` and must stay under `cyc == 2'd2`.

A `cover` guard should be **tight**, always `cyc == 2'd2`, whether or not it
uses `$past`. A cover asks whether a path exists, so a looser guard makes it
easier to satisfy, and a cover that is trivial to satisfy proves nothing.
Measured on this design: moving the cover block to `rst_n` drops three of the
"back to IDLE" transition covers from 28 and 44 cycles to **2**, because
`state` is the freshly reset IDLE while `$past(state)` is pre-reset garbage
the solver can set to anything.

## Three shapes of a vacuous PASS

A green light does not mean you proved something. All three of these PASS and
all three prove nothing:

1. **The assert's precondition is unreachable.** P6 is predicated on
   `lock=1`, and cover shows that takes 44 cycles. At a bmc depth of 40, P6
   passes because its precondition never holds.
2. **The assert's guard is too narrow.** Put `assert (!dut.lock || dut.en)`
   inside `cyc == 2'd1` and it holds trivially, because `lock` and `en` were
   just cleared by reset. Measured: it contributes nothing to the induction
   depth, and after injecting a bug the assert that fires is P6, not this one.
3. **The cover's guard sits before reset.** Move the cover block to
   `cyc == 2'd0` and every cover is reached in 1 cycle, because the solver
   simply picks the register values it needs.

How to tell:

- **Read the cycle counts, not just PASS.** A single SPI frame in this design
  takes 22 cycles, so any cover reached in single digits is almost certainly
  vacuous.
- **Pair every assert with a cover** that shows its precondition is
  reachable, and set the bmc depth above the deepest cover.
- **Mutation testing.** Inject a bug the assert ought to catch, and check that
  it actually fires, and that it is that assert firing rather than another
  one. This is far more informative than staring at a PASS.

## The proof task and invariants

`mode prove` uses k-induction, which proves a property holds forever rather
than for N cycles. It does not start from reset: the solver conjures a state
whose only constraint is that the asserts held for the previous k cycles. So
it frequently starts from a state reset could never produce, and fails.

**An induction failure usually means a missing invariant, not a bug.** The
working loop is: run, fail, decode cycle 0 of `trace_induct.vcd` to see what
impossible state the solver invented, add an assert that rules it out, run
again. This design hit two of them:

- The solver picked `lock=1` with `en=0`. The RTL guarantees that cannot
  happen, since `lock <= lock | (wr_data[4] & en)` only sets lock while en is
  1, but no assert said so. Fixed by `assert (!dut.lock || dut.en)`, which is
  INV1 in `wdt_formal.sv`.
- The solver picked "the pins have never moved (`quiet=1`) but the SPI shift
  register holds a complete frame", which three cycles later fabricates a
  write and breaks P4. Ruling this out needs a second invariant stating that
  the SPI and KICK synchronisers are idle whenever `quiet` holds. It is not in
  the file, because at this size it buys nothing (see below).

For `prove`, `depth` is the induction window k, which means something entirely
different from the bmc depth. k is **not** a quality metric: a pass at depth 3
and a pass at depth 40 are the same theorem. Measured on this design:

| Configuration | Required depth |
| --- | --- |
| no invariant | 5 |
| INV1 only (lock implies en) | 5, with the failure pushed to P4 |
| INV1 plus the quiet/synchroniser invariant | 3 |

Across that whole range the run takes between 0.42 s and 0.82 s, so writing
invariants to lower k is not worth it at this size. The time to care is when the circuit is large
enough that the solver stalls. Practical advice: set a comfortable
`proof: depth` and spend the effort on specification properties instead.

**Write invariants as `assert`, never as `assume`.** Measured with the same
injected bug:

```
invariant written as assert  ->  DONE (FAIL)   caught
invariant written as assume  ->  DONE (PASS)   same broken design, passed
```

`assume` tells the solver not to consider a case, which deletes the buggy
paths from the search space. Internal state always gets `assert`; keep
`assume` for genuine constraints on external inputs.

## Things to try next

1. Change `cover: depth 80` in `wdt.sby` to `30` and watch the covers that
   need `RESET` stop being reachable (that one takes 49 cycles).
2. Add a property you believe should hold, for example "RESET_WAIT only
   advances to RESET while rst_en is 1".
3. Change `proof: depth 5` to `4`, see which assert fails, then decode cycle 0
   of `wdt_proof/engine_0/trace_induct.vcd` as described above.
4. Run a mutation test: change `lock <= lock | (wr_data[4] & en)` in
   `project.v` to `lock <= lock | wr_data[4]`, run `make bmc`, and confirm
   INV1 is the assert that fires. Then change it back.
