# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the SPI-configurable watchdog timer.

Everything here drives and observes the design through its pins only, so the
same tests run against the gate level netlist. Internal state is checked by
reading the STATUS register back over SPI rather than by peeking at signals.

The shortest timeout is 2^18 clocks, which is far too long to simulate for
every case. Only test_timeout_fires runs a real timeout to completion; the
rest exercise the SPI and control paths, which are timeout independent.
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

CLK_NS = 20  # 50 MHz, matching the datasheet

# The RTL build shrinks the timeout windows so they can be simulated; see
# WD_BASE_EXP in the Makefile. Silicon uses 18. Gate level sim has the real
# netlist, so it falls back to the silicon value.
GATES = os.environ.get("GATES") == "yes"
WD_BASE_EXP = int(os.environ.get("WD_BASE_EXP", 18 if GATES else 8))

# Tests that wait out a real timeout window are RTL only. The netlist carries
# the silicon exponent, so one window is 2**18 clocks and TIMEOUT=111 is 2**28
# -- hours to weeks of wall time at gate level speeds, well past any CI limit.
#
# Nothing is lost by skipping them there. Gate level simulation exists to show
# the netlist still matches the RTL after synthesis and place-and-route, not to
# re-verify function; the tests that stay cover the combinational logic and
# registers, which is what synthesis could actually have broken.
rtl_only = cocotb.test(skip=GATES)

# Pin map
SCLK, MOSI, CS_N, PAUSE, KICK = 0, 1, 2, 3, 4
MISO, IRQ = 0, 1

# Register addresses
ADDR_CTRL, ADDR_KICK, ADDR_STATUS, ADDR_CTRL2 = 0, 1, 2, 3

KICK_MAGIC = 0x5A

# STATUS bits
ST_IRQ_FLAG = 1 << 0
ST_ARMED = 1 << 1
ST_EARLY_FLAG = 1 << 2

# Counter bit offset above WD_BASE_EXP for each TIMEOUT selection. Not a
# straight 0..7: the top three selections step by two so the range reaches
# ~5 s. Must match the case statement in project.v.
SEL_OFFSETS = [0, 1, 2, 3, 4, 6, 8, 10]

# Timeout selections: (CTRL field value, exponent)
TIMEOUTS = [(sel, WD_BASE_EXP + off) for sel, off in enumerate(SEL_OFFSETS)]

# The longest selection, used wherever a test needs the dog to stay armed
# across several SPI frames.
SEL_MAX = 7


# WINDOW selections: (CTRL field value, how far the closed window reaches into
# the timeout window). 00 disables the check; 01/10/11 close the first T/2, T/4
# and T/8. Must match the in_closed decode in project.v.
WINDOWS = [(1, 2), (2, 4), (3, 8)]


# PRESCALER divides the counter clock by 2**sel, so it multiplies every
# TIMEOUT window by the same factor. Must match the ps_tick case in project.v.
PRESCALERS = list(range(8))


def ctrl_word(en=0, irq_en=0, timeout=0, window=0):
    """Pack a CTRL register value: {WINDOW[1:0], TIMEOUT[2:0], IRQ_EN, EN}."""
    return ((en & 1) | ((irq_en & 1) << 1) | ((timeout & 7) << 2)
            | ((window & 3) << 5))


def ctrl2_word(prescaler=0):
    """Pack a CTRL2 register value: {4'b0, PRESCALER[2:0]}."""
    return prescaler & 7


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

    # SEL_MAX is the longest window, so the dog stays armed across the
    # several SPI frames this test needs.
    start = ctrl_word(en=1, irq_en=1, timeout=SEL_MAX)
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


@rtl_only
async def test_timeout_fires(dut):
    """A real 2^18 timeout: IRQ asserts, goes to IDLE, and sets the IRQ_FLAG."""
    spi = await setup(dut, "Timeout fires (2^18 clocks, this one is slow)")

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


@rtl_only
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


@rtl_only
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


@rtl_only
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


@rtl_only
async def test_kick_restarts_window(dut):
    """A kick during counting should restart the counter from zero."""
    spi = await setup(dut, "Kick restarts the window")

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=SEL_MAX))
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
    await wait_for_irq(spi, WD_BASE_EXP + SEL_OFFSETS[SEL_MAX])
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG


@rtl_only
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


@rtl_only
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


@rtl_only
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
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=SEL_MAX))
    await pulse_reset()
    await assert_cleared("IDLE")

    # --- from COUNTING ---
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=SEL_MAX))
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
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=SEL_MAX))
    await spi.write(ADDR_STATUS, ST_ARMED)
    assert not (await spi.read(ADDR_STATUS)) & ST_ARMED, "writing ARMED armed it"

    # From COUNTING: writing it must not disarm either.
    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "kick did not arm"
    await spi.write(ADDR_STATUS, ST_ARMED)
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "writing ARMED disarmed it"

    # And doing so must not have disturbed the flag beside it.
    assert not (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "IRQ_FLAG changed"


@rtl_only
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
    """I1 + I3: uo_out[7:3] stay low, wdt_rst idles high, ui_in[7:5] do nothing.

    I1 guards the output packing in project.v -- a mis-sized concatenation
    would light up a spare pin. uo_out[2] is the active-low wdt_rst, so its
    idle level is 1, not 0. I3 guards the input pin map: driving the
    unused inputs high must change nothing, which a typo'd index would break.

    This one runs at gate level too, deliberately: pin mapping and output
    packing are exactly what place-and-route could disturb, and the checks
    here need no timeout window. The IRQ-asserted sample lives in
    test_unused_pins_under_irq, which does.
    """
    spi = await setup(dut, "I1/I3 unused pins")

    def check_spare_outputs(where):
        spare = int(dut.uo_out.value) >> 3
        assert spare == 0, f"uo_out[7:3] = {spare:#04x} at {where}"
        assert spi.get_out(WDT_RST) == 1, f"wdt_rst not idle-high at {where}"

    check_spare_outputs("reset")

    # Drive the unused inputs high for the rest of the test.
    for bit in (5, 6, 7):
        spi.set_pin(bit, 1)

    # A normal arm cycle must behave exactly as it does elsewhere.
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    assert await spi.read(ADDR_CTRL) == ctrl_word(en=1, irq_en=1, timeout=0), \
        "CTRL readback changed by the unused inputs"
    check_spare_outputs("after CTRL write")

    await spi.kick()
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "kick did not arm"
    check_spare_outputs("armed")


@rtl_only
async def test_unused_pins_under_irq(dut):
    """I1: uo_out[7:3] stay low even while IRQ is asserted.

    Split from test_unused_pins because it needs a real timeout. Sampling the
    spare bits with IRQ high proves they are not simply a bus that happens to
    sit at zero.
    """
    spi = await setup(dut, "I1 unused outputs under IRQ")

    for bit in (5, 6, 7):
        spi.set_pin(bit, 1)

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
    await spi.kick()
    await wait_for_irq(spi)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "timeout did not fire"

    spare = int(dut.uo_out.value) >> 3
    assert spare == 0, f"uo_out[7:3] = {spare:#04x} while IRQ asserted"
    assert spi.get_out(WDT_RST) == 1, "wdt_rst asserted (low) by an IRQ"


@rtl_only
async def test_all_timeout_selections(dut):
    """D1-D8: each TIMEOUT selection fires on its own counter bit.

    All eight values are written and read back by test_ctrl_readback, and the
    case statement in project.v reaches full line coverage because always @(*)
    re-evaluates every branch. But only TIMEOUT=000 had ever been *timed*, so
    swapping two branches of that case would not have failed a single test.

    Each window is measured from the kick to the IRQ. TIMEOUT=111 is 2**18
    clocks with WD_BASE_EXP=8, which is why this is RTL only -- against the
    gate level build, where WD_BASE_EXP is 18, it would never finish.
    """
    spi = await setup(dut, "D1-D8 every timeout selection")

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


# ----------------------------------------------------------------------
# Windowed mode (CTRL[6:5] = WINDOW)
#
# A closed window has to be long enough to land a kick inside it, and the
# whole timeout window has to be short enough to simulate. A pin kick is ~12
# clocks against an SPI frame's ~130, so these tests feed through the pin and
# work at TIMEOUT=2: with WD_BASE_EXP=8 that is T = 2**10 = 1024 clocks, whose
# tightest closed window (T/8) is still 128 clocks wide.
# ----------------------------------------------------------------------
WIN_SEL = 2                       # TIMEOUT selection used by the window tests
WIN_EXP = WD_BASE_EXP + SEL_OFFSETS[WIN_SEL]


async def arm(spi, timeout=WIN_SEL, window=0, irq_en=1, prescaler=0):
    """Configure from IDLE and take the first kick into COUNTING.

    Returns immediately after the kick, with the counter still near 0. The
    caller times its own kick from here, so nothing may be inserted in
    between: an SPI frame is ~130 clocks and would eat a measurable slice of
    a window whose closed part is only 128 clocks wide. That is also why
    ARMED is not checked here -- reading STATUS is exactly the frame we
    cannot afford. The tests that care check it after their own kick.

    The configuration write needs IDLE, so EN is cleared first for callers
    that were counting.
    """
    await spi.write(ADDR_CTRL, 0)
    await spi.write(ADDR_CTRL2, ctrl2_word(prescaler))
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=irq_en, timeout=timeout,
                                         window=window))
    await spi.pin_kick()


@rtl_only
async def test_window_disabled_accepts_any_kick(dut):
    """WINDOW=00: the closed window does not exist, so no kick is ever early.

    The counter is swept across the positions that would be inside the T/2,
    T/4 and T/8 closed windows if any of them were selected. All three must
    simply feed the dog: ARMED stays set, EARLY_FLAG stays clear, and the
    machine never leaves COUNTING.
    """
    spi = await setup(dut, "WINDOW=00 accepts any kick")

    await arm(spi, window=0)

    # Kick at 1/16, 1/8 and 1/4 of the window -- deep inside every closed
    # window the design can select.
    for frac in (16, 8, 4):
        await ClockCycles(dut.clk, 2**WIN_EXP // frac)
        await spi.pin_kick()
        status = await spi.read(ADDR_STATUS)
        assert not status & ST_EARLY_FLAG, f"kick at T/{frac} flagged as early"
        assert status & ST_ARMED, f"kick at T/{frac} left COUNTING"
        assert spi.get_out(IRQ) == 0, f"kick at T/{frac} raised IRQ"

    # Those kicks really were feeds, not no-ops: stop feeding and the dog
    # still times out normally.
    await wait_for_irq(spi, WIN_EXP)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "timeout did not fire"


@rtl_only
async def test_early_kick_faults(dut):
    """A kick inside the closed window sets EARLY_FLAG, disarms and drives IRQ.

    This is the whole point of windowed mode: an early feed is a fault, so it
    must not restart the window. The check after the fault proves exactly
    that -- the machine is back in IDLE with the counter held at 0, so no
    timeout follows however long we wait.
    """
    spi = await setup(dut, "early kick faults")

    # T/2 closed. Kick at a quarter of the window, well inside it.
    await arm(spi, window=1)
    await ClockCycles(dut.clk, 2**WIN_EXP // 4)
    assert spi.get_out(IRQ) == 0, "IRQ up before the early kick"
    await spi.pin_kick()

    status = await spi.read(ADDR_STATUS)
    assert status & ST_EARLY_FLAG, "EARLY_FLAG not set by an early kick"
    assert not status & ST_ARMED, "early kick did not return to IDLE"
    assert not status & ST_IRQ_FLAG, "early kick also set IRQ_FLAG"
    assert spi.get_out(IRQ) == 1, "EARLY_FLAG did not drive IRQ"

    # Back in IDLE with the counter cleared: an early kick must not have
    # restarted the window, so nothing fires no matter how long we wait.
    await ClockCycles(dut.clk, 2**(WIN_EXP + 1))
    status = await spi.read(ADDR_STATUS)
    assert not status & ST_IRQ_FLAG, "a timeout ran on after the early kick"
    assert not status & ST_ARMED, "the early kick restarted the window"

    # W1C clears it and releases the pin.
    await spi.write(ADDR_STATUS, ST_EARLY_FLAG)
    assert not (await spi.read(ADDR_STATUS)) & ST_EARLY_FLAG, "W1C did not clear"
    assert spi.get_out(IRQ) == 0, "IRQ still up after clearing EARLY_FLAG"


@rtl_only
async def test_late_kick_restarts_window(dut):
    """A kick past the closed window is an ordinary feed.

    The mirror image of test_early_kick_faults, and the reason the boundary
    matters: outside the closed part the counter clears, ARMED stays set and
    the full window starts again. Fed repeatedly just past the boundary, the
    dog must never fire.
    """
    spi = await setup(dut, "late kick restarts the window")

    # T/2 closed: feed just after the halfway point, three times over.
    await arm(spi, window=1)
    for i in range(3):
        await ClockCycles(dut.clk, 2**WIN_EXP // 2 + 32)
        status = await spi.read(ADDR_STATUS)
        assert not status & ST_EARLY_FLAG, f"feed {i} in the open half was early"
        assert not status & ST_IRQ_FLAG, f"timed out before feed {i}"
        await spi.pin_kick()
        assert (await spi.read(ADDR_STATUS)) & ST_ARMED, f"feed {i} disarmed"

    # Each of those restarted the window from zero -- so a full window is
    # still ahead of us, and only stopping the feeds ends it.
    await wait_for_irq(spi, WIN_EXP)
    status = await spi.read(ADDR_STATUS)
    assert status & ST_IRQ_FLAG, "the dog never timed out after feeding stopped"
    assert not status & ST_EARLY_FLAG, "a late feed set EARLY_FLAG"


@rtl_only
async def test_first_kick_never_early(dut):
    """The kick that leaves IDLE is never early, at any WINDOW setting.

    In IDLE the counter sits at 0, which is inside every closed window. If the
    check applied there the first kick would fault instead of arming and the
    machine could never start -- so the design gates it on ARMED. All three
    window settings are checked because each decodes a different counter bit,
    and every one of them reads 0 at that moment.
    """
    spi = await setup(dut, "first kick is never early")

    for wsel, frac in WINDOWS:
        # Straight out of reset / IDLE, with no delay before the kick: the
        # counter is at 0, the worst case for the closed-window decode.
        await spi.write(ADDR_CTRL, 0)
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=WIN_SEL,
                                             window=wsel))
        await spi.pin_kick()

        status = await spi.read(ADDR_STATUS)
        assert status & ST_ARMED, f"WINDOW=T/{frac}: first kick did not arm"
        assert not status & ST_EARLY_FLAG, \
            f"WINDOW=T/{frac}: first kick flagged as early"
        assert spi.get_out(IRQ) == 0, f"WINDOW=T/{frac}: first kick raised IRQ"

        # An SPI kick out of IDLE must behave the same way as a pin kick.
        await spi.write(ADDR_CTRL, 0)
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=WIN_SEL,
                                             window=wsel))
        await spi.kick()
        status = await spi.read(ADDR_STATUS)
        assert status & ST_ARMED, f"WINDOW=T/{frac}: first SPI kick did not arm"
        assert not status & ST_EARLY_FLAG, \
            f"WINDOW=T/{frac}: first SPI kick flagged as early"


@rtl_only
async def test_status_flags_clear_independently(dut):
    """The two W1C bits in STATUS are independent of each other.

    They share a register and both drive IRQ, so a W1C built on the whole
    write data rather than on the individual bits would clear both at once and
    still pass every single-flag test. Setting both and clearing them one at a
    time is what separates the two.
    """
    spi = await setup(dut, "IRQ_FLAG and EARLY_FLAG clear independently")

    # Set EARLY_FLAG with a kick inside the closed T/2.
    await arm(spi, window=1)
    await ClockCycles(dut.clk, 2**WIN_EXP // 4)
    await spi.pin_kick()
    assert (await spi.read(ADDR_STATUS)) & ST_EARLY_FLAG, "EARLY_FLAG not set"

    # Set IRQ_FLAG too, by letting a window run out. The early kick left us in
    # IDLE, so re-arm -- with the window off, so the timeout is what fires.
    #
    # wait_for_irq is no use here: EARLY_FLAG already holds the IRQ pin up, so
    # it would return before the timeout had happened. Wait out the window and
    # read the flag itself instead.
    await arm(spi, window=0)
    await ClockCycles(dut.clk, 2**WIN_EXP + 2**WIN_EXP // 4)
    status = await spi.read(ADDR_STATUS)
    assert status & ST_IRQ_FLAG, "IRQ_FLAG not set by the timeout"
    assert status & ST_EARLY_FLAG, "the timeout path cleared EARLY_FLAG"

    # Clear IRQ_FLAG only. EARLY_FLAG survives and holds IRQ up on its own.
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
    status = await spi.read(ADDR_STATUS)
    assert not status & ST_IRQ_FLAG, "IRQ_FLAG W1C did not clear it"
    assert status & ST_EARLY_FLAG, "clearing IRQ_FLAG also cleared EARLY_FLAG"
    assert spi.get_out(IRQ) == 1, "IRQ dropped while EARLY_FLAG was still set"

    # Now the other one, and only then does IRQ let go.
    await spi.write(ADDR_STATUS, ST_EARLY_FLAG)
    assert not (await spi.read(ADDR_STATUS)) & ST_EARLY_FLAG, \
        "EARLY_FLAG W1C did not clear it"
    assert spi.get_out(IRQ) == 0, "IRQ still up with both flags clear"

    # And the same in the other order: EARLY_FLAG first, IRQ_FLAG left behind.
    await arm(spi, window=1)
    await ClockCycles(dut.clk, 2**WIN_EXP // 4)
    await spi.pin_kick()
    await arm(spi, window=0)
    await ClockCycles(dut.clk, 2**WIN_EXP + 2**WIN_EXP // 4)
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, "IRQ_FLAG not set"
    await spi.write(ADDR_STATUS, ST_EARLY_FLAG)
    status = await spi.read(ADDR_STATUS)
    assert not status & ST_EARLY_FLAG, "EARLY_FLAG W1C did not clear it"
    assert status & ST_IRQ_FLAG, "clearing EARLY_FLAG also cleared IRQ_FLAG"
    assert spi.get_out(IRQ) == 1, "IRQ dropped while IRQ_FLAG was still set"


@rtl_only
async def test_window_thresholds(dut):
    """T/2, T/4 and T/8 each close the fraction of the window they name.

    All three settings decode the same way and differ only in which counter
    bit they look at, so nothing above would notice if two of them were
    swapped: a kick at T/16 is early under all three. Each setting is
    therefore probed on both sides of its own boundary -- early just inside,
    a normal feed just outside -- which no other threshold would satisfy.

    The margin either side is a pin kick's own length plus the synchroniser
    depth. Its rising edge is what the design times, and that edge lands a few
    clocks into pin_kick, so the sample point is only approximately where the
    delay put it.
    """
    spi = await setup(dut, "T/2, T/4 and T/8 thresholds")

    margin = 32  # comfortably clear of the ~12 clock pin kick

    for wsel, frac in WINDOWS:
        boundary = 2**WIN_EXP // frac

        # --- inside: just before the boundary, must fault ---
        await arm(spi, window=wsel)
        await ClockCycles(dut.clk, boundary - margin)
        await spi.pin_kick()
        status = await spi.read(ADDR_STATUS)
        assert status & ST_EARLY_FLAG, \
            f"WINDOW=T/{frac}: kick at {boundary - margin} was not early"
        assert not status & ST_ARMED, \
            f"WINDOW=T/{frac}: early kick stayed in COUNTING"
        await spi.write(ADDR_STATUS, ST_EARLY_FLAG | ST_IRQ_FLAG)

        # --- outside: just after the boundary, must feed ---
        await arm(spi, window=wsel)
        await ClockCycles(dut.clk, boundary + margin)
        await spi.pin_kick()
        status = await spi.read(ADDR_STATUS)
        assert not status & ST_EARLY_FLAG, \
            f"WINDOW=T/{frac}: kick at {boundary + margin} was called early"
        assert status & ST_ARMED, \
            f"WINDOW=T/{frac}: kick past the boundary left COUNTING"

        # That feed restarted the window, so the dog is still alive and only
        # runs out once we stop.
        await wait_for_irq(spi, WIN_EXP)
        assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, \
            f"WINDOW=T/{frac}: no timeout after a valid late feed"
        await spi.write(ADDR_STATUS, ST_EARLY_FLAG | ST_IRQ_FLAG)


# ---------------------------------------------------------------------------
# H. PRESCALER
# ---------------------------------------------------------------------------

@cocotb.test()
async def test_ctrl2_readback(dut):
    """H1: CTRL2 holds PRESCALER and reads back; bits 6:3 are unimplemented.

    Every one of the eight settings is written and read back, which is what
    separates a real register from a constant. The last write proves the top
    four bits are not storage: writing 1s there must not change what comes
    back.
    """
    spi = await setup(dut, "H1 CTRL2 readback")

    for sel in PRESCALERS:
        await spi.write(ADDR_CTRL2, ctrl2_word(sel))
        got = await spi.read(ADDR_CTRL2)
        assert got == sel, f"PRESCALER={sel} read back {got}"

    # Bits 6:4 are unimplemented and read as 0, so a full-width write leaves
    # only the low four bits behind.
    await spi.write(ADDR_CTRL2, 0x7F)
    got = await spi.read(ADDR_CTRL2)
    assert got == 0xF, f"CTRL2 unimplemented bits stored, read back {got:#04x}"


@cocotb.test()
async def test_ctrl2_locked_while_counting(dut):
    """H2: CTRL2 is only writable in IDLE, matching CTRL.

    Changing the divider mid-window would move the deadline out from under a
    counter already partway to it, so the write has to be discarded rather
    than deferred.
    """
    spi = await setup(dut, "H2 CTRL2 locking")

    await arm(spi, timeout=SEL_MAX, prescaler=0)
    assert (await spi.read(ADDR_STATUS)) & ST_ARMED, "did not arm"

    await spi.write(ADDR_CTRL2, ctrl2_word(5))
    assert await spi.read(ADDR_CTRL2) == 0, "PRESCALER changed while counting"

    # Back in IDLE it takes again.
    await spi.write(ADDR_CTRL, 0)
    await spi.write(ADDR_CTRL2, ctrl2_word(5))
    assert await spi.read(ADDR_CTRL2) == 5, "PRESCALER not writable in IDLE"


@rtl_only
async def test_prescaler_scales_window(dut):
    """H3: PRESCALER=N multiplies the timeout window by 2**N.

    Each window is measured from the kick to the IRQ, the same way
    test_all_timeout_selections measures the TIMEOUT settings. The half-window
    check before each measurement is what separates one divider from the next;
    without it a window that fired early would still be inside the tolerance
    of a longer one.

    All eight settings fit in simulation at TIMEOUT=000, where the longest is
    2**8 * 128 clocks.
    """
    spi = await setup(dut, "H3 PRESCALER scales the window")

    for sel in PRESCALERS:
        want = 2**WD_BASE_EXP * (2**sel)

        await spi.write(ADDR_CTRL, 0)
        await spi.write(ADDR_STATUS, ST_IRQ_FLAG | ST_EARLY_FLAG)
        assert spi.get_out(IRQ) == 0, f"IRQ still set before PRESCALER={sel}"

        await arm(spi, timeout=0, prescaler=sel)
        start = cocotb.utils.get_sim_time(unit="ns")

        # Half the window must not be enough. A divider that was ignored would
        # fire here, since the undivided window is shorter than half of this
        # one for every sel above 0.
        await ClockCycles(dut.clk, want // 2)
        assert spi.get_out(IRQ) == 0, \
            f"PRESCALER={sel} fired at half of {want} clocks"

        for _ in range(want):
            if spi.get_out(IRQ):
                break
            await ClockCycles(dut.clk, 1)
        assert spi.get_out(IRQ) == 1, \
            f"PRESCALER={sel} never fired within {want} clocks"

        elapsed = (cocotb.utils.get_sim_time(unit="ns") - start) // CLK_NS
        assert 0.9 * want <= elapsed <= 1.15 * want, \
            f"PRESCALER={sel}: {elapsed} clocks, expected about {want}"
        dut._log.info(f"PRESCALER={sel}: fired after ~{elapsed} clocks "
                      f"(want ~{want})")


@rtl_only
async def test_prescaler_scales_closed_window(dut):
    """H4: the divider stretches the closed window along with the full one.

    WINDOW decodes from the same counter the prescaler feeds, so both edges
    have to move together. A kick just past the undivided T/2 boundary lands
    well inside the divided one and must still be called early.
    """
    spi = await setup(dut, "H4 PRESCALER scales the closed window")

    # WINDOW=01 closes the first half. With PRESCALER=2 the whole window is
    # four times longer, so T/2 sits at 2 * 2**WIN_EXP.
    undivided_half = 2**WIN_EXP // 2
    await arm(spi, window=1, prescaler=2)

    # Just past where the closed window would end without the divider.
    await ClockCycles(dut.clk, undivided_half + 64)
    await spi.pin_kick()

    status = await spi.read(ADDR_STATUS)
    assert status & ST_EARLY_FLAG, \
        "closed window did not scale: kick past the undivided T/2 was accepted"
    assert not status & ST_ARMED, "early kick stayed in COUNTING"


@rtl_only
async def test_prescaler_pause_holds_divider(dut):
    """H5: PAUSE freezes the prescaler along with the counter.

    The divider chain has its own state, so it has to stop where the counter
    stops. If it kept running, the first counter step after PAUSE would land
    at an arbitrary point in the divide sequence and the window would come out
    short.
    """
    spi = await setup(dut, "H5 PAUSE holds the divider")

    want = 2**WD_BASE_EXP * 4          # PRESCALER=2
    await arm(spi, timeout=0, prescaler=2)

    await ClockCycles(dut.clk, want // 2)
    spi.set_pin(PAUSE, 1)
    await ClockCycles(dut.clk, want)    # a full window's worth of paused time
    assert spi.get_out(IRQ) == 0, "timeout fired while paused"
    spi.set_pin(PAUSE, 0)

    # The remaining half still has to run before the IRQ appears.
    await ClockCycles(dut.clk, want // 4)
    assert spi.get_out(IRQ) == 0, "window came out short after PAUSE"

    for _ in range(want):
        if spi.get_out(IRQ):
            break
        await ClockCycles(dut.clk, 1)
    assert spi.get_out(IRQ) == 1, "timeout never fired after PAUSE released"


# ---------------------------------------------------------------------------
# Reset output, uo_out[2] -- ACTIVE LOW.
#
# The pin idles high and drops to 0 only in the RESET state, so it can drive
# an external MCU's RESET_N directly. Nothing above ever asserts it;
# test_unused_pins only checks the idle level. These tests set RST_EN and
# check the pulse itself.
#
# The RESET state runs a fixed 2**19 + 1 clocks. That count is hardwired
# rather than scaled by WD_BASE_EXP, so a full pulse is slow however short
# the simulated timeout is. Only test_reset_pulse_length sits through one;
# the rest bail out as soon as they have seen what they check.
# ---------------------------------------------------------------------------
WDT_RST = 2               # uo_out[2], driven by wdt_rst in project.v
RST_EN = 1 << 3           # CTRL2 bit 3

# FSM encoding, for the tests below that watch fsm_state directly.
IDLE, EARLY, NORMAL, RESET_WAIT, RESET = range(5)

# reset_counter starts at 0 and the FSM leaves RESET once bit 19 sets, so the
# state lasts one clock at each value 0 .. 2**19-1 plus one more at 2**19
# while the exit is decoded.
RESET_LEN = 2**19 + 1


async def arm_for_reset(dut, rst_en=1, timeout=0, prescaler=0, irq_en=1):
    """Arm the dog with RST_EN set, so the next timeout walks into RESET.

    CTRL2 carries RST_EN and is writable only in IDLE, so it goes first.
    """
    spi = await setup(dut, f"reset output, RST_EN={rst_en}")
    await spi.write(ADDR_CTRL2,
                    ctrl2_word(prescaler) | (RST_EN if rst_en else 0))
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=irq_en, timeout=timeout))
    await spi.kick()
    return spi


async def run_to_fault(dut, prescaler=0):
    """Advance until the timeout fires and the FSM leaves the window."""
    for _ in range(8 * (2**WD_BASE_EXP) * (2**prescaler)):
        await ClockCycles(dut.clk, 1)
        if int(dut.user_project.fsm_state.value) in (RESET_WAIT, RESET):
            return
    raise AssertionError("timeout never fired")


async def wait_for_reset_state(dut, limit=100):
    """Step from RESET_WAIT into RESET.

    RESET_WAIT is one prescaler tick, so this is a short hop.
    """
    for _ in range(limit):
        if int(dut.user_project.fsm_state.value) == RESET:
            return
        await ClockCycles(dut.clk, 1)
    raise AssertionError("never reached RESET")


@rtl_only
async def test_reset_pin_asserts_on_timeout(dut):
    """R1: with RST_EN set, a timeout drives uo_out[2] low and holds it.

    The core gap: no other test ever sees this pin asserted.
    """
    spi = await arm_for_reset(dut, rst_en=1)

    await run_to_fault(dut)
    await wait_for_reset_state(dut)

    assert spi.get_out(WDT_RST) == 0, \
        "uo_out[2] still high in RESET with RST_EN set"

    # It must stay asserted, not glitch for a single clock.
    for _ in range(1000):
        await ClockCycles(dut.clk, 1)
        assert spi.get_out(WDT_RST) == 0, "uo_out[2] released while in RESET"


@rtl_only
async def test_reset_pin_gated_by_rst_en(dut):
    """R2: RST_EN=0 keeps the pin deasserted (high) through the same timeout.

    This is what test_unused_pins was implicitly relying on. Here it is
    checked deliberately, with the dog actually timing out.
    """
    spi = await arm_for_reset(dut, rst_en=0)

    await run_to_fault(dut)

    # With RST_EN clear, RESET_WAIT falls straight back to IDLE and the FSM
    # never enters RESET, so the pin has no path to go low.
    for _ in range(2000):
        await ClockCycles(dut.clk, 1)
        assert spi.get_out(WDT_RST) == 1, "uo_out[2] asserted with RST_EN clear"
        assert int(dut.user_project.fsm_state.value) != RESET, \
            "entered RESET with RST_EN clear"

    # The timeout still happened -- only the reset output was suppressed.
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, \
        "timeout did not set IRQ_FLAG"


@rtl_only
async def test_reset_pulse_length(dut):
    """R3: the pulse is 2**19 + 1 clocks, then the FSM returns to IDLE.

    The slow one: it sits through a whole pulse. The others short-circuit,
    so this is the only test paying the full cost.
    """
    spi = await arm_for_reset(dut, rst_en=1)

    await run_to_fault(dut)
    await wait_for_reset_state(dut)

    # Bounded, so a stuck-low output fails here instead of hanging the run.
    held = 0
    while not spi.get_out(WDT_RST):
        await ClockCycles(dut.clk, 1)
        held += 1
        assert held <= RESET_LEN + 100, "reset pulse never ended"

    dut._log.info(f"reset pulse held {held} clocks (want {RESET_LEN})")
    assert held == RESET_LEN, f"pulse was {held} clocks, want {RESET_LEN}"
    assert int(dut.user_project.fsm_state.value) == IDLE, "did not return to IDLE"
    assert spi.get_out(WDT_RST) == 1, "uo_out[2] still low after RESET"

    # The flags survive the pulse: the host it just reset can still read why
    # it died, and only a W1C retires the evidence.
    assert (await spi.read(ADDR_STATUS)) & ST_IRQ_FLAG, \
        "IRQ_FLAG lost across the reset pulse"
    assert spi.get_out(IRQ) == 1, "IRQ dropped without a W1C"
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG)
    assert spi.get_out(IRQ) == 0, "W1C did not release IRQ"


@rtl_only
async def test_rst_n_clears_reset_output(dut):
    """R4: rst_n wins over an in-progress reset pulse.

    A watchdog that kept driving its own reset line through a system reset
    would latch the board into a loop.
    """
    spi = await arm_for_reset(dut, rst_en=1)

    await run_to_fault(dut)
    await wait_for_reset_state(dut)
    assert spi.get_out(WDT_RST) == 0, "expected the pin asserted before rst_n"

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    assert spi.get_out(WDT_RST) == 1, "uo_out[2] still asserted after rst_n"
    assert spi.get_out(IRQ) == 0, "IRQ survived rst_n"
    assert int(dut.user_project.fsm_state.value) == IDLE, "not IDLE after rst_n"
    assert await spi.read(ADDR_CTRL2) == 0, "CTRL2 survived rst_n"


@rtl_only
async def test_early_kick_drives_reset(dut):
    """R5: an early kick reaches the reset output, not just the IRQ.

    A too-early kick is a fault like a timeout and takes the same path
    through RESET_WAIT. R1 covers the timeout entry; this covers the other.
    """
    spi = await setup(dut, "early kick drives the reset output")

    # WIN_SEL is wide enough that an SPI frame fits inside the closed half.
    # A pin kick is used for the same reason arm() does: it is ~12 clocks
    # against a frame's ~130.
    await spi.write(ADDR_CTRL2, ctrl2_word(0) | RST_EN)
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=WIN_SEL,
                                         window=1))
    await spi.pin_kick()                      # arms, opens the closed half

    await ClockCycles(dut.clk, 2**WIN_EXP // 4)   # a quarter in, safely early
    await spi.pin_kick()                      # too early -> fault

    await wait_for_reset_state(dut, limit=200)
    assert spi.get_out(WDT_RST) == 0, "early kick did not assert uo_out[2]"


# ---------------------------------------------------------------------------
# FSM directed checks
#
# These watch fsm_state directly, so they are RTL only: the flattened netlist
# has no named state register to probe.
# ---------------------------------------------------------------------------
STATE_NAMES = {0: "IDLE", 1: "EARLY", 2: "NORMAL", 3: "RESET_WAIT", 4: "RESET"}


@rtl_only
async def test_early_then_normal(dut):
    """With WINDOW set, a kick lands in EARLY and the FSM walks into NORMAL."""
    spi = await setup(dut, "EARLY -> NORMAL")
    core = dut.user_project
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=1))
    await spi.kick()
    await ClockCycles(dut.clk, 5)
    st = int(core.fsm_state.value)
    assert st == EARLY, f"after kick expected EARLY, got {STATE_NAMES.get(st)}"
    for _ in range(2**WD_BASE_EXP):
        await ClockCycles(dut.clk, 1)
        if int(core.fsm_state.value) == NORMAL:
            break
    else:
        raise AssertionError("never reached NORMAL")
    dut._log.info("EARLY -> NORMAL transition confirmed")


@rtl_only
async def test_window_disabled_skips_early(dut):
    """WINDOW=0 arms straight into NORMAL and never touches EARLY."""
    spi = await setup(dut, "WINDOW=0 skips EARLY")
    core = dut.user_project
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=0))
    await spi.kick()
    for _ in range(200):
        await ClockCycles(dut.clk, 1)
        assert int(core.fsm_state.value) != EARLY, "entered EARLY with WINDOW=0"
    assert int(core.fsm_state.value) == NORMAL, "should sit in NORMAL"
    dut._log.info("WINDOW=0 bypasses EARLY as intended")


@rtl_only
async def test_kick_in_normal_restarts_early(dut):
    """A feed in NORMAL restarts the window at EARLY."""
    spi = await setup(dut, "feed in NORMAL restarts at EARLY")
    core = dut.user_project
    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=1))
    await spi.kick()
    for _ in range(2**WD_BASE_EXP):
        await ClockCycles(dut.clk, 1)
        if int(core.fsm_state.value) == NORMAL:
            break
    assert int(core.fsm_state.value) == NORMAL
    await spi.kick()
    await ClockCycles(dut.clk, 5)
    st = int(core.fsm_state.value)
    assert st == EARLY, \
        f"feed in NORMAL should restart at EARLY, got {STATE_NAMES.get(st)}"
    dut._log.info("feed in NORMAL restarts the window at EARLY")


@rtl_only
async def test_dropped_transitions_unreachable(dut):
    """The FSM's dropped transitions really are impossible.

    EARLY no longer tests normal_timeout and NORMAL no longer tests
    early_kick. That is only safe if those combinations never occur.
    """
    spi = await setup(dut, "dropped transitions are unreachable")
    core = dut.user_project
    bad = []

    async def watch(n):
        for _ in range(n):
            await ClockCycles(dut.clk, 1)
            st = int(core.fsm_state.value)
            if st == EARLY and int(core.normal_timeout.value):
                bad.append(("normal_timeout in EARLY", cocotb.utils.get_sim_time()))
            if st == NORMAL and int(core.early_kick.value):
                bad.append(("early_kick in NORMAL", cocotb.utils.get_sim_time()))

    for wsel in (1, 2, 3):
        # full window, no kicks: exercises EARLY -> NORMAL -> timeout
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=wsel))
        await spi.kick()
        await watch(3 * 2**WD_BASE_EXP)
        await spi.write(ADDR_STATUS, ST_EARLY_FLAG | ST_IRQ_FLAG)

        # kick early, inside EARLY
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=wsel))
        await spi.kick()
        await watch(20)
        await spi.kick()
        await watch(400)
        await spi.write(ADDR_STATUS, ST_EARLY_FLAG | ST_IRQ_FLAG)

        # kick late, inside NORMAL, repeatedly
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0, window=wsel))
        await spi.kick()
        for _ in range(4):
            await watch(2**WD_BASE_EXP // 2 + 40)
            await spi.kick()
        await watch(300)
        await spi.write(ADDR_STATUS, ST_EARLY_FLAG | ST_IRQ_FLAG)

    if bad:
        dut._log.error(f"{len(bad)} violations: {bad[:5]}")
    assert not bad, f"dropped transition was actually reachable: {bad[:5]}"


# ---------------------------------------------------------------------------
# RESET_WAIT grace period
#
# reset_counter clears on entry, then the FSM leaves once bit WD_BASE_EXP-2
# sets: one clock at each value 0 .. 2**(WD_BASE_EXP-2). The grace period is
# fixed in clk cycles -- neither the prescaler nor PAUSE changes it, and no
# kick aborts it. The one escape is deliberate: a W1C STATUS write landing
# inside the grace period drops the FSM back to IDLE and cancels the reset.
# ---------------------------------------------------------------------------
WAIT_LEN = 2**(WD_BASE_EXP - 2) + 1


@rtl_only
async def test_reset_wait_fixed_length(dut):
    """The grace period is WAIT_LEN clocks for every prescaler setting."""
    for ps in range(4):
        spi = await setup(dut, f"RESET_WAIT length, prescaler={ps}")
        await spi.write(ADDR_CTRL2, ctrl2_word(prescaler=ps) | RST_EN)
        await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=0))
        await spi.kick()
        await run_to_fault(dut, prescaler=ps)

        core = dut.user_project
        held = 0
        while int(core.fsm_state.value) == RESET_WAIT:
            await ClockCycles(dut.clk, 1)
            held += 1
            assert held < 4 * WAIT_LEN, "stuck in RESET_WAIT (lockup)"

        st = int(core.fsm_state.value)
        dut._log.info(f"prescaler={ps}: held {held} clocks, then state={st}")
        assert st == RESET, f"left RESET_WAIT into {STATE_NAMES.get(st)}"
        assert held == WAIT_LEN, f"prescaler={ps}: held {held}, want {WAIT_LEN}"


@rtl_only
async def test_kick_ignored_in_reset_wait(dut):
    """A kick during the grace period neither aborts nor delays the reset.

    Chosen semantics: once the dog has declared a fault, a kick is no longer
    trusted -- a runaway loop that still feeds must not save itself. The
    deliberate escape is the W1C STATUS write, covered by
    test_w1c_aborts_reset_wait. The pin also stays deasserted through the
    grace period: irq warns first, the reset pulse comes only after
    RESET_WAIT expires.
    """
    spi = await arm_for_reset(dut, rst_en=1)
    await run_to_fault(dut)
    core = dut.user_project
    assert int(core.fsm_state.value) == RESET_WAIT, "expected to catch RESET_WAIT"

    await spi.pin_kick()               # ~12 clocks, well inside the grace period

    for _ in range(4 * WAIT_LEN):
        if int(core.fsm_state.value) != RESET_WAIT:
            break
        assert spi.get_out(WDT_RST) == 1, "wdt_rst asserted during the grace period"
        await ClockCycles(dut.clk, 1)
    st = int(core.fsm_state.value)
    assert st == RESET, f"kick diverted RESET_WAIT into {STATE_NAMES.get(st)}"


@rtl_only
async def test_w1c_aborts_reset_wait(dut):
    """A W1C STATUS write landing inside the grace period cancels the reset.

    An SPI frame (~130 clk) is longer than the RTL grace period (65 clk), so
    the frame is launched while the window is still counting, timed so its
    commit (CS_N rising, ~128 clk after launch) lands a few clocks into
    RESET_WAIT. The state watcher proves the commit really fell inside the
    grace period -- if the timing drifts, the test fails rather than passing
    vacuously.
    """
    spi = await arm_for_reset(dut, rst_en=1)   # timeout=0: fault at 2**WD_BASE_EXP
    core = dut.user_project

    # Launch so the commit lands ~20 clocks into the grace period.
    launch_at = 2**WD_BASE_EXP - 106
    while int(core.counter.value) < launch_at:
        await ClockCycles(dut.clk, 1)

    seen = set()

    async def watch_states():
        while True:
            seen.add(int(core.fsm_state.value))
            await ClockCycles(dut.clk, 1)

    watcher = cocotb.start_soon(watch_states())
    await spi.write(ADDR_STATUS, ST_IRQ_FLAG | ST_EARLY_FLAG)
    # The escape is level-based: the write clears the flag registers, and the
    # FSM leaves on the cleared flags one clock later. Give it a few clocks.
    await ClockCycles(dut.clk, 4)
    watcher.kill()

    assert RESET_WAIT in seen, "W1C frame missed the grace period (timing off)"
    assert RESET not in seen, "reset fired before the W1C landed"
    assert int(core.fsm_state.value) == IDLE, "W1C did not abort RESET_WAIT"
    assert spi.get_out(IRQ) == 0, "IRQ still asserted after the abort"

    for _ in range(300):
        await ClockCycles(dut.clk, 1)
        assert spi.get_out(WDT_RST) == 1, "reset pulse fired after the abort"
    assert await spi.read(ADDR_STATUS) == 0, "flags survived the W1C"


@rtl_only
async def test_pause_does_not_stretch_reset_wait(dut):
    """PAUSE freezes windows, not the grace period or the reset itself."""
    spi = await arm_for_reset(dut, rst_en=1)
    await run_to_fault(dut)
    core = dut.user_project
    assert int(core.fsm_state.value) == RESET_WAIT, "expected to catch RESET_WAIT"

    spi.set_pin(PAUSE, 1)
    held = 0
    while int(core.fsm_state.value) == RESET_WAIT:
        await ClockCycles(dut.clk, 1)
        held += 1
        assert held < 4 * WAIT_LEN, "PAUSE froze RESET_WAIT"
    spi.set_pin(PAUSE, 0)

    assert int(core.fsm_state.value) == RESET, "did not proceed to RESET"
    assert held <= WAIT_LEN, f"PAUSE stretched the grace period to {held}"


@rtl_only
async def test_rearm_starts_fresh_window(dut):
    """Disarm mid-window, re-arm: the new window runs its full length.

    The main counter is not cleared on disarm -- it is cleared by the arming
    kick. If that clear were lost, the stale count would make the re-armed
    window fire early. Needs WD_BASE_EXP >= 8 so the SPI disarm frame fits
    inside the window.
    """
    window = 2**(WD_BASE_EXP + 1)     # timeout=1
    spi = await setup(dut, "re-arm starts a fresh window")
    core = dut.user_project

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=1))
    await spi.pin_kick()
    await ClockCycles(dut.clk, 150)
    await spi.write(ADDR_CTRL, ctrl_word(en=0))   # disarm about half-way in
    assert int(core.fsm_state.value) == IDLE, "disarm did not return to IDLE"

    await spi.write(ADDR_CTRL, ctrl_word(en=1, irq_en=1, timeout=1))
    await spi.pin_kick()
    held = 0
    while not spi.get_out(IRQ):
        await ClockCycles(dut.clk, 1)
        held += 1
        assert held < window + 200, "timeout never fired after re-arm"
    assert held > window - 100, \
        f"window only {held} clocks -- stale counter survived re-arm"
