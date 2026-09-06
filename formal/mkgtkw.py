#!/usr/bin/env python3
"""Write a GTKWave save file showing only the signals sby's message points at.

sby reports a failing assert or a reached cover with its exact source span:

    Assert failed in wdt_formal: wdt_formal.sv:121.7-121.21 (_witness_.check_419)

This reads that span out of the source, keeps the identifiers that exist in
the trace, and writes a .gtkw with those and nothing else.

Usage:
    mkgtkw.py <sby-task-dir>       # newest trace in that task
    mkgtkw.py <trace.vcd>
"""

import argparse
import os
import re
import sys

# SystemVerilog words that show up inside a property but are not signals.
KEYWORDS = {"assert", "assume", "cover", "past", "stable", "changed", "rose",
            "fell", "onehot", "onehot0", "countones", "isunknown", "signed",
            "unsigned", "begin", "end", "if", "else", "case", "endcase"}

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
REPORT = re.compile(
    r"(?:Assert failed|Reached cover statement).*?: "
    r"(\S+?):(\d+)\.(\d+)-(\d+)\.(\d+)")
DUMP = re.compile(r"Writing trace to VCD file: (\S+)")


def vcd_signals(path):
    """Return {full.path: width} for everything declared in the trace."""
    out, scope = {}, []
    with open(path) as f:
        for line in f:
            tok = line.split()
            if not tok:
                continue
            if tok[0] == "$scope":
                scope.append(tok[2])
            elif tok[0] == "$upscope":
                if scope:
                    scope.pop()
            elif tok[0] == "$var":
                out[".".join(scope + [tok[4]])] = int(tok[2])
            elif tok[0] == "$enddefinitions":
                break
    return out


def find_report(task_dir, vcd):
    """Locate the source span of the property that produced this trace.

    A cover run writes one trace per cover statement, so the log is walked in
    order: each report line is followed by the file it was dumped to.
    """
    log = os.path.join(task_dir, "logfile.txt")
    if not os.path.exists(log):
        return None
    want, pending, last = os.path.basename(vcd), None, None
    with open(log, errors="replace") as f:
        for line in f:
            m = REPORT.search(line)
            if m:
                src, l0, c0, l1, c1 = m.groups()
                pending = last = (src, int(l0), int(c0), int(l1), int(c1))
                continue
            m = DUMP.search(line)
            if m and pending:
                if os.path.basename(m.group(1)) == want:
                    return pending
                pending = None
    return last


def span_text(report, search_dirs):
    """Read the exact source span sby named."""
    src, l0, c0, l1, c1 = report
    for d in search_dirs:
        path = os.path.join(d, src)
        if os.path.exists(path):
            break
    else:
        return None
    lines = open(path, errors="replace").read().splitlines()
    if l1 > len(lines):
        return None
    if l0 == l1:
        return lines[l0 - 1][c0 - 1:c1]
    chunk = [lines[l0 - 1][c0 - 1:]] + lines[l0:l1 - 1] + [lines[l1 - 1][:c1]]
    return "\n".join(chunk)


def signals_in(text, signals, top):
    """Identifiers from the property text that name something in the trace."""
    hits = []
    for ident in IDENT.findall(text):
        if ident.split(".")[-1] in KEYWORDS or ident in KEYWORDS:
            continue
        for cand in (f"{top}.{ident}", ident):
            if cand in signals and cand not in hits:
                hits.append(cand)
                break
    return hits


def write_gtkw(out_path, vcd, picks, signals, note):
    lines = [
        "[*]", f"[*] {note}", "[*]",
        f'[dumpfile] "{os.path.abspath(vcd)}"',
        f'[savefile] "{os.path.abspath(out_path)}"',
        "[timestart] 0",
        "[size] 1000 400",
        "[pos] -1 -1",
        "[sst_width] 260",
        "[signals_width] 220",
        "[sst_expanded] 1",
    ]
    fmt = None
    for sig in picks:
        width = signals[sig]
        want = "@22" if width > 1 else "@28"
        if want != fmt:
            lines.append(want)
            fmt = want
        lines.append(f"{sig}[{width-1}:0]" if width > 1 else sig)
    lines += ["[pattern_trace] 1", "[pattern_trace] 0"]
    open(out_path, "w").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="an sby task directory, or a .vcd file")
    ap.add_argument("-o", "--output", help="output .gtkw (default: beside the .vcd)")
    args = ap.parse_args()

    if os.path.isdir(args.target):
        engine = os.path.join(args.target.rstrip("/"), "engine_0")
        vcds = [os.path.join(engine, f) for f in os.listdir(engine)
                if f.endswith(".vcd")] if os.path.isdir(engine) else []
        if not vcds:
            sys.exit(f"no .vcd under {engine}")
        vcd = max(vcds, key=os.path.getmtime)
    else:
        vcd = args.target

    task_dir = os.path.dirname(os.path.dirname(os.path.abspath(vcd)))
    signals = vcd_signals(vcd)
    if not signals:
        sys.exit(f"no signals in {vcd}")
    top = next((s.split(".")[0] for s in signals if "." in s), "top")

    report = find_report(task_dir, vcd)
    if not report:
        sys.exit(f"no assert/cover report for {os.path.basename(vcd)} in the sby log")

    # sby runs yosys from <task>/src, so sources sit there; fall back to cwd.
    text = span_text(report, [os.path.join(task_dir, "src"), ".", os.path.dirname(vcd)])
    if text is None:
        sys.exit(f"cannot read {report[0]}:{report[1]}")

    picks = signals_in(text, signals, top)
    if not picks:
        sys.exit(f"no trace signals named in: {text.strip()}")

    src, line = report[0], report[1]
    out = args.output or os.path.splitext(vcd)[0] + ".gtkw"
    write_gtkw(out, vcd, picks, signals, f"{src}:{line}  {text.strip()}")
    print(f"{out}: {' '.join(picks)}")


if __name__ == "__main__":
    main()
