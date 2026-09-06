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

然後在 `formal/` 目錄下用 Makefile：

```bash
make
```

`make` 跑 bmc 和 cover，是日常回歸最常用的組合。其他 target：

| 指令 | 做什麼 |
| --- | --- |
| `make bmc` | 有界模型檢查，驗 assert |
| `make cover` | 產生 cover 的示範波形 |
| `make proof` | k-induction，證明 assert 永遠成立 |
| `make irq_demo` | 故意失敗的範例，看反例波形 |
| `make all` | 四個全跑（`irq_demo` 預期 FAIL） |
| `make wave` | 用 gtkwave 開最近一次產生的波形 |
| `make clean` | 刪掉所有 sby 輸出目錄 |

- `bmc` 跑所有 assert，最後一行印 `PASS` 表示 `wdt.sby` 設定的深度（目前 100 拍）內，沒有任何輸入序列能違反它們。
- `cover` 讓 solver 示範「怎麼讓狗開始數」和「怎麼讓狗咬人」，波形在 `wdt_cover/engine_0/trace*.vcd`。
- `irq_demo` 故意加一條錯的 assert「IRQ 永遠不亮」，solver 會湊出一段 SPI 波形打臉你，波形在 `wdt_irq_demo/engine_0/trace.vcd`。

看波形，`make wave` 會挑最近產生的那個開：

```bash
make wave
```

指定某一個就直接給 gtkwave：

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

## 守衛怎麼下：assert 要鬆，cover 要緊

formal 沒有 testbench，時間 0 時每個暫存器都是任意值。`project.v` 的 `fsm_state`、`lock`、`en`
都沒有初值（ASIC 的正反器本來就沒有定義的開機狀態），而 reset 是同步的，要等第一個正緣才生效。
所以在 `cyc==0` 那拍，DUT 裡全是垃圾。每條性質都必須用守衛把那段擋掉。

`$past(x)` 也只是一個每個正緣抄一份 `x` 的暫存器，同樣有這個問題。它在 `cyc==1` 抄到的是
`cyc==0` 的垃圾，所以用了 `$past` 的性質還要再多等一拍。

守衛的原則是問「這條性質讀到的東西，最早從哪一拍開始都有意義」：

| 性質讀什麼 | 守衛 |
| --- | --- |
| 只讀當拍的訊號 | `rst_n` |
| 讀一層 `$past` | `cyc == 2'd2` |
| 專門驗 reset 那一拍 | `cyc == 2'd1`（P1 就是） |

**但 assert 和 cover 的鬆緊方向相反。**

`assert` 的守衛要**盡量鬆**，鬆到不會誤報為止。檢查的拍數越多，solver 能拿來擺垃圾的無人區越小。
INV1、P2、P3、P4 都不用 `$past`，所以放 `rst_n`；P5、P6 用了 `$past`，只能放 `cyc == 2'd2`。

`cover` 的守衛要**夠緊**，一律放 `cyc == 2'd2`，不管有沒有用 `$past`。cover 問的是「存不存在一條路」，
守衛越鬆越容易被滿足，而輕易被滿足的 cover 沒有價值。實測把 cover 區塊改成 `rst_n`，
三條「回到 IDLE」的 transition cover 就從 28 到 44 拍掉到 2 拍：`state` 是剛 reset 的 IDLE，
`$past(state)` 是 reset 前的垃圾，solver 想填什麼就填什麼，零工作量達成。

## 三種「空 PASS」

工具給綠燈不代表你證明了東西。這三種都會 PASS，而且都什麼都沒證明：

1. **assert 的前提到不了。** 例如 P6 的前提是 `lock=1`，但 cover 顯示它要 44 拍才到。
   bmc 深度設 40 的話 P6 必然 PASS，因為前提永遠不成立。
2. **assert 的守衛太窄。** 例如把 `assert (!dut.lock || dut.en)` 放進 `cyc == 2'd1`，
   那拍 `lock` 和 `en` 剛被清成 0，必然成立。實測顯示它對 induction 門檻毫無貢獻，
   而且注入 bug 後叫的是 P6 不是它。
3. **cover 的守衛落在 reset 之前。** 把 cover 區塊改成 `cyc == 2'd0`，16 條 cover 全部在 1 拍達成，
   因為 solver 直接把暫存器填成要的值。

怎麼分辨：

- **看拍數，不要只看 PASS。** 這個設計光一筆 SPI 寫入就要 22 拍，所以任何個位數拍就達成的
  cover 幾乎一定有問題。
- **每加一條 assert 就配一條 cover**，確認它的前提到得了，並把 bmc 深度設在最深的 cover 之上。
- **mutation testing。** 故意在 RTL 埋一個那條 assert 應該抓到的 bug，看它是不是真的叫，
  而且叫的是它而不是別人。這比盯著 PASS 有用得多。

## prove 模式與 invariant

`mode prove` 用 k-induction，證的是「永遠」而不是「前 N 拍」。它不從 reset 出發，而是讓 solver
憑空捏一個狀態，唯一的條件是前 k 拍 assert 都成立。所以它常常從一個 reset 根本到不了的狀態出發，
然後失敗。

**induction 失敗通常不是 bug，是缺 invariant。** 標準工作循環是：跑，失敗，解碼
`trace_induct.vcd` 的第 0 拍看 solver 捏了什麼不可能的狀態，補一條 assert 宣告它不合法，再跑。
這個設計實際遇過兩個：

- solver 捏 `lock=1` 但 `en=0`。RTL 的 `lock <= lock | (wr_data[4] & en)` 保證這不可能，
  但沒有 assert 說出來。補上 `assert (!dut.lock || dut.en)`。
- solver 捏「腳位從沒動過（`quiet=1`）但 SPI shift register 裡躺著一個完整封包」。
  三拍後就假造出一次寫入打破 P4。補上 `if (quiet)` 底下那組同步器閒置的 assert。

`prove` 的 `depth` 是 k，跟 bmc 的 depth 意義完全不同。k 只是 solver 收斂所需的窗口大小，
**不是品質指標**：depth 3 的 PASS 和 depth 40 的 PASS 是同一個定理。實測這個設計：

| 設定 | 需要的 depth |
| --- | --- |
| 沒有 invariant | 5 |
| 只加 INV1（lock 蘊含 en） | 5（失敗點推到 P4） |
| INV1 加 INV2（quiet 時同步器閒置） | 3 |

而 depth 3 跑 0.42 秒、depth 40 跑 0.82 秒，所以對這個大小的設計，寫 invariant 來降 k 不划算。
真正該降 k 的時機是電路大到 solver 跑不動。實務建議：`proof: depth 10`，然後專心寫規格性質。

**invariant 一定要用 `assert`，絕對不要用 `assume`。** 實測同一個注入的 bug：

```
invariant 寫成 assert  ->  DONE (FAIL)   抓到了
invariant 寫成 assume  ->  DONE (PASS)   同一個壞設計，過了
```

`assume` 是在告訴 solver「這種情況不用考慮」，等於把含 bug 的路徑從搜尋空間裡刪掉。
內部狀態一律用 assert，`assume` 只留給真正的外部輸入約定。

## 下一步可以玩的

1. 把 `wdt.sby` 的 `cover: depth 80` 改成 `30`，看 cover 是不是就走不到 `RESET` 了（它要 49 拍）。
2. 再加一條你覺得「應該成立」的性質，例如「RESET_WAIT 只有在 rst_en=1 時才會進 RESET」。
3. `make proof` 已經是 k-induction 了。把 `proof: depth 5` 改成 `4`，看它失敗在哪條 assert，
   再照「prove 模式與 invariant」那節解碼 `wdt_proof/engine_0/trace_induct.vcd` 的第 0 拍。
4. 做一次 mutation testing：把 `project.v` 的 `lock <= lock | (wr_data[4] & en)` 改成
   `lock <= lock | wr_data[4]`，跑 `make bmc`，確認 INV1 真的會叫。記得改回來。
