// ---------------------------------------------------------------------------
// Formal wrapper for tt_um_jjy_spi_watchdog (minimal SymbiYosys example)
//
// This file wraps the DUT and observes it through its ports, so nothing in
// src/ has to change.
//
// Three keywords:
//   assume(...)  tell the solver what the inputs are allowed to do
//   assert(...)  demand this holds under every possible input sequence
//   cover(...)   ask the solver to find one input sequence that makes this
//                true, and show it as a waveform
// ---------------------------------------------------------------------------
`default_nettype none

module wdt_formal (
    input wire       clk,
    input wire [7:0] ui_in
);

  // Shrink the timeout to the smallest legal value (2^3 = 8 ticks) so the
  // solver can reach it. Silicon ships 18, which needs 2^18 cycles: far
  // beyond any bounded model check.
  localparam WD_BASE_EXP = 3;

  wire [7:0] uo_out, uio_out, uio_oe;
  wire       rst_n;

  tt_um_jjy_spi_watchdog #(.WD_BASE_EXP(WD_BASE_EXP)) dut (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (8'd0),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk),
      .rst_n  (rst_n)
  );

  // FSM state read back from the debug pins (uo_out[5:3]), same encoding as
  // project.v
  wire [2:0] state     = uo_out[5:3];
  wire       wdt_rst_n = uo_out[2];
  wire       irq       = uo_out[1];
  wire       kick_pin  = ui_in[4];
  wire       cs_n      = ui_in[2];

  localparam IDLE = 3'd0, EARLY = 3'd1, NORMAL = 3'd2, RESET_WAIT = 3'd3, RESET = 3'd4;

  // -------------------------------------------------------------------------
  // 1. Time base and reset convention
  //
  // Formal has no testbench, and at time 0 every register holds an arbitrary
  // value. So count cycles here: cyc=0 forces rst_n low, and rst_n stays high
  // from then on.
  // -------------------------------------------------------------------------
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd2) cyc <= cyc + 2'd1;

  assign rst_n = (cyc != 2'd0);
  // An assign rather than an assume: same effect, easier to read.

  // "KICK has been low and CS_N high ever since reset", i.e. the outside
  // world has done nothing at all.
  reg quiet = 1'b1;
  always @(posedge clk) if (kick_pin || !cs_n) quiet <= 1'b0;

  // -------------------------------------------------------------------------
  // 2. Properties
  //
  // Guard choice follows one rule: what is the earliest cycle at which
  // everything this property reads is meaningful? Properties that only read
  // the current cycle are guarded by rst_n. Properties that read $past need
  // cyc==2, because $past is itself a register and holds pre-reset garbage
  // one cycle earlier.
  // -------------------------------------------------------------------------
  always @(posedge clk) begin

    if (rst_n) begin
      // Invariant: lock is only ever set while EN is 1, so (lock,en) can
      // only be (0,0), (0,1) or (1,1). Nothing else states this, and
      // k-induction has to be told, or it starts from lock=1 with en=0.
      assert (!dut.lock || dut.en);

      // P2: the state encoding is always legal (3 bits, only 5 used)
      assert (state <= RESET);

      // P3: the WDT_RST_N pin is low only in the RESET state
      assert (wdt_rst_n == (state != RESET));

      // P4: with nobody kicking and no SPI traffic, the dog never wakes up
      if (quiet)
        assert (state == IDLE);
    end

    // P1: the first cycle out of reset is always IDLE
    if (cyc == 2'd1)
      assert (state == IDLE);

    if (cyc == 2'd2) begin
      // P5: the FSM only takes edges that exist in the state diagram
      case ($past(state))
        IDLE:       assert (state == IDLE || state == EARLY || state == NORMAL);
        EARLY:      assert (state == IDLE || state == EARLY || state == NORMAL || state == RESET_WAIT);
        NORMAL:     assert (state == IDLE || state == EARLY || state == NORMAL || state == RESET_WAIT);
        RESET_WAIT: assert (state == IDLE || state == RESET_WAIT || state == RESET);
        RESET:      assert (state == IDLE || state == RESET);
        default:    assert (0);
      endcase

      // P6: once LOCK is set, EN cannot be cleared and LOCK cannot be cleared
      // (dut.lock is a hierarchical reference: only the slang frontend takes it)
      if ($past(dut.lock)) begin
        assert (dut.en);
        assert (dut.lock);
      end

`ifdef DEMO_BUG
      // A deliberately wrong property: "IRQ never goes high". It is of course
      // false, and the solver answers with an SPI frame plus a KICK that
      // proves it, which is how you learn to read a counterexample.
      assert (!irq);
`endif
    end

    // -------------------------------------------------------------------------
    // 3. Covers: ask the solver to show how it gets here.
    //
    // Covers stay under cyc==2 whether or not they use $past. A looser guard
    // makes them easier to satisfy, and a cover that is trivial to satisfy
    // proves nothing.
    // -------------------------------------------------------------------------
    if (cyc == 2'd2) begin
      // ---- state coverage: every state is reachable ----
      cover (state == EARLY);
      cover (state == RESET_WAIT);
      cover (state == RESET);

      // ---- transition coverage: every edge P5 allows is really taken ----
      cover ($past(state) == IDLE       && state == EARLY);       // kick, window set
      cover ($past(state) == IDLE       && state == NORMAL);      // kick, no window
      cover ($past(state) == EARLY      && state == NORMAL);      // early window elapsed
      cover ($past(state) == EARLY      && state == RESET_WAIT);  // fed too early
      cover ($past(state) == EARLY      && state == IDLE);        // EN cleared
      cover ($past(state) == NORMAL     && state == EARLY);       // fed normally, back to early window
      cover ($past(state) == NORMAL     && state == RESET_WAIT);  // timeout
      cover ($past(state) == NORMAL     && state == IDLE);        // EN cleared
      cover ($past(state) == RESET_WAIT && state == RESET);       // grace period over, it bites
      cover ($past(state) == RESET_WAIT && state == IDLE);        // escaped by W1C or rst_en=0
      // cover ($past(state) == RESET      && state == IDLE);     // needs 2^19 cycles, out of reach

      cover (!wdt_rst_n);              // all the way to a reset pulse
      cover (irq);                     // IRQ can be raised
      cover (dut.lock);                // LOCK is reachable, so P6 is not vacuous
      cover (irq && (state == IDLE));  // back in IDLE with IRQ still pending
    end
  end

endmodule
