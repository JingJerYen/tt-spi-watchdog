/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// ---------------------------------------------------------------------------
// SPI slave register interface, mode 0 (CPOL=0, CPHA=0).
//
// Frame layout, MSB first, valid only while cs_n is low:
//
//   [ R/W ][ ADDR (AW bits) ][ DATA (DW bits) ]
//
// R/W is 1 for a read. A read returns rd_data in the DATA positions; miso is
// 0 during the R/W and ADDR bits. A frame is acted on only if exactly
// 1 + AW + DW bits were clocked in before cs_n rose, so partial or overlong
// transfers are discarded rather than half applied.
//
// sclk, mosi and cs_n are treated as asynchronous and synchronised here, so
// they may be wired straight to package pins.
//
// sclk is sampled rather than used as a clock, so the whole module sits in
// the clk domain and nothing here is timed against sclk. The price is a rate
// limit: each sclk level must last at least two clk periods to be seen, so
// sclk must stay below roughly clk/4.
//
// The register file itself lives outside this module:
//   - a write appears as wr_en for one clk, with wr_addr / wr_data valid
//   - rd_addr is driven as soon as the address arrives, and rd_data must be
//     combinational from it, ready before the DATA bits are shifted out
// ---------------------------------------------------------------------------
module spi_regs #(
    parameter AW = 2,   // address bits
    parameter DW = 7    // data bits
) (
    input  wire          clk,
    input  wire          rst_n,

    // SPI pins, asynchronous
    input  wire          sclk,
    input  wire          mosi,
    input  wire          cs_n,
    output wire          miso,

    // Register write port, valid while wr_en is high
    output wire          wr_en,
    output wire [AW-1:0] wr_addr,
    output wire [DW-1:0] wr_data,

    // Register read port, rd_data must be combinational from rd_addr
    output reg  [AW-1:0] rd_addr,
    input  wire [DW-1:0] rd_data
);

  localparam FRAME_BITS = 1 + AW + DW;
  localparam CNT_W      = $clog2(FRAME_BITS + 2);
  // One past the frame length, used as a saturating "too long" marker.
  localparam CNT_MAX    = FRAME_BITS + 1;

  // --- Input synchronisers -------------------------------------------------
  // Three stages so both edges of sclk can be recovered by comparing the
  // last two, and so cs_n's edge is seen by the same stage that qualifies
  // sampling (mismatching them races the final shift).
  reg sclk_s0, sclk_s1, sclk_s2;
  reg cs_n_s0, cs_n_s1, cs_n_s2;

  always @(posedge clk) begin
    if (!rst_n) begin
      sclk_s0 <= 1'b0;
      sclk_s1 <= 1'b0;
      sclk_s2 <= 1'b0;
      cs_n_s0 <= 1'b1;
      cs_n_s1 <= 1'b1;
      cs_n_s2 <= 1'b1;
    end else begin
      sclk_s0 <= sclk;
      sclk_s1 <= sclk_s0;
      sclk_s2 <= sclk_s1;
      cs_n_s0 <= cs_n;
      cs_n_s1 <= cs_n_s0;
      cs_n_s2 <= cs_n_s1;
    end
  end

  wire cs_active = ~cs_n_s1;
  wire cs_n_rise = cs_n_s1 & ~cs_n_s2;         // end of frame
  wire sample    = (sclk_s1 & ~sclk_s2) & cs_active;  // rising: latch mosi
  wire shift_out = (~sclk_s1 & sclk_s2) & cs_active;  // falling: update miso

  // --- Receive shift register ----------------------------------------------
  reg [FRAME_BITS-1:0] rx;
  reg [CNT_W-1:0]      cnt;

  always @(posedge clk) begin
    if (!rst_n) begin
      rx  <= {FRAME_BITS{1'b0}};
      cnt <= {CNT_W{1'b0}};
    end else if (cs_n_rise) begin
      cnt <= {CNT_W{1'b0}};                    // frame consumed, re-arm
    end else if (sample) begin
      rx <= {rx[FRAME_BITS-2:0], mosi};
      if (cnt != CNT_MAX[CNT_W-1:0]) cnt <= cnt + 1'b1;  // saturate
    end
  end

  // These slices are only meaningful once every field has shifted into its
  // final position, which is exactly when a write commits.
  wire frame_ok = cs_n_rise & (cnt == FRAME_BITS[CNT_W-1:0]);

  assign wr_en   = frame_ok & ~rx[FRAME_BITS-1];   // R/W low = write
  assign wr_addr = rx[FRAME_BITS-2 -: AW];
  assign wr_data = rx[DW-1:0];

  // --- Read address capture ------------------------------------------------
  // Reads cannot wait for the frame to finish: SPI is full duplex, so the
  // master is clocking miso in while it is still clocking ADDR out. The
  // value has to be loaded before the DATA bits go past, at which point the
  // slices above still hold whatever is passing through. So each field is
  // captured off mosi the cycle it arrives, before shifting carries it away.
  reg rd_req;

  always @(posedge clk) begin
    if (!rst_n) begin
      rd_req  <= 1'b0;
      rd_addr <= {AW{1'b0}};
    end else if (sample) begin
      if (cnt == {CNT_W{1'b0}})
        rd_req <= mosi;                        // first bit is R/W
      else if (cnt <= AW[CNT_W-1:0])
        rd_addr <= {rd_addr[AW-2:0], mosi};    // next AW bits are the address
    end
  end

  // --- Transmit shift register ---------------------------------------------
  // Loaded once R/W and ADDR are in, so it lines up with the DATA positions.
  // Before that it shifts zeros out, which is what the master sees during
  // the R/W and ADDR bits.
  reg [DW-1:0] tx;

  always @(posedge clk) begin
    if (!rst_n) begin
      tx <= {DW{1'b0}};
    end else if (~cs_active) begin
      tx <= {DW{1'b0}};
    end else if (shift_out) begin
      if (cnt == (AW[CNT_W-1:0] + 1'b1))
        tx <= rd_req ? rd_data : {DW{1'b0}};
      else
        tx <= {tx[DW-2:0], 1'b0};
    end
  end

  assign miso = tx[DW-1];

endmodule

`default_nettype wire
