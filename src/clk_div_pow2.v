/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// ---------------------------------------------------------------------------
// Power-of-two clock divider. Outputs a 1-cycle tick, not a new clock.
//
// tick goes high for one clk cycle every 2^sel enabled cycles (on the last
// count of each period). Downstream logic advances on `en & tick`, so all
// logic stays in the clk domain.
//
//   en  - 1: count. 0: pause; the phase is kept, so counting resumes
//         mid-period. en does not gate tick: downstream must AND tick
//         with its own enable.
//   clr - restart the period from zero. Overrides en.
//
// Free-running: tie en = 1, clr = 0 (the counter self-clears on tick).
// sel = 0 gives /1 (tick always 1). Changing sel mid-period may shorten
// that period; assert clr with the change if a clean boundary matters.
// ---------------------------------------------------------------------------
module clk_div_pow2 #(
    // Number of ratio settings: /1 .. /2^(SEL_N-1). SEL_N = 8 gives /1../128.
    parameter SEL_N = 8,

    // Derived; do not override. Parameters (not localparams) only because
    // Verilog-2001 needs SEL_W visible in the port list.
    parameter SEL_W = $clog2(SEL_N),
    parameter CNT_W = SEL_N - 1
) (
    input  wire             clk,
    input  wire             rst_n,

    input  wire [SEL_W-1:0] sel,   // divide by 2^sel
    input  wire             en,    // advance the phase (does not gate tick)
    input  wire             clr,   // restart the period, overrides en

    output wire             tick   // high on the last clock of each period
);

  reg [CNT_W-1:0] cnt;

  // Terminal count = "the low sel bits of cnt are all 1". all_ones[i] means
  // every bit of cnt below i is 1, so all_ones[sel] is the terminal count;
  // this needs no comparator. all_ones[0] = 1 makes sel = 0 divide by 1.
  wire [SEL_N-1:0] all_ones;
  assign all_ones[0] = 1'b1;
  genvar gi;
  generate
    for (gi = 1; gi < SEL_N; gi = gi + 1) begin : g_all_ones
      assign all_ones[gi] = all_ones[gi-1] & cnt[gi-1];
    end
  endgenerate

  assign tick = all_ones[sel];

  always @(posedge clk) begin
    if (!rst_n)    cnt <= {CNT_W{1'b0}};
    else if (clr)  cnt <= {CNT_W{1'b0}};
    else if (en)   cnt <= tick ? {CNT_W{1'b0}} : cnt + 1'b1;
  end

endmodule

`default_nettype wire
