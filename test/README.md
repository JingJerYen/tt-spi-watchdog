# Testbench for the SPI watchdog

[cocotb](https://docs.cocotb.org/en/stable/) testbenches driving the design
through its real interfaces (SPI frames and pins). Two suites:

- `test.py` — the full watchdog: registers, timeout/window/prescaler
  behavior, the FSM, the fault path and grace period, the reset pulse,
  `LOCK`, and pin packing. Runs against RTL and the gate-level netlist.
- `test_spi.py` — unit tests for `spi_regs` alone, with a parameter sweep
  of the frame geometry.

Dependencies: `pip install -r requirements.txt`, plus Icarus Verilog
(default simulator) or Verilator.

## RTL simulation

```sh
make -B                  # full suite with Icarus
make -B SIM=verilator    # same, faster
```

The Makefile sets `WD_BASE_EXP = 8`, so simulated timeout windows are
2^8 clocks instead of the silicon 2^18 — every test runs in seconds.

To run a single test (the filter is a regex over test names):

```sh
make -B COCOTB_TEST_FILTER=test_lock_bit
```

## Gate-level simulation

Harden the project first, copy the netlist here as
`gate_level_netlist.v` (the GDS action does this automatically in CI),
then:

```sh
make -B GATES=yes
```

Tests marked `@rtl_only` are skipped: the netlist carries the silicon
`WD_BASE_EXP = 18`, so waiting out a real timeout window would take hours.
What remains covers the register interface and the pin packing — the
things synthesis could actually have broken.

## SPI unit tests

```sh
make -f Makefile.spi                # default geometry, AW=2 DW=7
make -f Makefile.spi AW=3 DW=8      # sweep a different frame width
make -f Makefile.spi SIM=verilator
```

`spi_regs` is parameterised, so the same tests also run against widths the
chip never instantiates — useful for checking the bit-counter sizing.

## Waveforms

Each run writes `tb.fst`. View it with:

```sh
gtkwave tb.fst tb.gtkw    # tb.gtkw restores a useful signal layout
surfer tb.fst
```

For VCD instead of FST: edit `tb.v` to `$dumpfile("tb.vcd")` and run
`make -B FST=`.

## Coverage (Verilator only)

```sh
make -B SIM=verilator RTL_COVERAGE=1   # run the suite instrumented
make cov-report                        # summarise + annotate + HTML
make cov-serve                         # serve the HTML report on localhost
make cov-clean                         # remove all coverage output
```

`RTL_COVERAGE=1` builds into `sim_build/cov`, so an instrumented build
never overwrites a plain one. `cov-report` prints two numbers that are
both correct: genhtml counts *lines*, the annotated view counts *toggle
points* (one unexercised 8-bit port is three lines but sixteen toggles).
It leaves:

- `cov_annotated/` — annotated sources; only `%000000` means uncovered,
  other `%00NNNN` values are hit counts.
- `coverage_html/` — open `index.html` in a browser.

`cov-serve` exists for Remote-SSH sessions where this machine has no
browser: it serves `coverage_html/` on a free localhost port. VSCode picks
the port up in its PORTS tab — click the globe icon to open the report in
the local browser. Stop it with Ctrl+C.
