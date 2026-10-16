import { Fragment, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Search, Pause, Play, Trash2, ArrowDown, ArrowUp, ChevronLeft, ChevronRight,
  ArrowRight, MoreHorizontal,
} from 'lucide-react'
import { Dropdown } from '../common/Dropdown.jsx'
import './DashboardWidgets.css'
import './EventStreamTable.css'

const PAGE_SIZE = 12

// ── Frame classification (defensive: frames are whatever the WS sends) ──────

function frameType(f) {
  const e = f.event || ''
  if (/\.req$|request/i.test(e)) return 'Request'
  if (/\.conf$|response|result/i.test(e)) return 'Response'
  if (typeof f.direction === 'string') return f.direction.charAt(0).toUpperCase() + f.direction.slice(1)
  return 'Event'
}

function frameStatus(f) {
  // Worker frames: command_result carries { result: { ok, error } }, state_change carries actual_state
  const result = f.payload?.result
  if (result && typeof result === 'object' && 'ok' in result) {
    return { tone: result.ok ? 'pass' : 'fail', raw: result.error ?? (result.ok ? 'ok' : 'failed') }
  }
  const raw = f.status ?? f.result ?? f.payload?.status ?? f.payload?.actual_state ?? f.payload?.result
  const s = String(raw ?? f.event ?? '').toLowerCase()
  let tone = 'info'
  if (/(fail|error|reject|fault|invalid|timeout)/.test(s)) tone = 'fail'
  else if (/(warn|unavailable|degraded|suspend)/.test(s)) tone = 'warn'
  else if (/(running|charging|started|progress|preparing|pending)/.test(s)) tone = 'running'
  else if (/(ok|pass|accept|success|complet|available|connected|ack)/.test(s)) tone = 'pass'
  return { tone, raw: raw ?? null }
}

const TONE_LABEL = { pass: 'PASS', fail: 'FAIL', warn: 'WARN', running: 'RUNNING', info: 'INFO' }

const fmtTime = (ts) =>
  ts ? new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '–'

function frameJson(f) {
  const { _id, ...rest } = f // eslint-disable-line no-unused-vars
  return JSON.stringify(rest, null, 2)
}

function pageWindow(page, totalPages, size = 5) {
  const start = Math.max(1, Math.min(page - Math.floor(size / 2), totalPages - size + 1))
  return Array.from({ length: Math.min(size, totalPages) }, (_, i) => start + i)
}

// ── Component ────────────────────────────────────────────────────────────────

export function EventStreamTable({ frames, connected, onClear }) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [eventFilter, setEventFilter] = useState('')
  const [vcpFilter, setVcpFilter] = useState('')
  const [paused, setPaused] = useState(false)
  const [snapshot, setSnapshot] = useState([])
  const [sortDesc, setSortDesc] = useState(true)
  const [page, setPage] = useState(1)
  const [expanded, setExpanded] = useState(() => new Set())

  const source = paused ? snapshot : frames

  // Frames that arrived while paused
  const pendingCount = useMemo(() => {
    if (!paused) return 0
    const lastId = snapshot[snapshot.length - 1]?._id
    const idx = lastId === undefined ? -1 : frames.findIndex((f) => f._id === lastId)
    // Not found: snapshot was empty, or its last frame rotated out of the buffer
    return idx === -1 ? frames.length : frames.length - idx - 1
  }, [paused, snapshot, frames])

  const eventTypes = useMemo(() => [...new Set(source.map((f) => f.event).filter(Boolean))].sort(), [source])
  const vcpIds = useMemo(() => [...new Set(source.map((f) => f.vcpId).filter(Boolean))].sort(), [source])

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = source.filter((f) => {
      if (eventFilter && f.event !== eventFilter) return false
      if (vcpFilter && f.vcpId !== vcpFilter) return false
      if (q) {
        const hay = `${f.event} ${f.vcpId} ${JSON.stringify(f.payload ?? '')}`.toLowerCase()
        if (!hay.includes(q)) return false
      }
      return true
    })
    // Frames arrive in chronological order; newest-first is just a reverse
    return sortDesc ? [...filtered].reverse() : filtered
  }, [source, search, eventFilter, vcpFilter, sortDesc])

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const current = Math.min(page, totalPages)
  const pageRows = rows.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE)
  const from = rows.length ? (current - 1) * PAGE_SIZE + 1 : 0
  const to = Math.min(current * PAGE_SIZE, rows.length)

  function togglePause() {
    if (paused) {
      setPaused(false)
    } else {
      setSnapshot(frames)
      setPaused(true)
    }
  }

  function handleClear() {
    onClear()
    setSnapshot([])
    setExpanded(new Set())
    setPage(1)
  }

  function toggleRow(id) {
    setExpanded((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const resetPage = (setter) => (e) => { setter(e.target.value); setPage(1) }

  return (
    <section className="es-card">
      <div className="es-head">
        <div className="es-title-wrap">
          <h2 className="es-title">
            Live event stream
            <span
              className={`es-dot ${paused ? 'es-dot--paused' : connected ? 'es-dot--live' : ''}`}
              title={paused ? 'Paused' : connected ? 'Connected' : 'Disconnected'}
            />
          </h2>
          <p className="es-sub">Real-time OCPP messages and events from VCPs</p>
        </div>

        <div className="es-toolbar">
          <label className="es-search">
            <Search size={16} />
            <input
              value={search}
              onChange={resetPage(setSearch)}
              placeholder="Search events…"
              aria-label="Search events"
            />
          </label>
          <select className="es-select" value={eventFilter} onChange={resetPage(setEventFilter)} aria-label="Event type">
            <option value="">All Events</option>
            {eventTypes.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
          <select className="es-select" value={vcpFilter} onChange={resetPage(setVcpFilter)} aria-label="VCP">
            <option value="">All VCPs</option>
            {vcpIds.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
          <button className="btn-primary es-btn" onClick={togglePause}>
            {paused ? <Play size={16} /> : <Pause size={16} />}
            {paused ? `Resume${pendingCount ? ` (${pendingCount})` : ''}` : 'Pause'}
          </button>
          <button className="btn-secondary es-btn" onClick={handleClear}>
            <Trash2 size={16} /> Clear
          </button>
        </div>
      </div>

      <div className="es-table-wrap">
        <table className="es-table">
          <thead>
            <tr>
              <th className="es-col-time">
                <button className="es-sort" onClick={() => setSortDesc((d) => !d)} title="Toggle sort order">
                  Time {sortDesc ? <ArrowDown size={14} /> : <ArrowUp size={14} />}
                </button>
              </th>
              <th>Event</th>
              <th>VCP</th>
              <th>Type</th>
              <th>Status</th>
              <th>Details</th>
              <th className="es-col-menu" aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 && (
              <tr>
                <td colSpan={7} className="es-empty">
                  {source.length === 0
                    ? connected ? 'No events yet. Waiting for live data…' : 'Event stream disconnected. Reconnecting…'
                    : 'No events match the current filters.'}
                </td>
              </tr>
            )}
            {pageRows.map((f) => {
              const { tone, raw } = frameStatus(f)
              const isOpen = expanded.has(f._id)
              return (
                <Fragment key={f._id}>
                  <tr className={isOpen ? 'es-row--open' : ''}>
                    <td className="es-time">{fmtTime(f.ts)}</td>
                    <td className="es-event">{f.event}</td>
                    <td>
                      {f.vcpId ? <Link to={`/vcps/${f.vcpId}`} className="es-vcp">{f.vcpId}</Link> : '–'}
                    </td>
                    <td>{frameType(f)}</td>
                    <td>
                      <span className={`es-badge es-badge--${tone}`} title={raw != null ? String(raw) : undefined}>
                        {TONE_LABEL[tone]}
                      </span>
                    </td>
                    <td>
                      <button className="es-details" onClick={() => toggleRow(f._id)} aria-expanded={isOpen}>
                        {'{…}'}
                      </button>
                    </td>
                    <td className="es-col-menu">
                      <Dropdown
                        trigger={({ toggle }) => (
                          <button className="es-menu-btn" onClick={toggle} aria-label="Row actions">
                            <MoreHorizontal size={16} />
                          </button>
                        )}
                      >
                        <button className="dd-item" onClick={() => toggleRow(f._id)}>
                          {isOpen ? 'Hide details' : 'View details'}
                        </button>
                        <button className="dd-item" onClick={() => navigator.clipboard?.writeText(frameJson(f))}>
                          Copy JSON
                        </button>
                        {f.vcpId && (
                          <button className="dd-item" onClick={() => navigate(`/vcps/${f.vcpId}`)}>
                            Open VCP
                          </button>
                        )}
                      </Dropdown>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr className="es-detail-row">
                      <td colSpan={7}>
                        <pre className="es-json">{frameJson(f)}</pre>
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="es-foot">
        <span className="es-count">
          Showing {from}–{to} of {rows.length} events
        </span>
        <div className="es-foot-right">
          {totalPages > 1 && (
            <nav className="es-pager" aria-label="Pagination">
              <button disabled={current === 1} onClick={() => setPage(current - 1)} aria-label="Previous page">
                <ChevronLeft size={16} />
              </button>
              {pageWindow(current, totalPages).map((n) => (
                <button
                  key={n}
                  className={n === current ? 'es-page--active' : ''}
                  onClick={() => setPage(n)}
                  aria-current={n === current ? 'page' : undefined}
                >
                  {n}
                </button>
              ))}
              <button disabled={current === totalPages} onClick={() => setPage(current + 1)} aria-label="Next page">
                <ChevronRight size={16} />
              </button>
            </nav>
          )}
          <Link to="/events" className="dash-link">
            View all events <ArrowRight size={15} />
          </Link>
        </div>
      </div>
    </section>
  )
}
