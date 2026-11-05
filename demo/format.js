// Corey Mathie, 2026
// Display formats for figures and money. Always en-US with a leading "$", whatever the browser locale:
// $940, $1,100, $180.50, $1.46M, $142K, $1.6B, and two decimals for unit prices under $10 ($8.01, $0.44, $0.50).
// Elements that hold these strings are marked translate="no" so page translation leaves them as written.

/** A count: 92400 -> "92,400". */
export const num = (n) => Number(n || 0).toLocaleString("en-US");

/** An amount: whole dollars without cents ($1,100), anything else with two decimals ($180.50). */
export function usd(n) {
  n = Number(n || 0);
  const whole = Number.isInteger(n);
  return "$" + n.toLocaleString("en-US", {minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2});
}

/** An amount with cents, for payments: $180.00. */
export const usdCents = (n) => "$" + Number(n || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});

/** Whole dollars, rounded: $93,412. */
export const money0 = (n) => "$" + Math.round(Number(n || 0)).toLocaleString("en-US");

/** A dashboard figure: $1.6B, $14.2M, $1.5M, $1.46M, $142K, $9,400. */
export function moneyShort(n) {
  n = Number(n || 0);
  const short = (x, digits) => String(Number(x.toFixed(digits))); // 1.50 -> "1.5", 1.46 -> "1.46"
  if (n >= 1e9) return `$${short(n / 1e9, 1)}B`;
  if (n >= 1e6) return `$${short(n / 1e6, n >= 1e7 ? 1 : 2)}M`;
  if (n >= 1e4) return `$${Math.round(n / 1e3)}K`;
  return money0(n);
}

/** A unit price: two decimals under $10 ($8.01, $0.44, $0.50), otherwise as usd(). */
export const unitPrice = (n) => (Number(n || 0) < 10 ? "$" + Number(n || 0).toFixed(2) : usd(n));

/** A figure inside a sentence that goes into innerHTML, kept out of page translation. */
export const fig = (html) => `<span translate="no">${html}</span>`;
