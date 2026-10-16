import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, Bell, ChevronDown, AlertTriangle, ServerCrash, Unplug } from 'lucide-react'
import { Dropdown } from '../common/Dropdown.jsx'
import { useControlPlane } from '../../context/ControlPlaneContext.jsx'
import './Topbar.css'

const HAS_TOKEN = !!import.meta.env.VITE_API_TOKEN

/** Build notifications from the live fleet summary — no static content. */
function buildNotices(online, loading, summary) {
  const notices = []
  if (!loading && !online) {
    notices.push({ icon: Unplug, text: 'Control plane is unreachable', to: '/fleet' })
  }
  for (const w of summary?.shards || []) {
    if (!w.reachable) notices.push({ icon: ServerCrash, text: `${w.workerId} is unreachable`, to: '/fleet' })
  }
  const faulted = summary?.actualStateCounts?.faulted || 0
  if (faulted > 0) {
    notices.push({ icon: AlertTriangle, text: `${faulted} VCP${faulted > 1 ? 's' : ''} in faulted state`, to: '/vcps' })
  }
  return notices
}

export function Topbar() {
  const { online, loading, summary } = useControlPlane()
  const navigate = useNavigate()
  const inputRef = useRef(null)
  const [query, setQuery] = useState('')

  // Ctrl/Cmd + K focuses global search
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  function submitSearch(e) {
    e.preventDefault()
    const term = query.trim()
    navigate(term ? `/vcps?q=${encodeURIComponent(term)}` : '/vcps')
  }

  const notices = buildNotices(online, loading, summary)
  const statusClass = loading ? 'cp-status--pending' : online ? 'cp-status--live' : 'cp-status--down'
  const statusText = loading ? 'Connecting…' : online ? 'Control Plane Live' : 'Control Plane Offline'

  return (
    <header className="topbar">
      <form className="topbar-search" onSubmit={submitSearch} role="search">
        <Search size={16} className="topbar-search-icon" />
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search VCPs, workers, events…"
          aria-label="Global search"
        />
        <kbd className="topbar-kbd">Ctrl + K</kbd>
      </form>

      <div className="topbar-right">
        <span className={`cp-status ${statusClass}`}>
          <span className="cp-status-dot" />
          {statusText}
        </span>

        <Dropdown
          trigger={({ toggle }) => (
            <button className="topbar-icon-btn" onClick={toggle} aria-label="Notifications">
              <Bell size={20} />
              {notices.length > 0 && <span className="topbar-bell-dot" />}
            </button>
          )}
        >
          <div className="dd-header">Notifications</div>
          {notices.length === 0 && <div className="dd-empty">You're all caught up.</div>}
          {notices.map(({ icon: Icon, text, to }) => (
            <button key={text} className="dd-item" onClick={() => navigate(to)}>
              <Icon size={15} /> {text}
            </button>
          ))}
        </Dropdown>

        <Dropdown
          trigger={({ toggle }) => (
            <button className="topbar-user" onClick={toggle}>
              <span className="topbar-avatar">A</span>
              <span className="topbar-user-name">Admin</span>
              <ChevronDown size={16} />
            </button>
          )}
        >
          <div className="dd-header">Signed in as Admin</div>
          <button className="dd-item" onClick={() => navigate('/fleet')}>Fleet overview</button>
          <button className="dd-item" onClick={() => navigate('/profiles')}>Charger profiles</button>
          <div className="dd-divider" />
          <div className="dd-note">API token: {HAS_TOKEN ? 'configured' : 'not set'}</div>
        </Dropdown>
      </div>
    </header>
  )
}
