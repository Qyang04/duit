const formatters = new Map<string, Intl.NumberFormat>();

function formatter(): Intl.NumberFormat {
  let f = formatters.get("en-MY");
  if (!f) {
    f = new Intl.NumberFormat("en-MY", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    formatters.set("en-MY", f);
  }
  return f;
}

/** 123456 -> "1,234.56". Sen are integers; dividing by 100 only happens for display. */
export function formatSen(sen: number): string {
  return formatter().format(Math.abs(sen) / 100);
}

export function signOf(sen: number): "+" | "−" {
  return sen < 0 ? "−" : "+"; // a real minus sign, not a hyphen
}

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

/** "2026-11-05" -> { day: "05", month: "NOV", key: "2026-11", long: "November 2026" } */
export function parts(isoDate: string) {
  const [y = "", m = "1", d = ""] = isoDate.split("-");
  const month = Number(m) - 1;
  const long = new Date(Number(y), month, 1).toLocaleString("en-MY", {
    month: "long",
    year: "numeric",
  });
  return { day: d, month: MONTHS[month] ?? "", key: `${y}-${m}`, long };
}

/** A small, stable tilt per id, so each stamp looks hand-pressed but never jumps around. */
export function tiltFor(id: string): number {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return ((Math.abs(h) % 7) - 3) * 0.9;
}
