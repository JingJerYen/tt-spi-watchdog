# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the SPI-configurable watchdog timer.

Everything here drives and observes the design through its pins only, so the
same tests run against the gate level netlist. Internal state is checked by
reading the STATUS register back over SPI rather than by peeking at signals.

The shortest timeout is 2^23 clocks, which is far too long to simulate for
every case. Only test_timeout_fires runs a real timeout to completion; the
rest exercise the SPI and control paths, which are timeout independent.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

CLK_NS = 20  # 50 MHz, matching the datasheet

# The RTL build shrinks the timeout windows so they can be simulated; see
# WD_BASE_EXP in the Makefile. Silicon uses 23. Gate level sim has the real
# netlist, so it falls back to the silicon value.
WD_BASE_EXP = int(os.environ.get("WD_BASE_EXP", 23 if os.environ.get("GATES") == "yes" else 8))

# Pin map
SCLK, MOSI, CS_N, PAUSE, KICK = 0, 1, 2, 3, 4
MISO, IRQ = 0, 1

# Register addresses
ADDR_CTRL, ADDR_KICK, ADDR_STATUS = 0, 1, 2

KICK_MAGIC = 0x5A

# STATUS bits
ST_IRQ_FLAG = 1 << 0
ST_ARMED = 1 << 1

# Timeout selections: (CTRL field value, exponent)
TIMEOUTS = [(sel, WD_BASE_EXP + 2 * sel) for sel in range(4)]


def ctrl_word(en=0, irq_en=0, timeout=0):
    """Pack a CTRL register value: {3'b0, TIMEOUT[1:0], IRQ_EN, EN}."""
    return (en & 1) | ((irq_en & 1) << 1) | ((timeout & 3) << 2)


class SpiMaster:
    """Bit-banged SPI mode 0 master driving ui_in."""

    def __init__(self, dut):
        self.dut = dut
        self._pins = 0
        self.set_pin(CS_N, 1)

    def set_pin(self, bit, value):
        if value:
            self._pins |= 1 << bit
        else:
            self._pins &= ~(1 << bit)
        self.dut.ui_in.value = self._pins

    def get_out(self, bit):
        return (int(self.dut.uo_out.value) >> bit) & 1

    async def _settle(self):
        # The design synchronises SCLK and CS_N through three flops, so each
        # phase must be held long enough for the edge to be seen.
        await ClockCycles(self.dut.clk, 4)

    async def xfer(self, word, nbits=10):
        """Clock nbits of `word` out MSB first, returning what MISO sent back."""
        self.set_pin(CS_N, 0)
        await self._settle()

        rx = 0
        for i in range(nbits - 1, -1, -1):
            self.set_pin(MOSI, (word >> i) & 1)
            await self._settle()
            self.set_pin(SCLK, 1)  # rising edge: design samples MOSI
            await self._settle()
            rx = (rx << 1) | self.get_out(MISO)
            self.set_pin(SCLK, 0)  # falling edge: design updates MISO
            await self._settle()

        await self._settle()
        self.set_pin(CS_N, 1)  # rising edge commits the frame
        await self._settle()
        return rx

    async def write(self, addr, data, nbits=10):
        return await self.xfer(((addr & 3) << 7) | (data & 0x7F), nbits)

    async def read(self, addr):
        rx = await self.xfer((1 << 9) | ((addr & 3) << 7))
        return rx & 0x7F

    async def kick(self):
        await self.write(ADDR_KICK, KICK_MAGIC)

    async def pin_kick(self):
        """Feed the dog with a rising edge on the KICK pin."""
        self.set_pin(KICK, 1)
        await ClockCycles(self.dut.clk, 6)
        self.set_pin(KICK, 0)
        await ClockCycles(self.dut.clk, 6)


async def wait_for_irq(spi, timeout_exp=None, slack=None):
    """Wait out a full timeout window, then poll the IRQ pin.

    Stepping the simulation in one long jump rather than polling in small
    chunks keeps these tests to a few seconds each.
    """
    if timeout_exp is None:
        timeout_exp = WD_BASE_EXP
    if slack is None:
        slack = max(64, 2**timeout_exp // 8)
    clk = spi.dut.clk
    await ClockCycles(clk, max(0, 2**timeout_exp - slack))
    for _ in range(3 * slack):
        if spi.get_out(IRQ):
            return
        await ClockCycles(clk, 1)
    raise AssertionError(f"IRQ never asserted around 2^{timeout_exp} clocks")


async def setup(dut, log="Start"):
    dut._log.info(log)
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())

    spi = SpiMaster(dut)
    dut.ena.value = 1
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    return spi


@cocotb.test()
async def test_reset_state(dut):
    """After reset the registers are clear and IRQ is low."""
    spi = await setup(dut, "Reset state")

    assert spi.get_out(IRQ) == 0, "IRQ should be low after reset"
    assert await spi.read(ADDR_CTRL) == 0, "CTRL should reset to 0"
    assert await spi.read(ADDR_STATUS) == 0, "STATUS should reset to 0"


@cocotb.test()
async def test_ctrl_readback(dut):
    """CTRL holds what was written and reads back over MISO."""
    spi = await setup(dut, "CTRL readback")

    for tsel, _ in TIMEOUTS:
        want = ctrl_word(en=1, irq_en=1, timeout=tsel)
        await spi.write(ADDR_CTRL, want)
        got = await spi.read(ADDR_CTRL)
        assert got == want, f"CTRL readback {got:#04x}, wanted {want:#04x}"
        # Return to IDLE so the next TIMEOUT write is not locked out.
        await spi.write(ADDR_CTRL, 0)


@cocotb.test()
async def test_unmapped_registers_read_zero(dut):
    """KICK and the unallocated address 3 read back as 0."""
    spi = await setup(dut, "Unmapped registers")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1))
    assert await spi.read(ADDR_KICK) == 0, "KICK should read as 0"
    assert await spi.read(3) == 0, "address 3 should read as 0"


@cocotb.test()
async def test_kick_arms(dut):
    """Both kick sources arm the watchdog, and only while EN is set."""
    spi = await setup(dut, "Kick arms the watchdog")

    # Disabled: a kick is ignored.
    await spi.kick()
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "kick armed while EN=0"

    # Enabled: an SPI kick arms it.
    await spi.write(ADDR_CTRL, ctrl_word(en=1))
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "armed before any kick"
    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "SPI kick did not arm"

    # Back to idle, then arm via the pin instead.
    await spi.write(ADDR_CTRL, 0)
    await spi.write(ADDR_CTRL, ctrl_word(en=1))
    await spi.pin_kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "pin kick did not arm"


@cocotb.test()
async def test_bad_kick_value_ignored(dut):
    """Only 0x5A feeds the dog; other values written to KICK do nothing."""
    spi = await setup(dut, "Bad kick value")

    await spi.write(ADDR_CTRL, ctrl_word(en=1))
    await spi.write(ADDR_KICK, 0x12)
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "0x12 should not kick"

    await spi.write(ADDR_KICK, KICK_MAGIC)
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "0x5A should kick"


@cocotb.test()
async def test_frame_length_enforced(dut):
    """Frames that are not exactly 10 bits are discarded."""
    spi = await setup(dut, "Frame length")

    known = ctrl_word(en=1, irq_en=1)
    await spi.write(ADDR_CTRL, known)

    # A 9-bit and an 11-bit frame both try to clear CTRL, and both must fail.
    for nbits in (9, 11):
        await spi.write(ADDR_CTRL, 0, nbits=nbits)
        got = await spi.read(ADDR_CTRL)
        assert got == known, f"{nbits}-bit frame was accepted (CTRL={got:#04x})"


@cocotb.test()
async def test_ctrl_locked_while_counting(dut):
    """While counting, a CTRL write updates EN only."""
    spi = await setup(dut, "CTRL locking")

    # TIMEOUT=3 is the longest window, so the dog stays armed across the
    # several SPI frames this test needs.
    start = ctrl_word(en=1, irq_en=1, timeout=3)
    await spi.write(ADDR_CTRL, start)
    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED

    # Try to change TIMEOUT and IRQ_EN while armed: they must be discarded.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=0, timeout=0))
    assert await spi.read(ADDR_CTRL) == start, "TIMEOUT/IRQ_EN changed while armed"

    # EN=0 still lands, and disarms.
    await spi.write(ADDR_CTRL, ctrl_word(en=0, irq_en=1, timeout=0))
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "EN=0 did not disarm"

    # Back in IDLE the full register is writable again.
    new = ctrl_word(en=1, irq_en=0, timeout=1)
    await spi.write(ADDR_CTRL, new)
    assert await spi.read(ADDR_CTRL) == new, "CTRL not writable in IDLE"


@cocotb.test()
async def test_disable_clears_armed(dut):
    """Clearing EN returns the machine to IDLE."""
    spi = await setup(dut, "Disable clears ARMED")

    await spi.write(ADDR_CTRL, ctrl_word(en=1))
    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED

    await spi.write(ADDR_CTRL, ctrl_word(en=0))
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "EN=0 left it armed"


@cocotb.test()
async def test_timeout_fires(dut):
    """A real 2^23 timeout: IRQ asserts, the counter clears, ARMED drops."""
    spi = await setup(dut, "Timeout fires (2^23 clocks, this one is slow)")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()
    assert spi.get_out(IRQ) == 0, "IRQ asserted before the timeout"

    start = cocotb.utils.get_sim_time(unit="ns")
    await wait_for_irq(spi)
    elapsed = cocotb.utils.get_sim_time(unit="ns") - start

    cycles = elapsed / CLK_NS
    want = 2**WD_BASE_EXP
    dut._log.info(f"IRQ fired after ~{cycles:.0f} clocks (expected ~{want})")
    # The kick and the poll granularity add a little slack either side.
    assert want * 0.9 < cycles < want * 1.1, f"timeout was {cycles:.0f} clocks"

    status = await spi.read(ADDR_STATUS)
    assert status & ST_IRQ_FLAG, "IRQ_FLAG not set after timeout"
    assert not status & ST_ARMED, "should return to IDLE after timeout"


@cocotb.test()
async def test_irq_flag_sticky_and_w1c(dut):
    """IRQ_FLAG survives a kick and clears only on a write-1-to-clear."""
    spi = await setup(dut, "IRQ_FLAG stickiness")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()
    await wait_for_irq(spi)

    # A kick re-arms but must not clear the flag.
    await spi.kick()
    status = await spi.read(ADDR_STATUS)
    assert status & ST_IRQ_FLAG, "kick cleared IRQ_FLAG"
    assert status & ST_ARMED, "kick did not re-arm"
    assert spi.get_out(IRQ) == 1, "IRQ deasserted on kick"

    # Writing 0 to STATUS must not clear it either.
    await spi.write(ADDR_STATUS, 0)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "write of 0 cleared the flag"

    # Write 1 clears.
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
    assert not (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "W1C did not clear"
    assert spi.get_out(IRQ) == 0, "IRQ still asserted after W1C"


@cocotb.test()
async def test_irq_en_gates_pin(dut):
    """IRQ_EN gates the pin without affecting the underlying flag."""
    spi = await setup(dut, "IRQ_EN gating")

    # IRQ_EN=0: the flag sets but the pin stays low.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=0, timeout=0))
    await spi.kick()
    await ClockCycles(dut.clk, 2**WD_BASE_EXP + 2**WD_BASE_EXP // 4)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "flag did not set"
    assert spi.get_out(IRQ) == 0, "IRQ pin high while IRQ_EN=0"

    # Enabling IRQ_EN in IDLE exposes the already-set flag.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await ClockCycles(dut.clk, 5)
    assert spi.get_out(IRQ) == 1, "IRQ pin low after enabling IRQ_EN"


@cocotb.test()
async def test_pause_freezes_counter(dut):
    """PAUSE holds the counter, delaying the timeout."""
    spi = await setup(dut, "PAUSE freezes the counter")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()

    # Run part way, then freeze well past the point where it would have fired.
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP - 1))
    spi.set_pin(PAUSE, 1)
    await ClockCycles(dut.clk, 2**WD_BASE_EXP * 2)
    assert spi.get_out(IRQ) == 0, "timeout fired while PAUSE was high"
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "PAUSE should not disarm"

    # Releasing PAUSE lets it finish.
    spi.set_pin(PAUSE, 0)
    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG


@cocotb.test()
async def test_kick_restarts_window(dut):
    """A kick part way through the window restarts it from zero."""
    spi = await setup(dut, "Kick restarts the window")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()

    # Feed at half the window, via the pin: a pin kick is ~12 clocks, where an
    # SPI frame is ~130 and would not fit inside a shortened window.
    for _ in range(3):
        await ClockCycles(dut.clk, 2**(WD_BASE_EXP - 1))
        assert spi.get_out(IRQ) == 0, "fired despite being fed in time"
        await spi.pin_kick()

    # Now stop feeding and let it expire.
    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG
