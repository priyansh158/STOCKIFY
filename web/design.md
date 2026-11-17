# Design: STOCKIFY

The locked system. Every Hallmark run reads this first and pages share it. Change it on purpose; the file is the rule.

## System
- Genre: modern-minimal (a working tool, not a marketing page)
- Shape of the first page: app screen (a sticky top bar on `--color-paper-2`, a step above the page, then tables, forms and stat
  rows; no hero)
- Theme: brand. STOCKIFY green on a green-tinted ink, kept from the original app; the Matrix grid and the glow were cut
- Axes: dark/grotesk-sans/chromatic-green. Posture: restrained. Two greens with two jobs (owner, 2026-11-17): the saturated green
  (`--color-up`) means a gain and nothing else; the accent, a pale sage (`--color-accent`), marks the primary action, the current
  page, keyboard focus, selection and the main chart line. Neither is spent on decoration
- Desktop only (owner, 2026-11-12). Built for a laptop browser, readable down to about 1024 px. Wide tables are fine.

## Tokens
`src/app/tokens.css` is the source of truth (Tailwind `@theme static`), `src/app/base.css` the base rules. The roles:
```css
:root {
  --color-paper: oklch(17% 0.012 160);  --color-paper-2: oklch(21% 0.014 160);  --color-paper-3: oklch(26% 0.016 160);
  --color-ink: oklch(95% 0.01 150);  --color-ink-2: oklch(84% 0.015 155);  --color-muted: oklch(71% 0.02 155);
  --color-rule: oklch(33% 0.018 160);  --color-edge: oklch(56% 0.02 155);   /* edge: field and button borders */
  --color-accent: oklch(87% 0.05 168);  --color-accent-ink: oklch(20% 0.04 150);  --color-focus: var(--color-accent);
  --color-up / --color-down / --color-warn / --color-info   /* gain, loss, needs you, for your information */
  --font-body: Inter;  --font-display: Inter (same face, one weight);  --font-outlier: JetBrains Mono (figures and ids only);
  --radius-card: 8px;  --radius-control: 6px;  --rule-hair: 1px;  --shadow-card: none;
}
```
Headings (owner, 2026-11-17): h1 `--text-display` 1.75rem, h2 `--text-section` 1.2rem, h3 `--text-title` 1.125rem, h4 and body
`--text-md` 1rem, so each of the first three levels differs from body by size, not by weight alone.
The spacing scale (`--space-*`), the type scale (`--text-*`, ratio 1.2), the easings and durations are in `tokens.css`. Tints (`--tint-*`) are
built with `color-mix` in `globals.css`. Never a colour or font literal outside the token block, never `#000` or `#fff`.

## Rules
- Labels, captions, table heads, chips and nav links are the body face, sentence case, untracked (no tracked capitals anywhere). The
  STOCKIFY name is shown only as the logo image (`public/brand/`, D-149), never set in type.
- Mono is for figures, ids and code only. Every money and quantity column is right-aligned with tabular numerals.
- Money is the API's paise shown as rupees by `src/lib/format.ts` (`Intl.NumberFormat("en-IN")`, ₹). Plain words everywhere: no raw codes
  (a status, a parity result, a margin step) reach the screen; map them in `src/lib/trade.ts` or `src/app/tests/ui.tsx`.
- Gains are `--color-up`, losses `--color-down`; colour is never the only signal (the sign or a word is shown too).
- Tables before cards. One container (`--container`, 1360 px), one gap scale. A card never sits inside a card: a table frame, chart
  stage, notice or panel placed inside a `Panel` drops its own border (globals.css does this; a notice keeps its tint as a well).
- Long pages of results (the run page, the paper session page) are ruled, not boxed (owner, 2026-11-17): `.stk-stack[data-ruled]`
  separates its sections with a hairline and space. A `Panel` is kept only for notices (running, failed, errors) and the chart stage.
- Every region that loads data has four states: loading (a `Skeleton` in the shape of what comes), empty (`Empty`: one left-aligned
  line at the reading measure, with the next action, and no box around it), error (a red `Panel`), full. Show only real API data; no placeholder numbers.
- Long state grids (data coverage), caveat lists and every status column in a table are a ledger, not a wall of boxes: the state is
  its word (and glyph, where it has one) in the state's colour, with no frame or fill. The full chip (frame and tint) is for a page's
  own head and single states outside tables (owner, 2026-11-17).
- A long ledger (the data coverage grid, the tests list, a run's fills) is a `Table tall` (owner, 2026-11-17): its frame is at most
  `min(75vh, 48rem)` high and scrolls on its own, sideways too, with the column heads stuck to its top. Short tables stay plain.
- Mono is for figures: a value that has no digit in it ("too short to say") is set in the body face, and so are the words under a figure.
- A money headline that moves over time carries its recent history as a `Spark` (96 by 28 px, the accent line, a dashed level
  and a dot at the start) only where a real series already reaches the page: today the run page's "Ended with", from the
  day-end equity. Never a placeholder line; Portfolio "Total value" and a paper session's "Made or lost today" have no series yet,
  so they show none (owner, 2026-11-17).
- One date format wherever a person reads a date: "5 Oct 2026", with ", 14:45" or ", 14:45 IST" where the time matters (`dayWords`,
  `istWhen` in `src/lib/format.ts`). ISO stays only where the date is an identifier. A fact shared by every row goes in the column head,
  once; a column that is empty in every row is not drawn; hashes and build ids keep their own case in the figure face.
- No control anywhere places a real order. There is no site footer (owner, D-141); the paper-trading pages say "no real orders" where
  orders appear, and the chart keeps the TradingView attribution logo.

## Controls
- Primary: filled accent, accent-ink text, 6 px radius, 36 px high with a 44 px hit target. One primary per screen region.
- Secondary: 1 px edge outline, same radius and height. Danger and warn are outlines in `--color-down` and `--color-warn`.
- Fields: 36 px, `--color-edge` border, `aria-invalid` turns it red. Focus is a 2 px `--color-focus` outline (the accent), instant.
- A tick box in a table cell is wrapped in a `<label>` that fills the cell (`.stk-pick-hit`, at least 44 px), so the whole cell
  ticks it (owner, 2026-11-17).
- Current page in the top bar: the `paper-3` fill and a 2 px accent bar along its foot; same weight as the other links.
- Accent contrast (owner, 2026-11-17): 13.1:1 on paper, 12.2:1 on paper-2, 10.6:1 on paper-3; accent-ink on the accent 12.4:1.
- Components live in `src/components/ui.tsx` (Panel, PageHead, Stat/StatGrid, Table, Field, Chip, Skeleton, Empty) and the classes
  `.stk-*` in `src/app/globals.css`.

## Motion
- Character: still. Colour and a 1 px press only, over `--dur-micro`; the skeleton pulse is the one animation. Under reduced motion
  the 1 px press and every movement stop; the short colour fades stay, since they carry hover and press feedback (owner,
  2026-11-17).

## Exports
`tokens.css` in this project. Tailwind utilities come from its `@theme static` block (`bg-paper`, `text-ink`, `font-outlier`).
