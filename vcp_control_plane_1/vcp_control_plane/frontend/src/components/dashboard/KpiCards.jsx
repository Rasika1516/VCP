import { Cpu, Users, Wifi, WifiOff } from 'lucide-react'
import { pct } from './stateColors.js'
import './DashboardWidgets.css'

function KpiCard({ icon: Icon, tone, label, value, sub, progress }) {
  return (
    <div className="kpi-card">
      <span className={`kpi-icon kpi-icon--${tone}`}>
        <Icon size={26} />
      </span>
      <div className="kpi-body">
        <span className="kpi-label">{label}</span>
        <span className="kpi-value">{value ?? '–'}</span>
        {sub && <span className="kpi-sub">{sub}</span>}
        {progress !== undefined && (
          <div className="kpi-progress">
            <div className="kpi-track">
              <div className={`kpi-fill kpi-fill--${tone}`} style={{ width: `${progress}%` }} />
            </div>
            <span className="kpi-progress-pct">{progress}%</span>
          </div>
        )}
      </div>
    </div>
  )
}

export function KpiCards({ summary }) {
  const total = summary?.vcpCount ?? 0
  const conn = summary?.connectionStateCounts || {}
  const actual = summary?.actualStateCounts || {}
  const shards = summary?.shards || []
  const reachable = shards.filter((s) => s.reachable).length
  const connected = conn.connected || 0
  const disconnected = conn.disconnected || 0

  return (
    <div className="kpi-grid">
      <KpiCard
        icon={Cpu}
        tone="blue"
        label="Total VCPs"
        value={summary?.vcpCount}
        sub={`${actual.charging || 0} charging now`}
      />
      <KpiCard
        icon={Users}
        tone="purple"
        label="Workers"
        value={summary?.workerCount}
        sub={shards.length ? `${reachable} active` : null}
      />
      <KpiCard
        icon={Wifi}
        tone="green"
        label="Connected"
        value={connected}
        sub={`${pct(connected, total)}% of total`}
        progress={pct(connected, total)}
      />
      <KpiCard
        icon={WifiOff}
        tone="red"
        label="Disconnected"
        value={disconnected}
        sub={`${pct(disconnected, total)}% of total`}
        progress={pct(disconnected, total)}
      />
    </div>
  )
}
