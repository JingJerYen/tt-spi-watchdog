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
// 0 during the R/W and ADDR bits. A write frame is acted on only if exactly
// 1 + AW + DW bits were clocked in before cs_n rose, so partial or overlong
// transfers are discarded rather than half applied.
//
// sclk is sampled rather than used as a clock, so the whole module sits in
// the clk domain and nothing here is timed against sclk. The price is a rate
// limit: each sclk level must last at least two clk periods to be stable, so
// sclk must last 4 times of clk.
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
    output reg           miso,

    // Register write port, valid while wr_en is high
    output wire          wr_en,
    output wire [AW-1:0] wr_addr,
    output wire [DW-1:0] wr_data,

    // Register read port, rd_data must be combinational from rd_addr
    output reg  [AW-1:0] rd_addr,
    input  wire [DW-1:0] rd_data
);

  localparam FRAME_BITS = 1 + AW + DW;
  localparam CNT_W      = $clog2(FRAME_BITS+2); // a slack for overcounts

  // --- Clock Domain Crossing ---
  // Classic 2 stage Flip-Flop synchronizer, with the 3rd Flip-Flop for edge
  // detection for sclk and cs_n. Mosi doesn't require edge detection.
  reg sclk_s0, sclk_s1, sclk_s2;
  reg cs_n_s0, cs_n_s1;
  reg mosi_s0, mosi_s1;

  always @(posedge clk) begin
    if (!rst_n) begin
      sclk_s0 <= 1'b0;
      sclk_s1 <= 1'b0;
      sclk_s2 <= 1'b0;
      cs_n_s0 <= 1'b1;
      cs_n_s1 <= 1'b1;
      mosi_s0 <= 1'b0;
      mosi_s1 <= 1'b0;
    end else begin
      sclk_s0 <= sclk;
      sclk_s1 <= sclk_s0;
      sclk_s2 <= sclk_s1;
      cs_n_s0 <= cs_n;
      cs_n_s1 <= cs_n_s0;
      mosi_s0 <= mosi;
      mosi_s1 <= mosi_s0;
    end
  end

  wire cs_n_active = ~cs_n_s1;
  wire sample = cs_n_active & (~sclk_s2 & sclk_s1); // 0-->1
  wire down = cs_n_active & (sclk_s2 & ~sclk_s1); // 1-->0

  reg [CNT_W-1:0] cnt;
  reg [FRAME_BITS-1:0] rx;

  // shift mosi to rx, MSB first
  always @(posedge clk) begin
    if (!rst_n) begin
      cnt <= 0;
      rx <= 0;
    end else if (!cs_n_active) begin
      cnt <= 0;
    end else if (sample && (cnt <= FRAME_BITS)) begin
      cnt <= cnt + 1;
      rx <= {rx[FRAME_BITS-2:0], mosi_s1};
    end
  end

  // only update when R/W bit comes
  reg is_read;
  always @(posedge clk) begin
    if (!rst_n)
      is_read <= 0;
    else if (sample && (cnt == 0))
      is_read <= mosi_s1;
  end

  // according to cnt, pick the index to miso
  always @(posedge clk) begin
    if (!rst_n)
      miso <= 0;
    else if (!is_read || !cs_n_active)
      miso <= 0;
    else if (down && (cnt > AW) && (cnt < FRAME_BITS))
      miso <= rd_data[DW-1-(cnt-AW-1)];
  end

  // after all mosi data collected, raise wr_en
  assign wr_en = ~cs_n_active & ~is_read & (cnt == FRAME_BITS);
  assign wr_addr = rx[DW+AW-1:DW];
  assign wr_data = rx[DW-1:0];

  // when cnt = 1~AW , shift out the mosi to rd_addr
  always @(posedge clk) begin
    if (!rst_n)
      rd_addr <= 0;
    else if (sample && (cnt >= 1) && (cnt <= AW))
      rd_addr <= {rd_addr[AW-2:0], mosi_s1};
  end

endmodule

`default_nettype wire
