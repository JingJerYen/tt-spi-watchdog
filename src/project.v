/*
 * Copyright (c) 2024 Your Name
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_spi_watchdog #(
    // Timeout base exponent. The eight settings span 2^18 .. 2^28 clocks.
    // Testbenches lower it to shorten simulation.
    // Minimum is 3: the closed-window index reaches WD_BASE_EXP - 3.
    // Tiny Tapeout does not override parameters, so 18 is what ships.
    parameter WD_BASE_EXP = 18
) (
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

  // Tie off unused inputs to avoid warnings
  wire _unused = &{ena, uio_in, ui_in[7:5], 1'b0};

  wire sclk    = ui_in[0];
  wire mosi    = ui_in[1];
  wire cs_n    = ui_in[2];
  wire pause   = ui_in[3];
  wire kick_pin = ui_in[4];

  // ------------------------------------------------------------------
  // SPI register interface
  //
  // spi_regs handles the frame format and length rule, and hands this module
  // finished writes plus a read port.
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

  // KICK is asynchronous. Synchronise it, then detect the rising edge.
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

  localparam ADDR_CTRL = 2'd0, ADDR_KICK = 2'd1, ADDR_STATUS = 2'd2, ADDR_CTRL2 = 2'd3;

  wire wr_ctrl   = wr_en & (wr_addr == ADDR_CTRL);
  wire wr_kick   = wr_en & (wr_addr == ADDR_KICK);
  wire wr_status = wr_en & (wr_addr == ADDR_STATUS);
  wire wr_ctrl2  = wr_en & (wr_addr == ADDR_CTRL2);

  // ------------------------------------------------------------------
  // Registers
  // ------------------------------------------------------------------
  reg       en, irq_en, rst_en;
  reg [2:0] timeout_sel;
  reg [1:0] window_sel;
  reg [2:0] prescaler;
  reg       irq_flag;
  reg       early_flag;

  // A window runs CLOSED then OPEN. A kick in CLOSED is too early; a kick in
  // OPEN is a normal feed. Splitting them means the state says which half we
  // are in, instead of a comparison buried in the kick logic.
  reg [2:0] fsm_state;
  reg [2:0] nxt_state;
  localparam IDLE       = 3'd0;
  localparam CLOSED     = 3'd1;
  localparam OPEN       = 3'd2;
  localparam RESET_WAIT = 3'd3;
  localparam RESET      = 3'd4;

  reg [19:0] reset_counter; // 2^19 cycles @ 50MHz ~= 10.486 ms

  wire closed   = (fsm_state == CLOSED);
  wire open_win = (fsm_state == OPEN);
  wire counting = closed | open_win;      // a window is running
  wire waiting  = (fsm_state == RESET_WAIT);

  wire [6:0] ctrl_rd   = {window_sel, timeout_sel, irq_en, en};
  wire [6:0] status_rd = {4'b0, early_flag, counting, irq_flag};
  wire [6:0] ctrl2_rd  = {3'b0, rst_en, prescaler};

  always @(*) begin
    case (rd_addr)
      ADDR_CTRL:   rd_data = ctrl_rd;
      ADDR_STATUS: rd_data = status_rd;
      ADDR_CTRL2:  rd_data = ctrl2_rd;
      default:     rd_data = 7'd0;              // KICK reads back 0
    endcase
  end

  // A kick is a rising edge on the KICK pin, or an SPI write of 0x5A
  // to address 1.
  wire kick_evt = kick_pin_evt | (wr_kick & (wr_data == 7'h5A));

  // ------------------------------------------------------------------
  // Watchdog counter
  //
  // Timeout fires when the selected counter bit goes 0 -> 1, so every window
  // is a power of two and selecting one costs a single mux.
  //
  // Bit offsets are 0,1,2,3,4,6,8,10. The top three step by 2, stretching the
  // range to 2^28 clocks without needing 16 settings.
  // ------------------------------------------------------------------
  localparam CNT_W = WD_BASE_EXP + 11;

  reg [CNT_W-1:0] counter;

  // Offset of the timeout bit above WD_BASE_EXP. Both window edges read it.
  reg [3:0] hi_off;
  always @(*) begin
    case (timeout_sel)
      3'd0:    hi_off = 4'd0;
      3'd1:    hi_off = 4'd1;
      3'd2:    hi_off = 4'd2;
      3'd3:    hi_off = 4'd3;
      3'd4:    hi_off = 4'd4;
      3'd5:    hi_off = 4'd6;
      3'd6:    hi_off = 4'd8;
      default: hi_off = 4'd10;
    endcase
  end

  // WD_BASE_EXP is a parameter, so this index folds into a mux.
  wire timeout_bit = counter[WD_BASE_EXP + hi_off];

  // WINDOW picks a threshold 1, 2 or 3 bits below the timeout bit, closing
  // the first half, quarter or eighth of the window.
  //
  // above[i] = OR of counter[i] and every bit above it. It stays 0 until the
  // counter reaches bit i, then stays 1 for the rest of the window.
  // in_closed reads the entry at the threshold: 1 means the counter has not
  // reached it yet.
  //
  // Cost: one OR chain plus a mux.
  wire [CNT_W-1:0] above;
  assign above[CNT_W-1] = counter[CNT_W-1];
  genvar gi;
  generate
    for (gi = CNT_W - 2; gi >= 0; gi = gi - 1) begin : g_above
      assign above[gi] = counter[gi] | above[gi+1];
    end
  endgenerate

  // WINDOW = 0 disables the closed half entirely.
  wire has_closed = (window_sel != 2'd0);

  wire in_closed = has_closed &
                   ~above[WD_BASE_EXP + {28'd0, hi_off} - {30'd0, window_sel}];

  // en updates on the next clock. clr_en forwards a CTRL write that clears
  // EN, so en_now disarms the dog in the same clock the write lands.
  wire clr_en     = wr_ctrl & ~wr_data[0];
  wire en_now     = en & ~clr_en;

  // The state decides what a kick means: in CLOSED it is too early, anywhere
  // else it feeds. No ordering terms are needed -- the two cannot both fire
  // because the machine is only ever in one state.
  wire kick_ok    = kick_evt & en_now;
  wire early_kick = kick_ok & closed;
  wire do_kick    = kick_ok & ~closed;

  // A kick beats a timeout in the same clock, as it does PAUSE.
  wire do_timeout = counting & timeout_bit & ~do_kick;

  // The window counter runs while a window is open and nothing is stopping
  // it. RESET_WAIT has its own tick, so this one only ever means "time into
  // the current window".
  wire running = counting & en_now & ~pause
                 & ~do_timeout & ~early_kick & ~do_kick;
  // Restart the counter whenever the window ends or restarts.
  wire clr_cnt = !en_now | do_timeout | early_kick | do_kick;

  // PRESCALER divides the counter clock by 2^PRESCALER, so counter only
  // advances on a prescaler tick. The divider itself is clk_div_pow2; this
  // module only says when it may run and when its period restarts.
  //
  // It runs in two situations, and they have different reasons:
  //   - a window is running, where the tick paces the watchdog counter
  //   - RESET_WAIT, where one tick is the whole delay
  // Everything else restarts the period, so a new window always begins on a
  // full division period rather than a partial one.
  wire ps_en  = running | waiting;
  wire ps_clr = clr_cnt & ~waiting;   // RESET_WAIT keeps its phase

  wire ps_tick;

  clk_div_pow2 #(
      .SEL_N(8)                       // /1 .. /128
  ) u_prescaler (
      .clk  (clk),
      .rst_n(rst_n),
      .sel  (prescaler),
      .en   (ps_en),
      .clr  (ps_clr),
      .tick (ps_tick)
  );

  // clk_div_pow2 does not gate tick with en, so each user ANDs in its own run
  // condition. running and waiting are mutually exclusive, so at most one of
  // these fires on any clock.
  wire inc_cnt   = running & ps_tick;

  // RESET_WAIT holds for one prescaler tick before the reset pulse goes out.
  wire wait_done = waiting & ps_tick;

  always @(posedge clk) begin
    if (!rst_n)       counter <= {CNT_W{1'b0}};
    else if (clr_cnt) counter <= {CNT_W{1'b0}};
    else if (inc_cnt) counter <= counter + 1'b1;
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      en          <= 1'b0;
      irq_en      <= 1'b0;
      rst_en      <= 1'b0;
      timeout_sel <= 3'd0;
      window_sel  <= 2'd0;
      irq_flag    <= 1'b0;
      early_flag  <= 1'b0;
      prescaler   <= 3'd0;
    end else begin
      // IDLE: all of CTRL is writable. COUNTING: only EN lands.
      if (wr_ctrl) begin
        en <= wr_data[0];
        if (fsm_state == IDLE) begin
          irq_en      <= wr_data[1];
          timeout_sel <= wr_data[4:2];
          window_sel  <= wr_data[6:5];
        end
      end

      // CTRL2 follows the same rule as CTRL: only writable in IDLE.
      if (wr_ctrl2 && fsm_state == IDLE) begin
        prescaler <= wr_data[2:0];
        rst_en <= wr_data[3];
      end

      // Both flags are sticky: a timeout or an early kick sets one, and a
      // W1C write or rst_n clears it. The two W1C bits are separate.
      if (do_timeout)
        irq_flag <= 1'b1;
      else if (wr_status && wr_data[0])
        irq_flag <= 1'b0;

      if (early_kick)
        early_flag <= 1'b1;
      else if (wr_status && wr_data[2])
        early_flag <= 1'b0;
    end
  end

  always @(posedge clk) begin
    if (!rst_n)
      fsm_state <= IDLE;
    else
      fsm_state <= nxt_state;
  end

  always @(*) begin
    case (fsm_state)
      // A feed starts a window. With WINDOW disabled there is no closed half,
      // so go straight to OPEN and every kick counts as a feed.
      IDLE:       nxt_state = do_kick ? (has_closed ? CLOSED : OPEN) : IDLE;

      // A kick here is too early: flag it and go handle the reset. The window
      // otherwise ends at the threshold, which opens it.
      CLOSED:     nxt_state = !en_now  ? IDLE :
                              early_kick ? RESET_WAIT :
                              !in_closed ? OPEN : CLOSED;

      // OPEN runs to the timeout. A kick here feeds and restarts the window.
      OPEN:       nxt_state = !en_now  ? IDLE :
                              do_timeout ? RESET_WAIT :
                              do_kick ? (has_closed ? CLOSED : OPEN) : OPEN;

      // Hold for one prescaler tick, then release. RST_EN is frozen outside
      // IDLE, so it still reads what was configured for this window. When
      // RST_EN is clear there is no reset to delay.
      RESET_WAIT: nxt_state = !(rst_en & (irq_flag | early_flag)) ? IDLE :
                              wait_done ? RESET : RESET_WAIT;

      RESET:      nxt_state = (reset_counter[19] == 1) ? IDLE : RESET;
      default: nxt_state = IDLE;
    endcase
  end

  // in RESET state, reset_counter counts up to 2^19, then go to IDLE
  always @(posedge clk) begin
    if (!rst_n)
      reset_counter <= 20'b0;
    else
      reset_counter <= (fsm_state == RESET) ? (reset_counter + 1) : 0;
  end

  wire irq  = irq_en & (irq_flag | early_flag);
  wire wdt_rst = rst_en & (fsm_state == RESET);

  assign uo_out = {5'b0, wdt_rst, irq, miso};

endmodule
