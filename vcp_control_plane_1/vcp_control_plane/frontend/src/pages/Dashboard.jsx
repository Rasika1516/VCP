import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, ChevronDown, Send, FlaskConical, Upload, Radio } from 'lucide-react'
import { KpiCards } from '../components/dashboard/KpiCards.jsx'
import { ConnectionHealth } from '../components/dashboard/ConnectionHealth.jsx'
import { OperationalStates } from '../components/dashboard/OperationalStates.jsx'
import { EventStreamTable } from '../components/dashboard/EventStreamTable.jsx'
import { Dropdown } from '../components/common/Dropdown.jsx'
import { useFleetSummary } from '../hooks/useFleetSummary.js'
import { useWebSocket } from '../hooks/useWebSocket.js'
import './Dashboard.css'

const QUICK_ACTIONS = [
  { icon: Send, label: 'Dispatch bulk command', to: '/fleet' },
  { icon: FlaskConical, label: 'Run scenario', to: '/scenarios' },
  { icon: Upload, label: 'Import profile', to: '/profiles' },
  { icon: Radio, label: 'Open live events', to: '/events' },
]

export default function Dashboard() {
  const navigate = useNavigate()
  const { summary, loading, error, refresh } = useFleetSummary(10_000)
  const { frames, connected, clear } = useWebSocket({}, 500)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  // Each successful poll yields a new summary object
  useEffect(() => {
    if (summary) setLastUpdated(new Date())
  }, [summary])

  async function handleRefresh() {
    setRefreshing(true)
    await refresh()
    setRefreshing(false)
  }

  return (
    <div className="page dashboard">
      <div className="dash-header">
        <div>
          <h1 className="dash-title">Dashboard</h1>
          <p className="dash-subtitle">Real-time overview of your virtual charge point fleet</p>
        </div>
        <div className="dash-actions">
          <span className="dash-updated">
            Last updated{' '}
            <strong>{lastUpdated ? lastUpdated.toLocaleTimeString() : '–'}</strong>
            <RefreshCw size={15} className={refreshing ? 'spin' : ''} />
          </span>
          <button className="btn-secondary dash-btn" onClick={handleRefresh} disabled={refreshing}>
            <RefreshCw size={16} /> Refresh
          </button>
          <Dropdown
            trigger={({ toggle }) => (
              <button className="btn-primary dash-btn" onClick={toggle}>
                Quick actions <ChevronDown size={16} />
              </button>
            )}
          >
            {QUICK_ACTIONS.map(({ icon: Icon, label, to }) => (
              <button key={label} className="dd-item" onClick={() => navigate(to)}>
                <Icon size={15} /> {label}
              </button>
            ))}
          </Dropdown>
        </div>
      </div>

      {error && <div className="dash-error">Could not load fleet summary: {error}</div>}

      {loading ? (
        <div className="page-loading">Loading…</div>
      ) : (
        <div className="dash-body">
          <KpiCards summary={summary} />
          <div className="dash-analytics">
            <ConnectionHealth summary={summary} />
            <OperationalStates summary={summary} />
          </div>
          <EventStreamTable frames={frames} connected={connected} onClear={clear} />
        </div>
      )}
    </div>
  )
}
