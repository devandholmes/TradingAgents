/**
 * Compare-series colours: categorical, assigned by selection slot (never by rank), validated as
 * an adjacent set on the card surface (CVD ΔE ≥ 8.4, contrast ≥ 3:1). Canvas can't read CSS
 * variables, hence hex. Green/red stay reserved for up/down.
 */
export const SERIES_COLORS = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#9085e9",
] as const
export const MAX_COMPARE = SERIES_COLORS.length

const finite = (n: number | null | undefined): n is number =>
  n != null && Number.isFinite(n)

export const fmtNum = (n: number | null | undefined, digits = 2) =>
  finite(n) ? n.toFixed(digits) : "—"

/** Drawdowns read as losses whatever sign the API uses. */
export const fmtDrawdown = (n: number | null | undefined) =>
  finite(n) ? `−${Math.abs(n).toFixed(1)}%` : "—"

/** Win rate arrives as a 0..1 fraction; tolerate a percentage too. */
export const fmtRate = (n: number | null | undefined) =>
  finite(n) ? `${(Math.abs(n) <= 1 ? n * 100 : n).toFixed(0)}%` : "—"

export const fmtUsdt = (n: number | null | undefined, digits = 2) =>
  finite(n)
    ? n.toLocaleString("en-US", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    : "—"

export const fmtSigned = (n: number | null | undefined, digits = 2) =>
  finite(n)
    ? `${n > 0 ? "+" : n < 0 ? "−" : ""}${fmtUsdt(Math.abs(n), digits)}`
    : "—"

export const tone = (n: number | null | undefined) =>
  !finite(n) || n === 0
    ? "text-muted-foreground"
    : n > 0
      ? "text-up"
      : "text-down"

const dateTime = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
})
const dateOnly = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
})

export const fmtWhen = (iso: string | null | undefined) =>
  iso ? dateTime.format(new Date(iso)) : "—"
export const fmtDay = (iso: string | null | undefined) =>
  iso ? dateOnly.format(new Date(iso)) : "—"

/** Normalised equity (100 = window start) as a return. */
export const fmtIndexed = (v: number) =>
  `${v >= 100 ? "+" : "−"}${Math.abs(v - 100).toFixed(1)}%`
