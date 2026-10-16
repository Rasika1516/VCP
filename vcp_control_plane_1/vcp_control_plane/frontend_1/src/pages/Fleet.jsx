import { useState } from 'react'
import { Header } from '../components/layout/Header.jsx'
import { useFleetSummary } from '../hooks/useFleetSummary.js'
import { fleetApi } from '../api/fleet.js'
import './Fleet.css'

export default function Fleet() {
  const { summary, loading, refresh } = useFleetSummary(15_000)
  const [cmd, setCmd] = useState({ commandType: 'reset', payload: '{"type":"Soft"}', groupId: '' })
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)

  async function sendBulk() {
    setSending(true)
    setErr(null)
    setResult(null)
    try {
      const payload = JSON.parse(cmd.payload || '{}')
      const body = {
        // Pasted values can carry tabs/spaces, which the worker rejects as an unknown command
        commandType: cmd.commandType.trim(),
        payload,
        issuedBy: 'ui',
        ...(cmd.groupId ? { groupId: cmd.groupId } : {}),
      }
      const res = await fleetApi.bulkCommand(body)
      setResult(res)
    } catch (e) {
      setErr(e.message)
    } finally {
      setSending(false)
    }
  }

  const workers = summary?.shards || []

  return (
    <div className="page">
      <Header title="Fleet Overview" />
      <div className="fleet-page-grid">
        {/* Workers */}
        <section className="fleet-section">
          <h2 className="section-title">Workers</h2>
          {loading ? (
            <p className="page-loading">Loading…</p>
          ) : (
            <div className="workers-table">
              <div className="workers-head">
                <span>Worker</span>
                <span>Status</span>
                <span>VCPs</span>
              </div>
              {workers.length === 0 && (
                <div className="workers-empty">No workers configured.</div>
              )}
              {workers.map((w) => (
                <div key={w.shardIndex} className="workers-row">
                  <span className="worker-id">{w.workerId}</span>
                  <span className={`worker-status ${w.reachable ? 'worker-status--up' : 'worker-status--down'}`}>
                    {w.reachable ? 'Reachable' : 'Unreachable'}
                  </span>
                  <span>{w.vcpCount}</span>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Bulk command */}
        <section className="fleet-section">
          <h2 className="section-title">Bulk Command</h2>
          <div className="bulk-form">
            <label className="form-label">
              Command Type
              <input
                className="form-input"
                value={cmd.commandType}
                onChange={(e) => setCmd({ ...cmd, commandType: e.target.value })}
              />
            </label>
            <label className="form-label">
              Group ID (leave blank for fleet-wide)
              <input
                className="form-input"
                value={cmd.groupId}
                placeholder="optional"
                onChange={(e) => setCmd({ ...cmd, groupId: e.target.value })}
              />
            </label>
            <label className="form-label">
              Payload (JSON)
              <textarea
                className="form-textarea"
                rows={4}
                value={cmd.payload}
                onChange={(e) => setCmd({ ...cmd, payload: e.target.value })}
              />
            </label>
            <button className="btn-primary" disabled={sending} onClick={sendBulk}>
              {sending ? 'Sending…' : 'Dispatch'}
            </button>
            {err && <div className="form-error">{err}</div>}
            {result && (
              <div className="form-success">
                Dispatched {result.dispatched} command(s). IDs: {result.commandIds?.join(', ')}
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
