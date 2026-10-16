import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { colorFor, orderedEntries, pct } from './stateColors.js'
import './DashboardWidgets.css'

export function OperationalStates({ summary }) {
  const total = summary?.vcpCount ?? 0
  const entries = orderedEntries(summary?.actualStateCounts, ['available', 'charging', 'faulted', 'offline'])

  return (
    <section className="dash-card">
      <div className="dash-card-head">
        <div>
          <h2 className="dash-card-title">Operational states</h2>
          <p className="dash-card-sub">Current operational status of VCPs</p>
        </div>
        <Link to="/vcps" className="dash-link">
          View details <ArrowRight size={15} />
        </Link>
      </div>

      <ul className="ops-list">
        {entries.map(([state, count], i) => (
          <li key={state} className="ops-row">
            <span className="ops-name">{state}</span>
            <div className="ops-track">
              <div
                className="ops-fill"
                style={{ width: `${pct(count, total)}%`, background: colorFor(state, undefined, i) }}
              />
            </div>
            <span className="ops-count">{count}</span>
            <span className="ops-pct">{pct(count, total)}%</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
