// Colors for connection / operational states, shared by donut, bars and legend.
export const STATE_COLORS = {
  connected: 'var(--p3-pass)',
  disconnected: 'var(--p3-fail)',
  faulted: 'var(--p3-fail)',
  connecting: 'var(--p3-dodger-blue)',
  idle: 'var(--p3-queued)',
  available: 'var(--p3-pass)',
  preparing: 'var(--p3-dodger-blue)',
  authorized: 'var(--p3-dodger-blue)',
  charging: 'var(--p3-running)',
  suspended: 'var(--p3-warn)',
  finishing: 'var(--p3-warn)',
  offline: '#B4BBC8',
  unknown: '#B4BBC8',
}

// Connection donut: faulted connections shown in gray to stay distinct from disconnected
export const CONNECTION_COLORS = { ...STATE_COLORS, faulted: 'var(--p3-queued)' }

const FALLBACK = ['var(--p3-dodger-blue)', 'var(--p3-warn)', 'var(--p3-stratos)', '#B4BBC8']

export function colorFor(state, palette = STATE_COLORS, i = 0) {
  return palette[state] || FALLBACK[i % FALLBACK.length]
}

/** Merge expected states (shown even at 0) with whatever the API returned. */
export function orderedEntries(counts = {}, expected = []) {
  const keys = [...expected, ...Object.keys(counts).filter((k) => !expected.includes(k))]
  return keys.map((k) => [k, counts[k] || 0])
}

export const pct = (n, total) => (total > 0 ? Math.round((n / total) * 100) : 0)
