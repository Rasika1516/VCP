import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { CONNECTION_COLORS, colorFor, orderedEntries, pct } from './stateColors.js'
import './DashboardWidgets.css'

const RADIUS = 66
const STROKE = 26
const CIRC = 2 * Math.PI * RADIUS

function Donut({ entries, total }) {
  let offset = 0
  return (
    <div className="donut">
      <svg viewBox="0 0 180 180" role="img" aria-label="Connection state distribution">
        <circle cx="90" cy="90" r={RADIUS} fill="none" stroke="var(--p3-queued-tint)" strokeWidth={STROKE} />
        {total > 0 &&
          entries.map(([state, count], i) => {
            if (!count) return null
            const len = (count / total) * CIRC
            const seg = (
              <circle
                key={state}
                cx="90"
                cy="90"
                r={RADIUS}
                fill="none"
                stroke={colorFor(state, CONNECTION_COLORS, i)}
                strokeWidth={STROKE}
                strokeDasharray={`${len} ${CIRC - len}`}
                strokeDashoffset={-offset}
                transform="rotate(-90 90 90)"
              >
                <title>{`${state}: ${count}`}</title>
              </circle>
            )
            offset += len
            return seg
          })}
      </svg>
      <div className="donut-center">
        <span className="donut-total">{total}</span>
        <span className="donut-label">Total VCPs</span>
      </div>
    </div>
  )
}

export function ConnectionHealth({ summary }) {
  const total = summary?.vcpCount ?? 0
  const entries = orderedEntries(summary?.connectionStateCounts, ['connected', 'disconnected', 'faulted'])

  return (
    <section className="dash-card">
      <div className="dash-card-head">
        <div>
          <h2 className="dash-card-title">Connection health</h2>
          <p className="dash-card-sub">Current connection status of all VCPs</p>
        </div>
        <Link to="/vcps" className="dash-link">
          View VCPs <ArrowRight size={15} />
        </Link>
      </div>

      <div className="conn-body">
        <Donut entries={entries} total={total} />
        <ul className="conn-legend">
          {entries.map(([state, count], i) => (
            <li key={state} className="conn-legend-row">
              <span className="conn-swatch" style={{ background: colorFor(state, CONNECTION_COLORS, i) }} />
              <span className="conn-name">{state}</span>
              <span className="conn-count">{count}</span>
              <span className="conn-pct">{pct(count, total)}%</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
