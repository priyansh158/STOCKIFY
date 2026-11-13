/** Display helpers: money arrives as integer paise and is shown in ₹ (en-IN grouping). */

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const INR_WHOLE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/** 1234567 paise → "₹12,345.67". */
export function rupees(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  return INR.format(paise / 100);
}

/** Whole rupees for big headline numbers: 100000000 paise → "₹10,00,000". */
export function rupeesWhole(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "—";
  return INR_WHOLE.format(paise / 100);
}

/** A fraction as a signed percentage: 0.1234 → "+12.34 %". */
export function pct(fraction: number | null | undefined, signed = false): string {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return "—";
  const v = (fraction * 100).toFixed(2);
  return `${signed && fraction > 0 ? "+" : ""}${v} %`;
}

/** The growth from start to end NAV, as a fraction, or null. */
export function growth(start?: number | null, end?: number | null): number | null {
  if (!start || end === null || end === undefined) return null;
  return end / start - 1;
}

/** An ISO IST timestamp ("2026-11-06T10:15:00+05:30") as "6 Nov 2026, 10:15 IST". */
export function istWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
  if (!m) return iso;
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(m[3])} ${months[Number(m[2]) - 1]} ${m[1]}, ${m[4]}:${m[5]} IST`;
}

/** Rupees typed by a person ("10,00,000" or "1500.5") → integer paise, or null when it is not a number. */
export function parseRupeesToPaise(text: string): number | null {
  const t = text.replace(/[₹,\s]/g, "");
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const [whole, frac = ""] = t.split(".");
  return Number(whole) * 100 + Number((frac + "00").slice(0, 2));
}
