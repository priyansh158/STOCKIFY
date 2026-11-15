# Design: STOCKIFY

The locked system. Every Hallmark run reads this first and pages share it. Change it on purpose; the file is the rule.

## System
- Genre: modern-minimal (a working tool, not a marketing page)
- Shape of the first page: app screen (a sticky top bar, then tables, forms and stat rows; no hero)
- Theme: brand. STOCKIFY green on a green-tinted ink, kept from the original app; the Matrix grid and the glow were cut
- Axes: dark/grotesk-sans/chromatic-green. Posture: restrained. The accent marks the primary action, the current page and gains only
- Desktop only (owner, 2026-11-12). Built for a laptop browser, readable down to about 1024 px. Wide tables are fine.

## Tokens
`src/app/tokens.css` is the source of truth (Tailwind `@theme static`), `src/app/base.css` the base rules. The roles:
```css
:root {
  --color-paper: oklch(17% 0.012 160);  --color-paper-2: oklch(21% 0.014 160);  --color-paper-3: oklch(26% 0.016 160);
  --color-ink: oklch(95% 0.01 150);  --color-ink-2: oklch(84% 0.015 155);  --color-muted: oklch(71% 0.02 155);
  --color-rule: oklch(33% 0.018 160);  --color-edge: oklch(56% 0.02 155);   /* edge: field and button borders */
  --color-accent: oklch(80% 0.19 150);  --color-accent-ink: oklch(20% 0.04 150);  --color-focus: oklch(78% 0.12 240);
  --color-up / --color-down / --color-warn / --color-info   /* gain, loss, needs you, for your information */
  --font-body: Inter;  --font-display: Inter (same face, one weight);  --font-outlier: JetBrains Mono (figures and ids only);
  --radius-card: 8px;  --radius-control: 6px;  --rule-hair: 1px;  --shadow-card: none;
}
```
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
- Every region that loads data has four states: loading (a `Skeleton` in the shape of what comes), empty (`Empty`: one left-aligned
  line at the reading measure, with the next action), error (a red `Panel`), full. Show only real API data; no placeholder numbers.
- Long state grids (data coverage) and caveat lists in a table are a ledger, not a wall of boxes: the state is its glyph and word in
  the state's colour, with no frame or fill. The full chip (frame and tint) is for a page's own head and single states.
- Mono is for figures: a value that has no digit in it ("too short to say") is set in the body face, and so are the words under a figure.
- One date format wherever a person reads a date: "5 Oct 2026", with ", 14:45" or ", 14:45 IST" where the time matters (`dayWords`,
  `istWhen` in `src/lib/format.ts`). ISO stays only where the date is an identifier. A fact shared by every row goes in the column head,
  once; a column that is empty in every row is not drawn; hashes and build ids keep their own case in the figure face.
- No control anywhere places a real order. There is no site footer (owner, D-141); the paper-trading pages say "no real orders" where
  orders appear, and the chart keeps the TradingView attribution logo.

## Controls
- Primary: filled accent, accent-ink text, 6 px radius, 36 px high with a 44 px hit target. One primary per screen region.
- Secondary: 1 px edge outline, same radius and height. Danger and warn are outlines in `--color-down` and `--color-warn`.
- Fields: 36 px, `--color-edge` border, `aria-invalid` turns it red. Focus is a 2 px `--color-focus` outline, instant.
- Components live in `src/components/ui.tsx` (Panel, PageHead, Stat/StatGrid, Table, Field, Chip, Skeleton, Empty) and the classes
  `.stk-*` in `src/app/globals.css`.

## Motion
- Character: still. Colour and a 1 px press only, over `--dur-micro`; the skeleton pulse is the one animation. Under reduced motion
  everything ends at once.

## Exports
`tokens.css` in this project. Tailwind utilities come from its `@theme static` block (`bg-paper`, `text-ink`, `font-outlier`).
