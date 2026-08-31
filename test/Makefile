# Makefile
# See https://docs.cocotb.org/en/stable/quickstart.html for more info

# defaults
SIM ?= icarus
FST ?= -fst # Use more efficient FST format
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../src
PROJECT_SOURCES = project.v spi_regs.v clk_div_pow2.v

# The simulation timeout exponent lives in tb.v. Python reads it from the
# environment, so the two stay in step from one place.
WD_BASE_EXP = 8
export WD_BASE_EXP

ifneq ($(GATES),yes)

# RTL simulation:
SIM_BUILD				= sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))

else

# Gate level simulation:
# GATES must reach the Python side too: test.py skips the timeout tests and
# picks the silicon WD_BASE_EXP from it. A make variable alone is not visible
# to the simulator's environment.
export GATES
SIM_BUILD				= sim_build/gl
COMPILE_ARGS    += -DGL_TEST
COMPILE_ARGS    += -DFUNCTIONAL
COMPILE_ARGS    += -DUSE_POWER_PINS
COMPILE_ARGS    += -DSIM
COMPILE_ARGS    += -DUNIT_DELAY=\#1
VERILOG_SOURCES += $(PDK_ROOT)/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v
VERILOG_SOURCES += $(PDK_ROOT)/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v

# this gets copied in by the GDS action workflow
VERILOG_SOURCES += $(PWD)/gate_level_netlist.v

endif

# Allow sharing configuration between design and testbench via `include`:
COMPILE_ARGS 		+= -I$(SRC_DIR)

ifeq ($(SIM),verilator)
EXTRA_ARGS += --timing --trace-fst -Wno-fatal

# Coverage is Verilator-only: Icarus collects none. Opt in with RTL_COVERAGE=1
# (not COVERAGE, which cocotb reserves for Python-level coverage),
# which also redirects the build directory so an instrumented build never
# overwrites a plain one.
ifeq ($(RTL_COVERAGE),1)
EXTRA_ARGS  += --coverage
override SIM_BUILD = sim_build/cov
endif
endif

# Include the testbench sources:
VERILOG_SOURCES += $(PWD)/tb.v
TOPLEVEL = tb

# List test modules to run, separated by commas and without the .py suffix:
COCOTB_TEST_MODULES = test

# include cocotb's make rules to take care of the simulator setup
include $(shell cocotb-config --makefiles)/Makefile.sim

# Report on the coverage.dat left by an RTL_COVERAGE=1 run.
#
#   make SIM=verilator RTL_COVERAGE=1   # run the suite and collect
#   make cov-report                     # annotate, summarise, build HTML
#
# Two percentages, both correct: genhtml counts lines, the annotated view
# counts toggle points, so one unexercised 8-bit port is three lines but
# sixteen toggles. In cov_annotated only %000000 means uncovered -- other
# %00NNNN values are hit counts.
PERL_LOCAL_LIB ?= $(HOME)/perl5/lib/perl5
PERL5LIB_ARG    = $(PERL_LOCAL_LIB):$(PERL_LOCAL_LIB)/x86_64-linux-thread-multi

.PHONY: cov-report
cov-report: coverage.dat
	@verilator_coverage --annotate cov_annotated --annotate-all coverage.dat \
	  | sed -n "s/^Total coverage/toggle coverage:/p"
	@verilator_coverage --write-info coverage.info coverage.dat
	@PERL5LIB=$(PERL5LIB_ARG) genhtml -o coverage_html coverage.info 2>/dev/null \
	  | sed -n "s/^  lines\.*: /line coverage:   /p" \
	  || echo "genhtml unavailable, skipped HTML"
	@echo
	@echo "--- uncovered points (%000000) ---"
	@grep -rn "%000000" cov_annotated/ || echo "(none)"
	@echo
	@echo "cov_annotated/     annotated sources"
	@echo "coverage_html/     open index.html in a browser"

coverage.dat:
	@echo "No coverage.dat. Run: make SIM=verilator RTL_COVERAGE=1" >&2
	@false

# Serve the HTML report over localhost. Handy under Remote-SSH, where there is
# no browser on this machine: VSCode picks the port up in its PORTS tab and
# forwards it, so the report opens in the local browser.
#
# The OS picks a free port and the server prints it, so this never collides
# with whatever else is already listening.
.PHONY: cov-serve
cov-serve: coverage_html/index.html
	@cd coverage_html && python3 -c "import functools, http.server as h; \
	  s = h.HTTPServer(('127.0.0.1', 0), functools.partial(h.SimpleHTTPRequestHandler)); \
	  print('Serving on http://localhost:%d  (Ctrl+C to stop)' % s.server_port); \
	  print('In VSCode, open the PORTS tab and click the globe icon.', flush=True); \
	  s.serve_forever()"

coverage_html/index.html: coverage.dat
	@$(MAKE) --no-print-directory cov-report

.PHONY: cov-clean
cov-clean:
	rm -rf coverage.dat coverage.info cov_annotated coverage_html
