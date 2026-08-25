`default_nettype none
`timescale 1ns / 1ps

/* Unit-level testbench for spi_regs.
 *
 * Unlike tb.v, which wraps the whole chip and can only observe it through
 * package pins, this one instantiates spi_regs on its own. That exposes the
 * register-port handshake directly, so a test can watch wr_en / wr_addr /
 * wr_data fire instead of inferring a write from its side effects, and can
 * drive rd_data with any pattern rather than only the values the watchdog
 * happens to hold.
 *
 * There is no gate level netlist at this level, so these tests are RTL only.
 *
 * AW and DW are overridable from the Makefile (-P tb_spi.AW=...), so the same
 * tests can sweep frame geometries the chip never uses.
 */
module tb_spi ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb_spi.fst");
    $dumpvars(0, tb_spi);
    #1;
  end

  parameter AW = 2;
  parameter DW = 7;

  localparam FRAME_BITS = 1 + AW + DW;

  // Driven by the test
  reg clk;
  reg rst_n;
  reg sclk;
  reg mosi;
  reg cs_n;
  reg [DW-1:0] rd_data;      // stands in for the whole register file

  // Observed by the test
  wire miso;
  wire wr_en;
  wire [AW-1:0] wr_addr;
  wire [DW-1:0] wr_data;
  wire [AW-1:0] rd_addr;

  spi_regs #(
      .AW(AW),
      .DW(DW)
  ) dut (
      .clk    (clk),
      .rst_n  (rst_n),

      .sclk   (sclk),
      .mosi   (mosi),
      .cs_n   (cs_n),
      .miso   (miso),

      .wr_en  (wr_en),
      .wr_addr(wr_addr),
      .wr_data(wr_data),

      .rd_addr(rd_addr),
      .rd_data(rd_data)
  );

endmodule
