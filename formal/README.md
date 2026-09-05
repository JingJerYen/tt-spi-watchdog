# Formal verification 最小範例 (SymbiYosys)

## 這在幹嘛？跟跑 testbench 差在哪？

| | 模擬 (cocotb / iverilog) | Formal (SymbiYosys) |
|---|---|---|
| 輸入從哪來 | 你自己在 testbench 裡寫 | solver 自動列舉「所有」可能 |
| 檢查什麼 | 你寫的那幾個 case | 你寫的「性質」在所有 case 下都成立 |
| 失敗時給什麼 | 一個 log | 一段反例波形：讓性質失敗的最短輸入序列 |
| 盲點 | 你沒想到的 case 不會被測 | 只能展開有限拍 (depth)，太長的路走不到 |

一句話：**模擬是「我試幾條路」，formal 是「所有路我都幫你走一遍」**。
代價是電路要小、要走的拍數要短，所以這裡把 `WD_BASE_EXP` 從 18 縮到 3。

## 檔案

- `wdt_formal.sv` 包住 DUT 的 wrapper，性質 (assert / cover) 都寫在這裡。src/ 一行都沒改。
- `wdt.sby` SymbiYosys 設定：讀哪些檔、跑多深、用哪個 solver。

## 三個關鍵字

```systemverilog
assume(cond);  // 限制輸入：「外面的世界只會這樣」
assert(cond);  // 要求：在所有輸入下永遠為真，否則報反例
cover(cond);   // 請 solver 找一條路讓 cond 成立，並把波形給你
```

## 跑起來

第一次先把工具加到 PATH（每開一個新 shell 都要）：

```bash
source ~/oss-cad-suite/environment
```

然後在 `formal/` 目錄下：

```bash
sby -f wdt.sby bmc
```

- `bmc` 跑五個 assert，最後一行印 `PASS` 表示 40 拍內沒有任何輸入序列能違反它們。
- `cover` 讓 solver 示範「怎麼讓狗開始數」和「怎麼讓狗咬人」，波形在 `wdt_cover/engine_0/trace*.vcd`。
- `irq_demo` 故意加一條錯的 assert「IRQ 永遠不亮」，solver 會湊出一段 SPI 波形打臉你，波形在 `wdt_irq_demo/engine_0/trace.vcd`。

看波形：

```bash
gtkwave wdt_irq_demo/engine_0/trace.vcd
```

## 怎麼讀結果

```
SBY  ... summary: Elapsed clock time [H:MM:SS (secs)]: 0:00:05 (5)
SBY  ... summary: engine_0 (smtbmc yices) returned pass
SBY  ... DONE (PASS, rc=0)
```

- `PASS` ＝ depth 拍內找不到反例。注意這只保證「前 N 拍」，不是數學上的永遠。
- `FAIL` ＝ 找到反例，看 `trace.vcd`，從最後一拍往前看哪個輸入造成的。
- cover 的 `PASS` ＝ 每個 cover 都找到了一條路；`FAIL` ＝ 有 cover 在 depth 內走不到（可能是 depth 太淺，也可能是設計真的到不了）。

## 看一個真實反例：irq_demo 的波形解碼

`irq_demo` 加了一條錯的 assert「IRQ 永遠不亮」。solver 在 27 拍內找到這條路（每拍一列，取自 trace.vcd）：

```
step  SCLK MOSI CS_N KICK | state  irq | wr_en addr data
  1     1    0    0    0  | IDLE    0  |
  ...   (CS_N 拉低，SCLK 每拍翻轉，MOSI 一路送進 0 0 0 1 0 0 0 0 1 1)
 20     1    1    1    0  | IDLE    0  |          <- CS_N 拉高，frame 結束
 22     0    1    1    0  | IDLE    0  | 1     0   0x43   <- 寫入 CTRL = 0x43
 21/23             KICK=1 -> 0 -> 1                       <- KICK 腳兩個上升緣
 24                       | EARLY   0  |
 26                       | RWAIT   1  |                  <- early_flag 設起，IRQ 亮
```

CTRL = 0x43 = `0b1000011`：EN=1、IRQ_EN=1、WINDOW=2。
solver 沒有讀過 datasheet，它純粹靠「窮舉所有輸入」發現：
先用 SPI 開狗並設一個 early window，再在 early window 裡踢第二次 KICK，就會觸發 EARLY_FLAG → IRQ。
這就是 formal 的價值：你不用想 test case，它幫你找。

## 想 assert DUT 內部的 reg（例如 lock、en）怎麼辦

業界標準做法有兩種：直接寫 `dut.lock` 這種階層參照，或用 SystemVerilog `bind` 把性質模組黏進 DUT。
yosys 內建的 `read -formal` 前端**兩種都不支援**：階層參照會報錯（或更糟，變成沒人驅動的浮空 wire，
solver 亂填、結果全是垃圾）；`bind` 會被 parse 然後靜默丟掉，性質根本沒進設計。
SBY 文件說支援這些，指的是付費的 Verific 前端。

開源的解法是 OSS CAD Suite 附的 **slang 前端**（`plugin -i slang; read_slang ...`），它是完整的 SystemVerilog 實作，
階層參照可以直接用。所以 `wdt.sby` 改用 `read_slang`，P6 就能在 wrapper 裡寫 `$past(dut.lock)`，`src/` 不用碰。

另一個常見但比較醜的做法是把性質寫進 RTL、用 `` `ifdef FORMAL `` 包住（yosys 社群早年的慣例）。能動，但 RTL 會被驗證碼塞胖。

寫性質時兩個要注意的：

1. **reset 前所有 reg 的值都是任意的。** 所以 assert 和 cover 都要守在 `cyc == 2` 裡面。
   沒守的 `cover(dut.lock)` 會在第 0 拍就「到達」，因為 solver 選了 lock 初值為 1。
2. **每加一條 assert 就加一條對應的 cover，確認前提到得了。** P6 的前提是 lock=1，cover 顯示它要 44 拍才到。
   如果 bmc 深度還是 40，P6 會 PASS，但那是「前提永遠不成立」的空 PASS，什麼都沒證明。
   這就是為什麼 `wdt.sby` 的 bmc 深度改成了 60。

## 下一步可以玩的

1. 把 `wdt.sby` 的 `depth 40` 改成 `10`，看 cover 是不是就走不到 `RESET` 了。
2. 再加一條你覺得「應該成立」的性質，例如「RESET_WAIT 只有在 rst_en=1 時才會進 RESET」。
3. 把 `mode bmc` 改成 `mode prove`（k-induction）。它會嘗試證明「永遠」而不是「前 N 拍」，
   通常第一次會失敗，因為 solver 會從一個「不可能到達的狀態」出發；補 assert 把那些狀態排除就是 formal 的日常。
