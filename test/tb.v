`default_nettype none
`timescale 1ns / 1ps

/* This testbench just instantiates the module and makes some convenient wires
   that can be driven / tested by the cocotb test.py.
*/
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  // Wire up the inputs and outputs:
  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
`ifdef GL_TEST
  wire VPWR = 1'b1;
  wire VGND = 1'b0;
`endif

  // Shrink the timeout windows so they are simulatable: 2**8 clocks rather
  // than the silicon 2**18, keeping the same structure at a length a
  // simulator can reach.
  //
  // Keep this in step with WD_BASE_EXP in the Makefile, which passes the same
  // number to Python. If they disagree, every timeout measurement is wrong.
  //
  // Gate level does not use this. That netlist was hardened with the silicon
  // default and has no parameter left to override, so the instantiation below
  // is compiled without it and the windows stay full length -- which is why
  // the tests that wait one out are skipped when GATES=yes. See rtl_only in
  // test.py.
  parameter WD_BASE_EXP = 8;

`ifdef GL_TEST
  tt_um_spi_watchdog user_project (
`else
  tt_um_spi_watchdog #(
      .WD_BASE_EXP(WD_BASE_EXP)
  ) user_project (
`endif

      // Include power ports for the Gate Level test:
`ifdef GL_TEST
      .VPWR(VPWR),
      .VGND(VGND),
`endif

      .ui_in  (ui_in),    // Dedicated inputs
      .uo_out (uo_out),   // Dedicated outputs
      .uio_in (uio_in),   // IOs: Input path
      .uio_out(uio_out),  // IOs: Output path
      .uio_oe (uio_oe),   // IOs: Enable path (active high: 0=input, 1=output)
      .ena    (ena),      // enable - goes high when design is selected
      .clk    (clk),      // clock
      .rst_n  (rst_n)     // not reset
  );

endmodule
