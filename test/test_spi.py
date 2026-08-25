# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the spi_regs module, driven through tb_spi.v.

These complement test.py rather than replacing it. test.py drives the whole
chip through its package pins and so also runs against the gate level netlist;
it can only infer a register write from its side effects. Here spi_regs is
instantiated alone, which buys two things:

  - wr_en / wr_addr / wr_data are observable directly, so "no write happened"
    is checkable instead of merely "CTRL did not change"
  - rd_data is driven by the test, so a read can return any pattern, not only
    the values the watchdog happens to hold

Frame geometry comes from AW/DW so the same tests can sweep widths:

    make -f Makefile.spi
    make -f Makefile.spi AW=3 DW=8

Each test below names the feature ID it covers from docs/test.md. Only the
group A protocol rows live at this level; everything else -- the register
semantics in group B and the watchdog behaviour in C through I -- needs the
full chip and stays in test.py.
"""

import math
import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLK_NS = 20  # 50 MHz, matching the datasheet

# Frame geometry, overridable from the Makefile. Keep these in step with the
# -P tb_spi.AW / -P tb_spi.DW arguments.
AW = int(os.environ.get("AW", 2))
DW = int(os.environ.get("DW", 7))

FRAME_BITS = 1 + AW + DW
DATA_MASK = (1 << DW) - 1
ADDR_MASK = (1 << AW) - 1

# One past the frame length; spi_regs saturates its bit counter here.
CNT_MAX = FRAME_BITS + 1


def frame(rw, addr, data=0):
    """Pack a frame word: [R/W][ADDR:AW][DATA:DW], MSB first."""
    return ((rw & 1) << (AW + DW)) | ((addr & ADDR_MASK) << DW) | (data & DATA_MASK)


class SpiMaster:
    """Bit-banged SPI mode 0 master driving the spi_regs pins directly.

    Unlike the one in test.py there is no ui_in packing here -- sclk, mosi and
    cs_n are separate ports on the DUT, so each is driven on its own.
    """

    def __init__(self, dut):
        self.dut = dut
        self.dut.sclk.value = 0
        self.dut.mosi.value = 0
        self.dut.cs_n.value = 1

    async def _settle(self):
        # spi_regs synchronises sclk and cs_n through three flops, so each
        # phase must be held long enough for the edge to be seen.
        await ClockCycles(self.dut.clk, 4)

    async def xfer(self, word, nbits=None):
        """Clock nbits of `word` out MSB first, returning what MISO sent back.

        nbits defaults to a full frame. Pass something else to build the
        malformed frames A4 and A5 are about.
        """
        if nbits is None:
            nbits = FRAME_BITS

        self.dut.cs_n.value = 0
        await self._settle()

        rx = 0
        for i in range(nbits - 1, -1, -1):
            self.dut.mosi.value = (word >> i) & 1
            await self._settle()
            self.dut.sclk.value = 1  # rising edge: DUT samples MOSI
            await self._settle()
            rx = (rx << 1) | int(self.dut.miso.value)
            self.dut.sclk.value = 0  # falling edge: DUT updates MISO
            await self._settle()

        # Let the last bit land before CS_N rises, so cs_n_rise cannot race
        # the final sample pulse.
        await self._settle()
        self.dut.cs_n.value = 1  # rising edge commits the frame
        await self._settle()
        return rx

    async def write(self, addr, data, nbits=None):
        return await self.xfer(frame(0, addr, data), nbits)

    async def read(self, addr):
        """Return just the DATA field of a read frame."""
        return (await self.read_raw(addr)) & DATA_MASK

    async def read_raw(self, addr):
        """Return the whole MISO word, including the R/W and ADDR positions.
        """
        return await self.xfer(frame(1, addr))

    async def pulse_sclk(self, n=1):
        """Drive n SCLK cycles without touching CS_N.
        """
        for _ in range(n):
            self.dut.sclk.value = 1
            await self._settle()
            self.dut.sclk.value = 0
            await self._settle()


class WriteMonitor:
    """Records every wr_en pulse, with the address and data alongside it.

    This is the whole point of testing spi_regs on its own: test.py can only
    ask "did CTRL change?", which stays silent if a write lands on the wrong
    address. Here an unexpected write is visible directly.

    Start one with `mon = WriteMonitor(dut)` after the clock is running.
    """

    def __init__(self, dut):
        self.dut = dut
        self.writes = []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            if self.dut.wr_en.value == 1:
                self.writes.append(
                    (int(self.dut.wr_addr.value), int(self.dut.wr_data.value))
                )

    def clear(self):
        self.writes = []


async def setup(dut, log="Start"):
    dut._log.info(f"{log}  (AW={AW} DW={DW} FRAME_BITS={FRAME_BITS})")
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())

    spi = SpiMaster(dut)
    dut.rd_data.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    return spi


# ---------------------------------------------------------------------------
# Group A -- SPI protocol
# ---------------------------------------------------------------------------


@cocotb.test()
async def test_a1_mosi_sampled_on_rising_edge(dut):
    """A1: MOSI is sampled on the rising edge of SCLK.
    Send correct data before the rising edge, so it expect to sample the
    right things.
    Then send wrong data before the falling edge, so it
    does not sample the wrong things.
    """
    spi = await setup(dut, "A1 MOSI sampled on rising edge")
    mon = WriteMonitor(dut)

    # a random addr + data with explicit `write` bit (MSB = 0)
    want = 0b0010101101 & ~((1 << FRAME_BITS) - 1)
    disturb = ~want & ~((1 << FRAME_BITS) - 1)

    # pull down CS_N to start
    spi.dut.cs_n.value = 0
    await spi._settle()

    for i in range(FRAME_BITS - 1, -1, -1):
        # write correct data before rising edge
        spi.dut.mosi.value = (want >> i) & 1
        await spi._settle()
        spi.dut.sclk.value = 1
        await spi._settle()
        # write wrong data before falling edge
        spi.dut.mosi.value = (disturb >> i) & 1
        await spi._settle()
        spi.dut.sclk.value = 0
        await spi._settle()

    # pull up CS_N to commit the frame
    await spi._settle()
    spi.dut.cs_n.value = 1
    await spi._settle()

    # check the sent data is correct
    assert len(mon.writes) == 1, f"A1: expected 1 write, got {len(mon.writes)}"
    addr, data = mon.writes[0]
    assert addr == (want >> DW) & ADDR_MASK, f"A1: wr_addr={addr:#04x} != {want >> DW:#04x}"
    assert data == want & DATA_MASK, f"A1: wr_data={data:#04x} != {want & DATA_MASK:#04x}"


@cocotb.test()
async def test_a2_miso_updates_on_falling_edge(dut):
    """A2: MISO changes on the falling edge of SCLK.

    Drive rd_data with a pattern whose bits differ, then check MISO is stable
    across each rising edge and only moves after the falling one.

    Hint: sample dut.miso before and after setting sclk high; they must match.
    """
    spi = await setup(dut, "A2 MISO updates on falling edge")

    want = 0b1010101 & DATA_MASK
    spi.dut.rd_data.value = want

    # pull down CS_N to start
    spi.dut.cs_n.value = 0
    await spi._settle()

    # read request: R/W=1
    spi.dut.mosi.value = 1
    await spi._settle()
    spi.dut.sclk.value = 1
    await spi._settle()
    spi.dut.sclk.value = 0
    await spi._settle()

    # any address, doesn't matter
    for i in range(AW):
        spi.dut.mosi.value = 0
        await spi._settle()
        spi.dut.sclk.value = 1
        await spi._settle()
        spi.dut.sclk.value = 0
        await spi._settle()

    # any mosi, but check miso is stable across the rising edge
    rx = 0
    for i in range(DW):
        spi.dut.mosi.value = 0
        await spi._settle()
        before = spi.dut.miso.value
        spi.dut.sclk.value = 1
        await spi._settle()
        assert spi.dut.miso.value == before, f"A2: MISO changed on rising edge at bit {i}"
        rx = (rx << 1) | int(spi.dut.miso.value)
        spi.dut.sclk.value = 0
        await spi._settle()

    # pull up CS_N to commit the frame
    await spi._settle()
    spi.dut.cs_n.value = 1
    await spi._settle()

    assert rx & DATA_MASK == want, f"A2: MISO={rx:#04x} != {want:#04x}"


@cocotb.test()
async def test_a3_frame_layout_and_direction(dut):
    """A3 + A9: field layout, and the R/W bit selecting write (0) or read (1).

    A9 is folded in here rather than kept separate: its two claims -- a write
    raises wr_en, a read raises none while returning rd_data -- are already
    what the write and read halves below assert. Only the last check is new,
    covering the remaining corner where a write frame must not return data
    either.

    The data patterns are asymmetric on purpose. A value like 0x55 survives a
    one-position shift still looking like a legal value for a neighbouring
    address, which is exactly the bug this test exists to catch.
    """
    spi = await setup(dut, "A3 frame layout")
    mon = WriteMonitor(dut)

    # asymmetric pattern
    data = [(a, (0x4B + a * 0x11) & DATA_MASK) for a in range(1<<AW)]
    # check write succeed
    for addr, value in data:
        await spi.write(addr, value)
    assert mon.writes == data, f"A3: frame layout incorrect: {mon.writes}"
    mon.clear()
    # check read succeed, and not write happened
    for addr, value in data:
        spi.dut.rd_data.value = value
        rd = await spi.read(addr)
        assert rd == value, f"A3: read frame layout incorrect: {rd:#04x} != {value:#04x}"
    assert len(mon.writes) == 0, f"A3: read frame caused a write: {mon.writes}"

    # A9: a write frame must not return data either. rd_req stays low, so tx
    # shifts zeros for the whole frame. Driving rd_data all-ones makes any
    # leak unmistakable -- a sparse pattern could leak through a 0 bit.
    mon.clear()
    spi.dut.rd_data.value = DATA_MASK
    raw = await spi.xfer(frame(0, 0, 0x55 & DATA_MASK))
    assert raw == 0, f"A9: write frame returned {raw:#05x}, expected all zeros"
    assert len(mon.writes) == 1, f"A9: write frame did not commit: {mon.writes}"


@cocotb.test()
async def test_a4_frame_length_enforced(dut):
    """A4 + A5: a frame commits only at exactly FRAME_BITS bits.

    test.py covers FRAME_BITS-1 and FRAME_BITS+1 by checking CTRL did not
    change. Here the stronger claim is checkable: no wr_en pulse at all, at
    any address.

    A5 (the bit counter saturates rather than wrapping) is folded in as one
    more entry in the same sweep. Its failure mode is a frame of
    FRAME_BITS + 2**CNT_W bits aliasing back onto a legal count and committing
    garbage -- a specific length, not a separate scenario.

    The final positive control matters: without it, a helper that silently
    drove nothing at all would satisfy every assertion above.
    """
    spi = await setup(dut, "A4 frame length enforced")
    mon = WriteMonitor(dut)
    data = 0x55 & DATA_MASK

    # Where cnt would wrap without the saturating guard in spi_regs. Computed
    # rather than hard-coded so the AW/DW sweep builds still hit the wrap
    # point: 26 bits at the default geometry, 28 at AW=3 DW=8.
    cnt_w = math.ceil(math.log2(FRAME_BITS + 2))
    alias_len = FRAME_BITS + (1 << cnt_w)

    # Clearing between frames keeps the failure message specific about which
    # length was the one that committed.
    for flen in (0, 1, FRAME_BITS - 1, FRAME_BITS + 1, alias_len):
        mon.clear()
        await spi.write(0, data, flen)
        assert mon.writes == [], f"A4: {flen}-bit frame committed {mon.writes}"

    mon.clear()
    await spi.write(0, data)
    assert mon.writes == [(0, data)], (
        f"A4: {FRAME_BITS}-bit frame did not commit: {mon.writes}"
    )


@cocotb.test()
async def test_a6_miso_zero_during_rw_and_addr(dut):
    """A6: on a read, MISO is 0 during the R/W and ADDR bit positions.

    A datasheet promise that test.py cannot check properly: its read() masks
    those bits off, and the watchdog can never return an all-ones value.

    Drive rd_data with every bit set so any leak into the leading positions is
    unmistakable, then use read_raw() and assert the top 1+AW bits are 0 while
    the DATA field still comes back intact.
    """
    spi = await setup(dut, "A6 MISO zero during R/W and ADDR")
    spi.dut.rd_data.value = DATA_MASK
    data = await spi.read_raw(0)
    assert (data >> DW) == 0, f"A6: MISO leaked into ADDR and R/W: {data:#04x}"
    assert data & DATA_MASK == DATA_MASK, f"A6: data received is wrong"


@cocotb.test()
async def test_a7_sclk_ignored_while_cs_high(dut):
    """A7: SCLK edges arriving while CS_N is high are ignored.

    Both sample and shift_out are qualified by cs_active, so clocking away
    with CS_N high must neither shift the receive register nor commit
    anything.

    Hint: pulse_sclk() a few times before the frame, then run a normal write
    and confirm it still lands correctly -- the stray edges must not have
    displaced the frame's bit alignment.
    """
    spi = await setup(dut, "A7 SCLK ignored while CS_N high")
    mon = WriteMonitor(dut)
    # asymmetric pattern
    data = [(a, (0x4B + a * 0x11) & DATA_MASK) for a in range(1<<AW)]
    # check write succeed even with extra sclk during cs_n=1
    for addr, value in data:
        await spi.pulse_sclk(FRAME_BITS)
        await spi.write(addr, value)
    assert mon.writes == data, f"A3: frame layout incorrect: {mon.writes}"
    mon.clear()
    # check read succeed, and not write happened
    for addr, value in data:
        await spi.pulse_sclk(FRAME_BITS)
        spi.dut.rd_data.value = value
        rd = await spi.read(addr)
        assert rd == value, f"A7: read frame layout incorrect: {rd:#04x} != {value:#04x}"
    assert len(mon.writes) == 0, f"A7: read frame caused a write: {mon.writes}"
