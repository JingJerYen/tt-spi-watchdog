// FSM diagram for the TT SPI watchdog datasheet.
// Build: typst compile state_machine.typ state_machine.svg
#set page(width: 690pt, height: 360pt, margin: 0pt, fill: white)
#set text(font: "Lato", size: 9pt, fill: rgb("#222222"))

#let sc = rgb("#555555")

// -- helpers ---------------------------------------------------------------
#let seg(x1, y1, x2, y2) = place(line(start: (x1, y1), end: (x2, y2), stroke: 1pt + sc))
#let head-r(x, y) = place(dx: x - 7pt, dy: y - 4pt, polygon(fill: sc, (0pt, 0pt), (7pt, 4pt), (0pt, 8pt)))
#let head-l(x, y) = place(dx: x, dy: y - 4pt, polygon(fill: sc, (7pt, 0pt), (0pt, 4pt), (7pt, 8pt)))
#let head-u(x, y) = place(dx: x - 4pt, dy: y, polygon(fill: sc, (0pt, 7pt), (4pt, 0pt), (8pt, 7pt)))
#let head-d(x, y) = place(dx: x - 4pt, dy: y - 7pt, polygon(fill: sc, (0pt, 0pt), (4pt, 7pt), (8pt, 0pt)))
#let lab(x, y, body) = place(dx: x, dy: y, text(size: 7.5pt, fill: rgb("#333333"), body))

#let state(x, y, fillc, name, sub: none) = place(dx: x, dy: y,
  rect(width: 110pt, height: 40pt, fill: fillc, stroke: 1pt + sc, radius: 10pt,
    align(center + horizon)[
      #text(weight: "bold", size: 10pt)[#name]
      #if sub != none [ #v(0pt) #text(size: 7pt, fill: rgb("#444444"))[#sub] ]
    ]))

// -- states ----------------------------------------------------------------
// top row: the normal life cycle; bottom row: the fault path
#state(60pt, 60pt, rgb("#e8f0fe"), [IDLE], sub: [config writable])
#state(280pt, 60pt, rgb("#fef7e0"), [EARLY], sub: [kicks are faults here])
#state(500pt, 60pt, rgb("#e6f4ea"), [NORMAL], sub: [kicks feed the dog])
#state(450pt, 230pt, rgb("#fde8e8"), [RESET_WAIT], sub: [grace: 2#super[16] clk])
#state(200pt, 230pt, rgb("#fbd3d0"), [RESET], sub: [rst low: 2#super[19] clk])

// -- entry -----------------------------------------------------------------
#place(dx: 16pt, dy: 76pt, circle(radius: 4pt, fill: sc))
#seg(24pt, 80pt, 60pt, 80pt) #head-r(60pt, 80pt)

// -- transitions -----------------------------------------------------------
// IDLE -> EARLY
#seg(170pt, 80pt, 280pt, 80pt) #head-r(280pt, 80pt)
#lab(180pt, 66pt)[KICK · window on]

// IDLE -> NORMAL, routed over the top
#seg(115pt, 60pt, 115pt, 30pt)
#seg(115pt, 30pt, 555pt, 30pt)
#seg(555pt, 30pt, 555pt, 60pt) #head-d(555pt, 60pt)
#lab(290pt, 18pt)[KICK · window off]

// EARLY -> NORMAL / NORMAL -> EARLY
#seg(390pt, 72pt, 500pt, 72pt) #head-r(500pt, 72pt)
#lab(405pt, 58pt)[early part passed]
#seg(500pt, 92pt, 390pt, 92pt) #head-l(390pt, 92pt)
#lab(415pt, 96pt)[KICK (feed)]

// NORMAL -> RESET_WAIT
#seg(540pt, 100pt, 540pt, 230pt) #head-d(540pt, 230pt)
#lab(548pt, 158pt)[timeout \ sets IRQ_FLAG]

// EARLY -> RESET_WAIT, routed between the rows
#seg(335pt, 100pt, 335pt, 165pt)
#seg(335pt, 165pt, 475pt, 165pt)
#seg(475pt, 165pt, 475pt, 230pt) #head-d(475pt, 230pt)
#lab(343pt, 143pt)[KICK too early \ sets EARLY_FLAG]

// RESET_WAIT -> RESET
#seg(450pt, 250pt, 310pt, 250pt) #head-l(310pt, 250pt)
#lab(338pt, 237pt)[grace over]

// RESET -> IDLE
#seg(200pt, 258pt, 115pt, 258pt)
#seg(115pt, 258pt, 115pt, 100pt) #head-u(115pt, 100pt)
#lab(122pt, 262pt)[pulse done]

// RESET_WAIT -> IDLE, routed along the bottom
#seg(505pt, 270pt, 505pt, 310pt)
#seg(505pt, 310pt, 80pt, 310pt)
#seg(80pt, 310pt, 80pt, 100pt) #head-u(80pt, 100pt)
#lab(150pt, 296pt)[W1C clears all flags (second chance) · or RST_EN = 0]

// -- footnote --------------------------------------------------------------
#lab(280pt, 336pt)[EN = 0 returns EARLY/NORMAL to IDLE. rst_n returns to IDLE from any state. \
With window off, a KICK in NORMAL stays in NORMAL.]
