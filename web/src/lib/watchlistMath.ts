/**
 * The watchlist's small sums (CW 2), with no imports so a test can run them. Money is integer paise; a live price (Kite
 * sends rupees as a number) is turned into paise once, by `ltpPaise`.
 */

/** A live price in rupees as whole paise. */
export function ltpPaise(ltp: number): number {
  return Math.round(ltp * 100);
}

/** How far a price is below the 52-week high, as a fraction of the high (0.12 = 12 % below); null without both
 *  figures. Zero or less means the price is at or past the high. */
export function belowHigh(price: number | null | undefined, high: number | null | undefined): number | null {
  if (price == null || high == null || high <= 0) return null;
  return (high - price) / high;
}

/** How far a price is above the 52-week low, as a fraction of the low; null without both figures. Zero or less means
 *  the price is at or past the low. */
export function aboveLow(price: number | null | undefined, low: number | null | undefined): number | null {
  if (price == null || low == null || low <= 0) return null;
  return (price - low) / low;
}
