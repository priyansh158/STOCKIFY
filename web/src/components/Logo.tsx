/**
 * STOCKIFY brand mark — green monospace text + accent glow.
 *
 * Stays a single component so brand changes happen in one place. The candle-
 * stick S-letter SVG from the legacy app is intentionally not ported here —
 * it was tuned for a 260px hero rendering, not the favicon/header sizes the
 * new app uses.
 */
export function Logo({ size = "lg" }: { size?: "sm" | "lg" }) {
  const fontSize = size === "lg" ? "text-3xl sm:text-5xl" : "text-xl sm:text-2xl";
  return (
    <h1
      className={`stk-mono font-bold ${fontSize}`}
      style={{
        color: "var(--color-green)",
        textShadow: "0 0 14px rgba(0,255,65,0.35)",
        letterSpacing: "0.06em",
      }}
    >
      STOCKIFY
    </h1>
  );
}
