# Formal verification (SymbiYosys)
#
#   make            bmc + cover, the quickest useful regression
#   make bmc        bounded model check, checks the asserts
#   make cover      produce a waveform for each cover
#   make proof      k-induction, proves the asserts hold forever
#   make irq_demo   deliberately failing example, shows a counterexample
#   make all        all four tasks (irq_demo is expected to FAIL)
#   make wave       open the most recent waveform in gtkwave, showing the
#                   signals from the assert or cover sby reported
#   make clean      remove every directory sby produced
#
# Needs the OSS CAD Suite. If sby is not on PATH, first run:
#   source ~/oss-cad-suite/environment

SBY      := sby
SBY_FILE := wdt.sby
# irq_demo is listed separately: it is expected to FAIL, so its rule differs
TASKS      := bmc proof cover
ALL_TASKS  := $(TASKS) irq_demo

# One output directory per task, plus wdt/ which holds status.sqlite
OUTDIRS  := wdt $(addprefix wdt_,$(ALL_TASKS))

.PHONY: default all $(ALL_TASKS) wave clean check-tools

default: bmc cover

all:
	@$(MAKE) --no-print-directory $(ALL_TASKS)

# -f lets sby overwrite an existing output directory
$(TASKS): check-tools
	$(SBY) -f $(SBY_FILE) $@

# irq_demo is meant to fail, so do not let it break `make all`
irq_demo: check-tools
	-$(SBY) -f $(SBY_FILE) irq_demo

check-tools:
	@command -v $(SBY) >/dev/null || { \
	  echo "$(SBY) not found. First run: source ~/oss-cad-suite/environment"; \
	  exit 1; }

# sby writes a .vcd but no .gtkw, so gtkwave opens on an empty pane.
# mkgtkw.py builds one holding just the signals from the reported property.
wave:
	@vcd=$$(ls -t $(addsuffix /engine_0/*.vcd,$(OUTDIRS)) 2>/dev/null | head -1); \
	if [ -z "$$vcd" ]; then echo "No waveform yet. Run make cover or make irq_demo first."; exit 1; fi; \
	./mkgtkw.py "$$vcd"; \
	echo "opening $$vcd"; gtkwave "$$vcd" "$${vcd%.vcd}.gtkw" >/dev/null 2>&1 &

clean:
	rm -rf $(OUTDIRS)
