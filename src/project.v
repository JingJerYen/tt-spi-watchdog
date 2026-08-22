/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_spi_watchdog (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // All output pins must be assigned. If not used, assign to 0.
  assign uio_out = 8'b0;
  assign uio_oe  = 8'b0;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in, ui_in[7:5], 1'b0};

  wire sclk    = ui_in[0];
  wire mosi    = ui_in[1];
  wire cs_n    = ui_in[2];
  wire pause   = ui_in[3];
  wire kick_pin = ui_in[4];

  // ------------------------------------------------------------------
  // SPI register interface
  //
  // Frame layout and the exact-length rule live in spi_regs; this module
  // only sees committed register writes and a combinational read port.
  // ------------------------------------------------------------------
  wire       wr_en;
  wire [1:0] wr_addr;
  wire [6:0] wr_data;
  wire [1:0] rd_addr;
  reg  [6:0] rd_data;
  wire       miso;

  spi_regs #(
      .AW(2),
      .DW(7)
  ) u_spi (
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

  // KICK is a free-running asynchronous pin, so it needs its own
  // synchroniser and edge detector.
  reg kick_s0, kick_s1, kick_s2;
  always @(posedge clk) begin
    if (!rst_n) begin
      kick_s0 <= 1'b0;
      kick_s1 <= 1'b0;
      kick_s2 <= 1'b0;
    end else begin
      kick_s0 <= kick_pin;
      kick_s1 <= kick_s0;
      kick_s2 <= kick_s1;
    end
  end
  wire kick_pin_evt = kick_s1 & ~kick_s2;

  localparam ADDR_CTRL = 2'd0, ADDR_KICK = 2'd1, ADDR_STATUS = 2'd2;

  wire wr_ctrl   = wr_en & (wr_addr == ADDR_CTRL);
  wire wr_kick   = wr_en & (wr_addr == ADDR_KICK);
  wire wr_status = wr_en & (wr_addr == ADDR_STATUS);

  // ------------------------------------------------------------------
  // Registers
  // ------------------------------------------------------------------
  reg       en, irq_en;
  reg [1:0] timeout_sel;
  reg       irq_flag;
  reg       armed;          // the state machine: 0 = IDLE, 1 = COUNTING

  wire [6:0] ctrl_rd   = {3'b000, timeout_sel, irq_en, en};
  wire [6:0] status_rd = {5'b0, armed, irq_flag};

  always @(*) begin
    case (rd_addr)
      ADDR_CTRL:   rd_data = ctrl_rd;
      ADDR_STATUS: rd_data = status_rd;
      default:     rd_data = 7'd0;              // KICK and addr 3 read as 0
    endcase
  end

  // KICK: a rising edge on the pin, or an SPI write of 0x5A to addr 1.
  wire kick_evt = kick_pin_evt | (wr_kick & (wr_data == 7'h5A));

  // ------------------------------------------------------------------
  // Watchdog counter
  //
  // Timeout fires on the rising edge of counter bit
  // WD_BASE_EXP + 2*TIMEOUT. Selecting a bit rather than doing a
  // full-width magnitude compare keeps this to a 4:1 mux.
  //
  // WD_BASE_EXP is 23 in silicon, giving the 168 ms .. 10.7 s range in
  // the datasheet. A testbench can override it to shrink the windows to
  // something simulatable; nothing else in the design depends on it.
  // ------------------------------------------------------------------
`ifndef WD_BASE_EXP
  `define WD_BASE_EXP 23
`endif
  localparam WD_BASE_EXP = `WD_BASE_EXP;
  localparam CNT_W       = WD_BASE_EXP + 7;

  reg [CNT_W-1:0] counter;

  reg timeout_bit;
  always @(*) begin
    case (timeout_sel)
      2'd0:    timeout_bit = counter[WD_BASE_EXP];
      2'd1:    timeout_bit = counter[WD_BASE_EXP + 2];
      2'd2:    timeout_bit = counter[WD_BASE_EXP + 4];
      default: timeout_bit = counter[WD_BASE_EXP + 6];
    endcase
  end

  // A CTRL write clearing EN must disarm in the same cycle it lands,
  // otherwise ARMED reads back stale for one clock.
  wire clr_en     = wr_ctrl & ~wr_data[0];
  wire en_now     = en & ~clr_en;

  // KICK wins over the timeout, matching the PAUSE priority rule.
  wire do_kick    = kick_evt & en_now;
  wire do_timeout = armed & timeout_bit & ~do_kick;

  always @(posedge clk) begin
    if (!rst_n) begin
      counter <= {CNT_W{1'b0}};
      armed   <= 1'b0;
    end else if (!en_now) begin
      counter <= {CNT_W{1'b0}};                 // disarmed: held in IDLE
      armed   <= 1'b0;
    end else if (do_kick) begin
      counter <= {CNT_W{1'b0}};                 // feed: restart the window
      armed   <= 1'b1;
    end else if (do_timeout) begin
      counter <= {CNT_W{1'b0}};                 // fire, then back to IDLE
      armed   <= 1'b0;
    end else if (armed && !pause) begin
      counter <= counter + 1'b1;
    end
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      en          <= 1'b0;
      irq_en      <= 1'b0;
      timeout_sel <= 2'd0;
      irq_flag    <= 1'b0;
    end else begin
      // CTRL is only fully writable in IDLE; while counting, EN alone lands.
      if (wr_ctrl) begin
        en <= wr_data[0];
        if (!armed) begin
          irq_en      <= wr_data[1];
          timeout_sel <= wr_data[3:2];
        end
      end

      // Sticky flag: set by the timeout, cleared only by W1C or rst_n.
      if (do_timeout)
        irq_flag <= 1'b1;
      else if (wr_status && wr_data[0])
        irq_flag <= 1'b0;
    end
  end

  wire irq  = irq_flag & irq_en;

  assign uo_out = {6'b0, irq, miso};

endmodule
