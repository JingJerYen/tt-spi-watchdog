/*
 * Copyright (c) 2026 Jing Jer Yen
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_spi_watchdog #(
    // Timeout base exponent. The eight settings span 2^18 .. 2^28 clocks.
    // Testbenches lower it to shorten simulation.
    // Minimum is 3: the early-window index reaches WD_BASE_EXP - 3.
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
      kick_s0 <= 0;
      kick_s1 <= 0;
      kick_s2 <= 0;
    end else begin
      kick_s0 <= kick_pin;
      kick_s1 <= kick_s0;
      kick_s2 <= kick_s1;
    end
  end
  // s1 is newer, s2 is older
  wire kick_pin_evt = kick_s1 & ~kick_s2;

  localparam ADDR_CTRL = 2'd0, ADDR_KICK = 2'd1, ADDR_STATUS = 2'd2, ADDR_CTRL2 = 2'd3;

  // Magic value a KICK write has
  localparam KICK_MAGIC = 7'h5A;

  wire wr_ctrl   = wr_en & (wr_addr == ADDR_CTRL);
  wire wr_kick   = wr_en & (wr_addr == ADDR_KICK);
  wire wr_status = wr_en & (wr_addr == ADDR_STATUS);
  wire wr_ctrl2  = wr_en & (wr_addr == ADDR_CTRL2);

  // ------------------------------------------------------------------
  // Registers
  // ------------------------------------------------------------------
  reg       en; // writes 0 : force state to IDLE. writes 1 + kick : starts counting
  reg       irq_en; // irq gating
  reg       rst_en; // wdt_rst gating

  reg [2:0] timeout_sel; // CTRL[4:2] TIMEOUT
  reg [1:0] window_sel; // CTRL[6:5] WINDOW
  reg [2:0] prescaler; // CTRL2[2:0] PRESCALER
  reg       irq_flag; // STATUS[0] IRQ_FLAG
  reg       early_flag; // STATUS[2] EARLY_FLAG

  // two counters
  localparam CNT_W = WD_BASE_EXP + 11;
  reg [CNT_W-1:0] counter; // main counter
  reg [19:0] reset_counter; // reset pulse lasts 2^19 cycles (@ 50MHz ~= 10.486 ms)

  // state machine
  reg [2:0] fsm_state;
  reg [2:0] nxt_state;
  localparam IDLE       = 3'd0; // only in this state can user set CTRL, CTRL2
  localparam EARLY     = 3'd1; // counting, below early threshold, feed dog cause irq
  localparam NORMAL       = 3'd2; // counting, normal region, feed dog resets counter
  localparam RESET_WAIT = 3'd3; // grace period before RESET; a W1C STATUS write escapes to IDLE
  localparam RESET      = 3'd4; // send reset pulse then back to IDLE

  // frequently used states
  wire early   = (fsm_state == EARLY);
  wire counting = (fsm_state == EARLY) | (fsm_state == NORMAL);
  wire waiting  = (fsm_state == RESET_WAIT);

  // output pins
  wire irq  = irq_en & (irq_flag | early_flag);
  wire wdt_rst = ~(fsm_state == RESET);
  assign uo_out = {5'b0, wdt_rst, irq, miso};

  // read internal registers
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
  wire kick_evt = kick_pin_evt | (wr_kick & (wr_data == KICK_MAGIC));

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

  // WD_BASE_EXP is a parameter, so this index folds into a mux instead of adder
  // bit = 1 triggers timeout
  wire timeout_bit = counter[WD_BASE_EXP + hi_off];

  // suffix_or[i] = | counter[CNT_W-1:i]
  // suffix_or[i]===1 means the counter value is larger than 2^i
  wire [CNT_W-1:0] suffix_or;
  assign suffix_or[CNT_W-1] = counter[CNT_W-1];
  genvar i;
  generate
    for (i = 0; i < CNT_W - 1; i = i + 1) begin: suffix_calc
      assign suffix_or[i] = suffix_or[i+1] | counter[i];
    end
  endgenerate

  // WINDOW = 0 means no early stage setting
  wire has_early = (window_sel != 2'd0);
  wire early_timeout = suffix_or[WD_BASE_EXP + {28'd0, hi_off} - {30'd0, window_sel}];

  // en updates on the next clock. clr_en forwards a CTRL write that clears
  // EN, so en_now disarms the dog in the same clock
  wire clr_en     = wr_ctrl & ~wr_data[0];
  wire en_now     = en & ~clr_en;

  wire valid_kick = kick_evt & en_now;
  wire early_kick = valid_kick & early;
  wire normal_kick = valid_kick & ~early;

  // A kick beats a timeout in the same clock, as it does PAUSE.
  wire normal_timeout = counting & timeout_bit & ~normal_kick;

  wire clr_cnt;
  wire inc_cnt;

  // prescaler clock : enable when needed to save power
  wire ps_en  = (counting | waiting) & ~pause;
  wire ps_clr = clr_cnt & ~waiting;   // RESET_WAIT keeps its phase
  wire ps_tick;

  clk_div_pow2 #(
      .SEL_N(8)
  ) u_prescaler (
      .clk  (clk),
      .rst_n(rst_n),
      .sel  (prescaler),
      .en   (ps_en),
      .clr  (ps_clr),
      .tick (ps_tick)
  );

  // counter control, kick precedes pause
  // early kick : EARLY --> WAIT
  // normal kick : NORMAL --> EARLY
  // normal timeout : NORMAL --> WAIT
  assign clr_cnt = early_kick | normal_kick | normal_timeout;
  assign inc_cnt = counting & ps_tick & ~pause;

  always @(posedge clk) begin
    if (!rst_n)       counter <= {CNT_W{1'b0}};
    else if (clr_cnt) counter <= {CNT_W{1'b0}};
    else if (inc_cnt) counter <= counter + 1'b1;
  end

  // in RESET_WAIT, reset_counter counts up to 2^(WD_BASE-2) ticks
  // in RESET, reset_counter holds wdt_rst low for 2^19 ps_ticks
  wire wait_or_reset = (fsm_state == RESET_WAIT) | (fsm_state == RESET);

  always @(posedge clk) begin
    if (!rst_n)
      reset_counter <= 20'b0;
    else if (!wait_or_reset || (fsm_state != nxt_state))
      reset_counter <= 20'b0;
    else
      reset_counter <= reset_counter + 1;
  end

  // writes to CTRL, CTRL2
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
      // CTRL::en is always writable, while other fields are only writable in IDLE
      if (wr_ctrl) begin
        en <= wr_data[0];
        if (fsm_state == IDLE) begin
          irq_en      <= wr_data[1];
          timeout_sel <= wr_data[4:2];
          window_sel  <= wr_data[6:5];
        end
      end

      // CTRL2 follows the same rule as CTRL: only writable in IDLE
      if (wr_ctrl2 && fsm_state == IDLE) begin
        prescaler <= wr_data[2:0];
        rst_en <= wr_data[3];
      end

      // Both flags are sticky: a timeout or an early kick sets one, and a
      // W1C write or rst_n clears it
      if (normal_timeout)
        irq_flag <= 1'b1;
      else if (wr_status && wr_data[0])
        irq_flag <= 1'b0;

      if (early_kick)
        early_flag <= 1'b1;
      else if (wr_status && wr_data[2])
        early_flag <= 1'b0;
    end
  end

  wire no_pending_flag = !early_flag & !irq_flag ;

  // state machine
  always @(posedge clk) begin
    if (!rst_n)
      fsm_state <= IDLE;
    else
      fsm_state <= nxt_state;
  end

  always @(*) begin
    case (fsm_state)
      IDLE: nxt_state = !valid_kick ? IDLE:
                          has_early ? EARLY : NORMAL;

      EARLY: nxt_state = !en_now ? IDLE :
                          kick_evt ? RESET_WAIT :
                          early_timeout ? NORMAL : EARLY;

      NORMAL: nxt_state = !en_now ? IDLE :
                          normal_timeout ? RESET_WAIT :
                          !kick_evt ? NORMAL :
                          has_early ? EARLY : NORMAL;

      RESET_WAIT: nxt_state = !rst_en ? IDLE :
                              no_pending_flag ? IDLE :
                              reset_counter[WD_BASE_EXP-2] ? RESET : RESET_WAIT;

      RESET: nxt_state = reset_counter[19] ? IDLE : RESET;
      default: nxt_state = IDLE;
    endcase
  end

endmodule
