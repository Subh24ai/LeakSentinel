// Formatting helpers.
//
// IMPORTANT: money arrives as a fixed-2dp string (e.g. "7736.14"). We format it
// for display WITHOUT ever parsing to a float — grouping is done by string regex
// on the integer part, so rupee precision is never at the mercy of IEEE-754.

export function formatMoney(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const negative = value.startsWith("-");
  const raw = negative ? value.slice(1) : value;
  const [intPart, decRaw = "00"] = raw.split(".");
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const dec = (decRaw + "00").slice(0, 2);
  return `${negative ? "−" : ""}₹${grouped}.${dec}`;
}

export function formatInt(n: number): string {
  return n.toLocaleString("en-US");
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Turn an UPPER_SNAKE reason code into a readable label.
export function humanize(code: string | null | undefined): string {
  if (!code) return "—";
  return code
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
