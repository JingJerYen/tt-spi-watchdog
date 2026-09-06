![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# SPI-Configurable Watchdog Timer

A windowed watchdog timer for an external MCU, configured over SPI, built for
[Tiny Tapeout](https://tinytapeout.com) sky26c.

- Eight timeouts from 5.24 ms to 5.37 s (at 50 MHz), stretchable up to 128x
  by a prescaler.
- Window mode: a kick that comes too early is a fault, catching firmware
  stuck in a tight kick loop.
- Two-stage response: an `IRQ` warning with a grace period first, then an
  active-low reset pulse. During the grace period the MCU gets one last
  chance to cancel the reset.
- Fault flags survive the reset pulse, so the MCU can read back why it was
  reset.

📖 **[Full documentation (datasheet source)](docs/info.md)** — interface,
SPI register map, state machine, and timing.

## PPA

Numbers from the sky130A OpenLane 2 signoff run.

| | |
| --- | --- |
| **Power** | ~0.44 mW at the 50 MHz clock, 0.38 mW typical; 44% clock tree, 54% flops, leakage 7.6 nW. A coin cell would keep the dog awake for ~2 months. |
| **Performance** | Ships at 50 MHz, clean at every corner down to 100 C at 1.60 V; ~105 MHz worst case, ~180 MHz typical. Timeouts from 5.24 ms to 5.37 s, or 687 s with the prescaler, with a formally proven 1.31 ms grace period and 10.49 ms reset pulse. |
| **Area** | One tile, 161 x 111.52 um = 0.018 mm^2; 817 standard cells, 102 flops, 36.2% utilization, 9.5 mm of routing. |

## GDS

[View in 3D](https://gds-viewer.tinytapeout.com/?pdk=sky130A&model=https%3A%2F%2Fjingjeryen.github.io%2Ftt-spi-watchdog%2Ftinytapeout.oas)

![GDS render](gds_render.png)

## Testing

The cocotb testbench lives in [test/](test/); see
[test/README.md](test/README.md) for how to run it at RTL and gate level.

A testbench only covers the cases you thought of, so the properties that matter
most are proven instead: [formal/](formal/) holds a SymbiYosys setup that checks
the state machine, the reset pulse and the `LOCK` invariant under every possible
input sequence.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that makes it easier and cheaper than
ever to get your digital and analog designs manufactured on a real chip.
To learn more and get started, visit https://tinytapeout.com.
