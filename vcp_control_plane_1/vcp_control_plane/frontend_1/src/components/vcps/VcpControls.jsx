import { useState } from 'react'
import { vcpsApi } from '../../api/vcps.js'
import './VcpControls.css'

const QUICK_COMMANDS = [
  { label: 'Reset (Soft)', type: 'reset', payload: { type: 'Soft' } },
  { label: 'Reset (Hard)', type: 'reset', payload: { type: 'Hard' } },
  { label: 'Set Available', type: 'change_state', payload: { desired_state: 'Available' } },
  { label: 'Set Unavailable', type: 'change_state', payload: { desired_state: 'Unavailable' } },
  { label: 'Unlock Connector', type: 'unlock_connector', payload: { connector_id: 1 } },
  { label: 'Get Config', type: 'get_configuration', payload: {} },
]

export function VcpControls({ vcpId, onCommandSent }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  async function send(commandType, payload) {
    setBusy(commandType)
    setError(null)
    try {
      await vcpsApi.sendCommand(vcpId, {
        commandType: commandType,
        payload,
        issuedBy: 'ui',
      })
      onCommandSent?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="vcp-controls">
      <h3 className="vcp-controls-heading">Quick Commands</h3>
      {error && <div className="vcp-controls-error">{error}</div>}
      <div className="vcp-controls-grid">
        {QUICK_COMMANDS.map(({ label, type, payload }) => (
          <button
            key={label}
            className="ctrl-btn"
            disabled={busy !== null}
            onClick={() => send(type, payload)}
          >
            {busy === type ? 'Sending…' : label}
          </button>
        ))}
      </div>
    </div>
  )
}
