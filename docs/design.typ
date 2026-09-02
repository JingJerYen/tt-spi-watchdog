// Block diagram for the TT SPI watchdog datasheet.
// Build: typst compile design.typ design.svg
#set page(width: 690pt, height: 300pt, margin: 0pt, fill: white)
#set text(font: "Lato", size: 9pt, fill: rgb("#222222"))

#let sc = rgb("#555555")

// -- helpers ---------------------------------------------------------------
#let seg(x1, y1, x2, y2) = place(line(start: (x1, y1), end: (x2, y2), stroke: 1pt + sc))
#let head-r(x, y) = place(dx: x - 7pt, dy: y - 4pt, polygon(fill: sc, (0pt, 0pt), (7pt, 4pt), (0pt, 8pt)))
#let head-l(x, y) = place(dx: x, dy: y - 4pt, polygon(fill: sc, (7pt, 0pt), (0pt, 4pt), (7pt, 8pt)))
#let head-u(x, y) = place(dx: x - 4pt, dy: y, polygon(fill: sc, (0pt, 7pt), (4pt, 0pt), (8pt, 7pt)))
#let head-d(x, y) = place(dx: x - 4pt, dy: y - 7pt, polygon(fill: sc, (0pt, 0pt), (4pt, 7pt), (8pt, 0pt)))
#let lab(x, y, body) = place(dx: x, dy: y, text(size: 7.5pt, fill: rgb("#333333"), body))

#let blockbox(x, y, w, h, fillc, title, sub) = place(dx: x, dy: y,
  rect(width: w, height: h, fill: fillc, stroke: 1pt + sc, radius: 5pt, inset: 6pt,
    align(center + horizon)[
      #text(weight: "bold", size: 10pt)[#title]
      #v(2pt)
      #text(size: 8pt, fill: rgb("#333333"))[#sub]
    ]))

// -- title -----------------------------------------------------------------
#place(dx: 70pt, dy: 14pt, text(weight: "bold", size: 12pt)[SPI Watchdog — block diagram])

// -- blocks ----------------------------------------------------------------
#blockbox(70pt, 60pt, 130pt, 95pt, rgb("#e8f0fe"),
  [SPI Interface], [spi\_regs \ mode 0 · 10-bit frame])

#blockbox(260pt, 40pt, 140pt, 120pt, rgb("#fef7e0"),
  [Internal Registers], [CTRL · CTRL2 \ STATUS (flags, W1C) \ KICK (0x5A)])

#blockbox(460pt, 40pt, 150pt, 150pt, rgb("#e6f4ea"),
  [Control Logic], [5-state FSM \ 29-bit counter \ window decode \ reset timer])

#blockbox(465pt, 220pt, 140pt, 55pt, rgb("#f3e8fd"),
  [Prescaler], [clk\_div\_pow2 \ /1 … /128])

// -- input pins (left) -----------------------------------------------------
#seg(14pt, 80pt, 70pt, 80pt)   #head-r(70pt, 80pt)   #lab(16pt, 68pt)[SCLK]
#seg(14pt, 98pt, 70pt, 98pt)   #head-r(70pt, 98pt)   #lab(16pt, 86pt)[MOSI]
#seg(14pt, 116pt, 70pt, 116pt) #head-r(70pt, 116pt)  #lab(16pt, 104pt)[CS\_N]
#seg(70pt, 140pt, 14pt, 140pt) #head-l(14pt, 140pt)  #lab(16pt, 128pt)[MISO]

// -- SPI <-> registers -----------------------------------------------------
#seg(200pt, 85pt, 260pt, 85pt)  #head-r(260pt, 85pt)  #lab(215pt, 73pt)[write]
#seg(260pt, 125pt, 200pt, 125pt) #head-l(200pt, 125pt) #lab(217pt, 113pt)[read]

// -- registers <-> control -------------------------------------------------
#seg(400pt, 75pt, 460pt, 75pt)  #head-r(460pt, 75pt)  #lab(412pt, 63pt)[config]
#seg(460pt, 115pt, 400pt, 115pt) #head-l(400pt, 115pt) #lab(408pt, 103pt)[set flags]

// -- KICK / PAUSE pins straight into the control logic ---------------------
#seg(14pt, 172pt, 460pt, 172pt) #head-r(460pt, 172pt) #lab(16pt, 160pt)[KICK]
#seg(14pt, 190pt, 460pt, 190pt) #head-r(460pt, 190pt) #lab(16pt, 193pt)[PAUSE]

// -- control <-> prescaler -------------------------------------------------
#seg(505pt, 190pt, 505pt, 220pt) #head-d(505pt, 220pt) #lab(478pt, 200pt)[en · clr]
#seg(560pt, 220pt, 560pt, 190pt) #head-u(560pt, 190pt) #lab(566pt, 200pt)[tick]

// -- output pins (right) ---------------------------------------------------
#seg(610pt, 80pt, 664pt, 80pt)  #head-r(664pt, 80pt)  #lab(622pt, 68pt)[IRQ]
#seg(610pt, 115pt, 664pt, 115pt) #head-r(664pt, 115pt) #lab(608pt, 103pt)[WDT\_RST\_N]

// -- footnote --------------------------------------------------------------
#lab(70pt, 250pt)[clk (50 MHz) and rst\_n go to every block.]
