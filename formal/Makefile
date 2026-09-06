# Formal verification (SymbiYosys)
#
#   make            跑 bmc + cover (日常回歸用，最快)
#   make bmc        有界模型檢查，驗 assert
#   make cover      產生 cover 的示範波形
#   make proof      k-induction，證明 assert 永遠成立
#   make irq_demo   故意失敗的範例，看反例波形
#   make all        四個 task 全跑 (irq_demo 預期會 FAIL)
#   make wave       用 gtkwave 開最近一次的反例/示範波形
#   make clean      刪掉所有 sby 產生的輸出
#
# 工具需要 OSS CAD Suite。若 sby 不在 PATH 上，先執行:
#   source ~/oss-cad-suite/environment

SBY      := sby
SBY_FILE := wdt.sby
# irq_demo 分開列，因為它預期會 FAIL，規則不同
TASKS      := bmc proof cover
ALL_TASKS  := $(TASKS) irq_demo

# sby 的輸出目錄: 每個 task 一個，外加放 status.sqlite 的 wdt/
OUTDIRS  := wdt $(addprefix wdt_,$(ALL_TASKS))

.PHONY: default all $(ALL_TASKS) wave clean check-tools

default: bmc cover

all:
	@$(MAKE) --no-print-directory $(ALL_TASKS)

# -f 讓 sby 覆蓋既有的輸出目錄，不必先手動刪
$(TASKS): check-tools
	$(SBY) -f $(SBY_FILE) $@

# irq_demo 本來就該 FAIL，所以 all 不因為它中斷
irq_demo: check-tools
	-$(SBY) -f $(SBY_FILE) irq_demo

check-tools:
	@command -v $(SBY) >/dev/null || { \
	  echo "找不到 $(SBY)。先執行: source ~/oss-cad-suite/environment"; \
	  exit 1; }

wave:
	@vcd=$$(ls -t $(addsuffix /engine_0/*.vcd,$(OUTDIRS)) 2>/dev/null | head -1); \
	if [ -z "$$vcd" ]; then echo "沒有波形。先跑 make cover 或 make irq_demo"; exit 1; fi; \
	echo "開啟 $$vcd"; gtkwave "$$vcd" >/dev/null 2>&1 &

clean:
	rm -rf $(OUTDIRS)
