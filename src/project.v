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
  // Input synchronisers
  //
  // SCLK and CS_N cross from the SPI master's clock domain, KICK is a
  // free-running asynchronous pin. All three get two flops; SCLK gets a
  // third so both edges can be recovered by comparing stages 1 and 2.
  // ------------------------------------------------------------------
  reg sclk_sync_0, sclk_sync_1, sclk_sync_2;
  reg cs_n_sync_0, cs_n_sync_1, cs_n_sync_2;
  reg kick_sync_0, kick_sync_1, kick_sync_2;

  always @(posedge clk) begin
    if (!rst_n) begin
      sclk_sync_0 <= 1'b0;
      sclk_sync_1 <= 1'b0;
      sclk_sync_2 <= 1'b0;
      cs_n_sync_0 <= 1'b1;
      cs_n_sync_1 <= 1'b1;
      cs_n_sync_2 <= 1'b1;
      kick_sync_0 <= 1'b0;
      kick_sync_1 <= 1'b0;
      kick_sync_2 <= 1'b0;
    end else begin
      sclk_sync_0 <= sclk;
      sclk_sync_1 <= sclk_sync_0;
      sclk_sync_2 <= sclk_sync_1;
      cs_n_sync_0 <= cs_n;
      cs_n_sync_1 <= cs_n_sync_0;
      cs_n_sync_2 <= cs_n_sync_1;
      kick_sync_0 <= kick_pin;
      kick_sync_1 <= kick_sync_0;
      kick_sync_2 <= kick_sync_1;
    end
  end

  wire cs_n_sync   = cs_n_sync_1;
  wire sclk_rise   = sclk_sync_1 & ~sclk_sync_2;
  wire sclk_fall   = ~sclk_sync_1 & sclk_sync_2;
  wire cs_n_rise   = cs_n_sync_1 & ~cs_n_sync_2;  // end of frame

  wire spi_sample  = sclk_rise & ~cs_n_sync;      // mode 0: latch MOSI here
  wire spi_shift_o = sclk_fall & ~cs_n_sync;      // mode 0: MISO changes here

  wire kick_pin_evt = kick_sync_1 & ~kick_sync_2; // rising edge on KICK pin

  // ------------------------------------------------------------------
  // SPI shift register
  //
  // 10-bit frame, MSB first. The bit counter saturates at 11 so that an
  // over-long frame is distinguishable from an exact one; the frame is
  // only acted on if exactly 10 bits arrived before CS_N went high.
  // ------------------------------------------------------------------
  localparam SPI_BITS = 10;

  reg [SPI_BITS-1:0] spi_rx;
  reg [3:0]          spi_cnt;
  reg [6:0]          spi_tx;

  always @(posedge clk) begin
    if (!rst_n) begin
      spi_rx  <= {SPI_BITS{1'b0}};
      spi_cnt <= 4'd0;
    end else if (cs_n_rise) begin
      spi_cnt <= 4'd0;                            // frame consumed, re-arm
    end else if (spi_sample) begin
      spi_rx <= {spi_rx[SPI_BITS-2:0], mosi};
      if (spi_cnt != 4'd11) spi_cnt <= spi_cnt + 4'd1;  // saturate
    end
  end

  // A frame commits on the CS_N rising edge, and only at exactly 10 bits.
  wire frame_ok = cs_n_rise & (spi_cnt == SPI_BITS);

  // These slices are only meaningful once the whole frame has arrived and
  // every field has shifted into its final position. That is exactly when
  // writes commit (on frame_ok), so the write path can use them directly.
  wire       f_write = ~spi_rx[9];
  wire [1:0] f_addr  =  spi_rx[8:7];
  wire [6:0] f_data  =  spi_rx[6:0];

  // Reads cannot use them. SPI is full duplex, so the master is clocking
  // MISO in while it is still clocking ADDR out: the register value has to
  // be loaded by spi_cnt 3, seven bits before the frame ends. At that point
  // spi_rx[8:7] still holds whatever happens to be passing through, not the
  // address -- it only lands there once the last bit is in.
  //
  // So the read path captures each field off mosi the cycle it arrives,
  // before shifting can carry it away:
  //
  //   spi_cnt 0 -> R/W
  //   spi_cnt 1 -> ADDR[1]
  //   spi_cnt 2 -> ADDR[0]
  reg        rd_req;
  reg  [1:0] rd_addr;
  always @(posedge clk) begin
    if (!rst_n) begin
      rd_req  <= 1'b0;
      rd_addr <= 2'd0;
    end else if (spi_sample) begin
      case (spi_cnt)
        4'd0: rd_req  <= mosi;
        4'd1: rd_addr[1] <= mosi;
        4'd2: rd_addr[0] <= mosi;
        default: ;
      endcase
    end
  end

  localparam ADDR_CTRL = 2'd0, ADDR_KICK = 2'd1, ADDR_STATUS = 2'd2;

  wire wr_ctrl   = frame_ok & f_write & (f_addr == ADDR_CTRL);
  wire wr_kick   = frame_ok & f_write & (f_addr == ADDR_KICK);
  wire wr_status = frame_ok & f_write & (f_addr == ADDR_STATUS);

  // ------------------------------------------------------------------
  // Registers
  // ------------------------------------------------------------------
  reg       en, irq_en;
  reg [1:0] timeout_sel;
  reg       irq_flag;
  reg       armed;          // the state machine: 0 = IDLE, 1 = COUNTING

  wire [6:0] ctrl_rd   = {3'b000, timeout_sel, irq_en, en};
  wire [6:0] status_rd = {5'b0, armed, irq_flag};

  reg [6:0] reg_rd;
  always @(*) begin
    case (rd_addr)
      ADDR_CTRL:   reg_rd = ctrl_rd;
      ADDR_STATUS: reg_rd = status_rd;
      default:     reg_rd = 7'd0;               // KICK and addr 3 read as 0
    endcase
  end

  // KICK: a rising edge on the pin, or an SPI write of 0x5A to addr 1.
  wire kick_evt = kick_pin_evt | (wr_kick & (f_data == 7'h5A));

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
  wire clr_en     = wr_ctrl & ~f_data[0];
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
        en <= f_data[0];
        if (!armed) begin
          irq_en      <= f_data[1];
          timeout_sel <= f_data[3:2];
        end
      end

      // Sticky flag: set by the timeout, cleared only by W1C or rst_n.
      if (do_timeout)
        irq_flag <= 1'b1;
      else if (wr_status && f_data[0])
        irq_flag <= 1'b0;
    end
  end

  // ------------------------------------------------------------------
  // MISO: shifted out MSB first on the SCLK falling edge. The register
  // value is loaded once the address bits have arrived, so it lines up
  // with the 7 data bit positions; MISO reads 0 before that.
  // ------------------------------------------------------------------
  always @(posedge clk) begin
    if (!rst_n) begin
      spi_tx <= 7'd0;
    end else if (cs_n_sync) begin
      spi_tx <= 7'd0;
    end else if (spi_shift_o) begin
      if (spi_cnt == 4'd3)
        spi_tx <= rd_req ? reg_rd : 7'd0;       // R/W + ADDR are in, load
      else
        spi_tx <= {spi_tx[5:0], 1'b0};
    end
  end

  wire miso = spi_tx[6];
  wire irq  = irq_flag & irq_en;

  assign uo_out = {6'b0, irq, miso};

endmodule
