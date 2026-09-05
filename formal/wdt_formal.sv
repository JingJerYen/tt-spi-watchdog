// ---------------------------------------------------------------------------
// Formal wrapper for tt_um_jjy_spi_watchdog (SymbiYosys 最小範例)
//
// 這個檔案「包住」DUT，只透過 port 觀察它，完全不改 src/ 裡的 RTL。
// 三種關鍵字:
//   assume(...)  告訴 solver「輸入只會長這樣」(限制環境)
//   assert(...)  要求「在所有可能的輸入序列下，這個式子永遠為真」
//   cover(...)   要求 solver「找一條輸入序列讓這個式子成立」(給我看一個例子)
// ---------------------------------------------------------------------------
`default_nettype none

module wdt_formal (
    input wire       clk,
    input wire [7:0] ui_in
);

  // 把 timeout 縮到最小 (2^3 = 8 個 tick) 讓 solver 跑得動。
  // 真正流片是 18，那樣 timeout 要 2^18 cycle，BMC 根本走不到。
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

  // 從 debug pin 讀回 FSM 狀態 (uo_out[5:3])，編碼同 project.v
  wire [2:0] state     = uo_out[5:3];
  wire       wdt_rst_n = uo_out[2];
  wire       irq       = uo_out[1];
  wire       kick_pin  = ui_in[4];
  wire       cs_n      = ui_in[2];

  localparam IDLE = 3'd0, EARLY = 3'd1, NORMAL = 3'd2, RESET_WAIT = 3'd3, RESET = 3'd4;

  // -------------------------------------------------------------------------
  // 1. 時間軸 / reset 約定
  //
  // formal 沒有 testbench，時間 0 時每個 reg 的值都是「任意」。
  // 所以我們自己數 cycle：cyc=0 那拍強迫 rst_n=0，之後 rst_n 固定為 1。
  // -------------------------------------------------------------------------
  reg [1:0] cyc = 2'd0;
  always @(posedge clk) if (cyc != 2'd2) cyc <= cyc + 2'd1;

  assign rst_n = (cyc != 2'd0);
  // 註: 這裡用 assign 而不是 assume，效果一樣，但更直觀。

  // 「從 reset 以來 KICK 腳一直是 0、CS_N 一直是 1」= 外面完全沒動作
  reg quiet = 1'b1;
  always @(posedge clk) if (kick_pin || !cs_n) quiet <= 1'b0;

  // -------------------------------------------------------------------------
  // 2. 性質 (properties)
  // -------------------------------------------------------------------------
  always @(posedge clk) begin
    // P1: reset 完的第一拍一定在 IDLE
    if (cyc == 2'd1)
      assert (state == IDLE);

    if (cyc == 2'd2) begin
      // P2: 狀態編碼永遠合法 (3 bit 有 8 種，只用了 5 種)
      assert (state <= RESET);

      // P3: WDT_RST_N 腳只有在 RESET 狀態才會拉低
      assert (wdt_rst_n == (state != RESET));

      // P4: 沒人餵狗、沒 SPI 動作，狗就不會自己醒來
      if (quiet)
        assert (state == IDLE);

      // P5: FSM 只能走圖上畫的邊 ($past = 上一拍的值)
      case ($past(state))
        IDLE:       assert (state == IDLE || state == EARLY || state == NORMAL);
        EARLY:      assert (state == IDLE || state == EARLY || state == NORMAL || state == RESET_WAIT);
        NORMAL:     assert (state == IDLE || state == EARLY || state == NORMAL || state == RESET_WAIT);
        RESET_WAIT: assert (state == IDLE || state == RESET_WAIT || state == RESET);
        RESET:      assert (state == IDLE || state == RESET);
        default:    assert (0);
      endcase


      // P6: LOCK 設了以後，EN 就不能被關掉，LOCK 也不能被清掉
      // (dut.lock 是階層參照，只有 slang 前端吃得下)
      if ($past(dut.lock)) begin
        assert (dut.en);
        assert (dut.lock);
      end

`ifdef DEMO_BUG
      // 故意寫錯的性質：「IRQ 永遠不會亮」。
      // 它當然是錯的，solver 會自己湊出一段 SPI 波形 + KICK 來打臉你，
      // 順便示範怎麼看反例 (counter-example) 波形。
      assert (!irq);
`endif
    end

    // -------------------------------------------------------------------------
    // 3. cover: 請 solver 示範「怎麼走到這裡」
    // -------------------------------------------------------------------------
    if (cyc == 2'd2) begin
      cover (state == NORMAL);   // 狗被啟動、正在數
      cover (!wdt_rst_n);        // 一路走到咬人 (reset pulse)
      cover (dut.lock);          // LOCK 到得了嗎 (P6 的前提)
    end
  end

endmodule
