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

    # A frame with the wrong number of bits tries to clear CTRL, and must fail.
    for nbits in (0, 9, 11, 26):
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
    """A real 2^23 timeout: IRQ asserts, goes to IDLE, and sets the IRQ_FLAG."""
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
    """After a timeout, the IRQ_FLAG is sticky until cleared by writing 1"""
    spi = await setup(dut, "IRQ_FLAG stickiness")

    # timeout, goes to IDLE state
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
    assert spi.get_out(IRQ) == 1, "IRQ deasserted on kick"

    # Write 1 clears IRQ_FLAG
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
    assert not (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "W1C did not clear"
    assert spi.get_out(IRQ) == 0, "IRQ still asserted after W1C"


@cocotb.test()
async def test_irq_en_gates_pin(dut):
    """IRQ_EN gates the IRQ output pin, but the flag still sets."""
    spi = await setup(dut, "IRQ_EN gating")

    # IRQ_EN=0: the flag sets but the IRQ pin stays low.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=0, timeout=0))
    await spi.kick()
    await ClockCycles(dut.clk, 2**WD_BASE_EXP + 2**WD_BASE_EXP // 4)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "flag did not set"
    assert spi.get_out(IRQ) == 0, "IRQ pin high while IRQ_EN=0"

    # Enabling IRQ_EN in IDLE exposes the already-set flag.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await ClockCycles(dut.clk, 5)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "flag did not set"
    assert spi.get_out(IRQ) == 1, "IRQ pin low after enabling IRQ_EN"


@cocotb.test()
async def test_pause_freezes_counter(dut):
    """PAUSE holds the counter, delaying the timeout."""
    spi = await setup(dut, "PAUSE freezes the counter")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()

    # Run for half the timeout limit, pause, then run for twice the timeout
    # limit, it should not fire IRQ because pause stops the counter.
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
    """A kick during counting should restart the counter from zero."""
    spi = await setup(dut, "Kick restarts the window")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=3))
    await spi.kick()

    # Feed at half the window, via the pin: a pin kick is ~12 clocks, where an
    # SPI frame is ~130 and would not fit inside a shortened window.
    for _ in range(3):
        await ClockCycles(dut.clk, 2**(WD_BASE_EXP - 1))
        assert spi.get_out(IRQ) == 0, "fired despite being fed in time"
        await spi.pin_kick()

    # Same, but kick with register
    for _ in range(3):
        await ClockCycles(dut.clk, 2**(WD_BASE_EXP - 1))
        assert spi.get_out(IRQ) == 0, "fired despite being fed in time"
        await spi.kick()

    # Now stop feeding and let it expire.
    await wait_for_irq(spi, WD_BASE_EXP + 6)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG


@cocotb.test()
async def test_kick_is_not_level_trigger(dut):
    """same as kick_restarts_window, but not pull down kick pin, so
    it should always timeout
    """
    spi = await setup(dut, "Kick is not level triggered")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))

    # kick pin maintains at high
    spi.set_pin(KICK, 1)
    await ClockCycles(spi.dut.clk, 6)
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "the rising edge did not arm"

    # time limit
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP))
    assert spi.get_out(IRQ) == 1, "level kick = 1 still feed dog"

    # Now stop feeding and let it expire.
    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG


@cocotb.test()
async def test_kick_is_prior_to_pause(dut):
    """If kick and pause comes at the same cycle, kick wins"""
    spi = await setup(dut, "Kick is prior to pause")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.pin_kick()

    # half time window
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP-1))
    # kick and pause at same time, kick wins so counter restart from 0
    # but since pause is still held, so counter freezes at 0
    spi.set_pin(KICK, 1)
    spi.set_pin(PAUSE, 1)

    # half and more time, since it paused, counter stays at 0
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP))
    assert spi.get_out(IRQ) == 0, "pause not work"

    # cancel pause
    spi.set_pin(PAUSE, 0)

    # now total is half+slack < timeout, so no irq
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP-1)+10)
    assert spi.get_out(IRQ) == 0, "kick wins pause, so half+slack should not trigger irq"

    # now total is timeout+slack > timeout, so fire irq
    await ClockCycles(dut.clk, 2**(WD_BASE_EXP-1))
    assert spi.get_out(IRQ) == 1, "now total is timeout+slack, should fire irq"


@cocotb.test()
async def test_reset_from_any_state(dut):
    """C6: rst_n returns to IDLE from any state.

    test_reset_state only covers the power-up values. Reset is applied here
    from IDLE, from COUNTING, and from IDLE with IRQ_FLAG already sticky. The
    last is the only path that proves reset clears the flag: a W1C write is
    the only other way to clear it, so nothing else exercises that reset.
    """
    spi = await setup(dut, "reset from any state")

    async def pulse_reset():
        """Hold rst_n low long enough to take, then release and settle.

        A single clk of reset is not enough -- the value has to propagate, and
        the outputs need time to settle before they are read. Reads must also
        wait for the release: while rst_n is low spi_regs holds rx and cnt
        cleared, so no frame is received and read() returns 0 no matter what
        the registers actually hold.
        """
        spi.dut.rst_n.value = 0
        await ClockCycles(dut.clk, 10)
        spi.dut.rst_n.value = 1
        await ClockCycles(dut.clk, 5)

    async def assert_cleared(where):
        assert spi.get_out(IRQ) == 0, f"IRQ still set after reset from {where}"
        assert await spi.read(ADDR_CTRL) == 0, f"CTRL not cleared from {where}"
        assert await spi.read(ADDR_STATUS) == 0, f"STATUS not cleared from {where}"

    # --- from IDLE ---
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=2))
    await pulse_reset()
    await assert_cleared("IDLE")

    # --- from COUNTING ---
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=2))
    await spi.pin_kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "did not arm"
    await pulse_reset()
    await assert_cleared("COUNTING")

    # --- from IDLE with IRQ_FLAG sticky ---
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.pin_kick()
    await wait_for_irq(spi)
    # Positive control: without it the final check would be clearing a flag
    # that was never set in the first place.
    assert spi.get_out(IRQ) == 1, "IRQ did not fire"
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "IRQ_FLAG not set"
    await pulse_reset()
    await assert_cleared("IDLE with IRQ_FLAG set")


@cocotb.test()
async def test_status_armed_is_read_only(dut):
    """B6: STATUS bit 1 (ARMED) is read-only; writes to it are ignored.

    Only wr_data[0] takes part in the W1C path, so writing 0x02 must leave
    ARMED alone in either direction: it cannot arm the watchdog from IDLE, and
    it cannot disarm it while counting.
    """
    spi = await setup(dut, "B6 ARMED is read-only")

    # From IDLE: writing the ARMED bit must not arm anything.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=3))
    await spi.write(ADDR_STATUS, ST_ARMED)
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "writing ARMED armed it"

    # From COUNTING: writing it must not disarm either.
    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "kick did not arm"
    await spi.write(ADDR_STATUS, ST_ARMED)
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "writing ARMED disarmed it"

    # And doing so must not have disturbed the flag beside it.
    assert not (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "IRQ_FLAG changed"


@cocotb.test()
async def test_irq_en_release_keeps_flag(dut):
    """G5: clearing IRQ_EN releases the IRQ pin but retains IRQ_FLAG.

    test_irq_en_gates_pin only goes from IRQ_EN=0 to 1. The reverse matters
    because IRQ is combinational from both: dropping IRQ_EN must take the pin
    with it while leaving the sticky flag readable, so a later re-enable
    surfaces the same pending interrupt.

    Note CTRL is locked while counting, so IRQ_EN can only be changed once the
    timeout has returned the machine to IDLE.
    """
    spi = await setup(dut, "G5 IRQ_EN release keeps the flag")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()
    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "IRQ_FLAG not set"

    # The timeout returned us to IDLE, so the whole of CTRL is writable again.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=0, timeout=0))
    assert spi.get_out(IRQ) == 0, "IRQ pin still high after clearing IRQ_EN"
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, \
        "clearing IRQ_EN cleared the flag"

    # Re-enabling surfaces the same pending flag; only a W1C clears it.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    assert spi.get_out(IRQ) == 1, "IRQ pin did not return with IRQ_EN"
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
    assert spi.get_out(IRQ) == 0, "W1C did not clear IRQ"


@cocotb.test()
async def test_unused_pins(dut):
    """I1 + I3: uo_out[7:2] stay low, and ui_in[7:5] affect nothing.

    I1 guards the output packing in project.v -- a mis-sized concatenation
    would light up a spare pin. I3 guards the input pin map: driving the
    unused inputs high must change nothing, which a typo'd index would break.

    Both are folded into one test since neither needs a scenario of its own,
    only a run of ordinary traffic to observe.
    """
    spi = await setup(dut, "I1/I3 unused pins")

    def check_spare_outputs(where):
        spare = int(dut.uo_out.value) >> 2
        assert spare == 0, f"uo_out[7:2] = {spare:#04x} at {where}"

    check_spare_outputs("reset")

    # Drive the unused inputs high for the rest of the test.
    for bit in (5, 6, 7):
        spi.set_pin(bit, 1)

    # A normal arm-and-fire cycle must behave exactly as it does elsewhere.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    assert await spi.read(ADDR_CTRL) == ctrl_word(en=1, irq_en=1, timeout=0), \
        "CTRL readback changed by the unused inputs"
    check_spare_outputs("after CTRL write")

    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "kick did not arm"
    check_spare_outputs("armed")

    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "timeout did not fire"
    # IRQ is live here, so this also proves the spare bits are not just a
    # copy of a stuck-low bus.
    check_spare_outputs("IRQ asserted")


@cocotb.test()
async def test_all_timeout_selections(dut):
    """D1-D4: each TIMEOUT selection fires on its own counter bit.

    All four values are written and read back by test_ctrl_readback, and the
    case statement in project.v reaches full line coverage because always @(*)
    re-evaluates every branch. But only TIMEOUT=00 had ever been *timed*, so
    swapping two branches of that case would not have failed a single test.

    Each window is measured from the kick to the IRQ. TIMEOUT=11 is 2**14
    clocks with WD_BASE_EXP=8, which is why this is RTL only -- against the
    gate level build, where WD_BASE_EXP is 23, it would never finish.
    """
    spi = await setup(dut, "D1-D4 every timeout selection")

    for sel, exp in TIMEOUTS:
        # Reconfiguring needs IDLE: CTRL is locked to EN alone while counting.
        await spi.write(ADDR_CTRL, 0)
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=sel))
        assert await spi.read(ADDR_CTRL) == ctrl_word(en=1, irq_en=1, timeout=sel), \
            f"TIMEOUT={sel} did not take"

        # Clear any flag left by the previous selection, so the poll below
        # cannot see a stale one.
        await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
        assert spi.get_out(IRQ) == 0, f"IRQ still set before TIMEOUT={sel}"

        await spi.kick()
        start = cocotb.utils.get_sim_time(unit="ns")

        # Half the window must not be enough -- this is what separates one
        # selection from the next, and what a swapped case branch would break.
        await ClockCycles(dut.clk, 2**(exp - 1))
        assert spi.get_out(IRQ) == 0, \
            f"TIMEOUT={sel} fired at half of 2^{exp}: window is too short"

        # Poll out the remainder rather than calling wait_for_irq, which would
        # wait a further full window from here and inflate the measurement.
        for _ in range(2**exp):
            if spi.get_out(IRQ):
                break
            await ClockCycles(dut.clk, 1)
        else:
            raise AssertionError(f"TIMEOUT={sel} never fired around 2^{exp}")
        cycles = (cocotb.utils.get_sim_time(unit="ns") - start) / CLK_NS

        want = 2**exp
        dut._log.info(f"TIMEOUT={sel}: fired after ~{cycles:.0f} clocks (want ~{want})")
        assert want * 0.9 < cycles < want * 1.1, \
            f"TIMEOUT={sel} fired after {cycles:.0f} clocks, wanted ~{want}"
