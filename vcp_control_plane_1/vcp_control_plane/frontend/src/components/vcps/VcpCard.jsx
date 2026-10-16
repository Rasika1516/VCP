import { Link } from 'react-router-dom'
import { Zap, Wifi, WifiOff, AlertTriangle } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import clsx from 'clsx'
import './VcpCard.css'

const STATE_ICONS = {
  connected: <Wifi size={14} />,
  disconnected: <WifiOff size={14} />,
  faulted: <AlertTriangle size={14} />,
}

export function VcpCard({ vcp }) {
  const lastSeen = vcp.lastSeen
    ? formatDistanceToNow(new Date(vcp.lastSeen), { addSuffix: true })
    : 'never'

  return (
    <Link to={`/vcps/${vcp.vcpId}`} className="vcp-card">
      <div className="vcp-card-header">
        <span className="vcp-card-id">{vcp.vcpId}</span>
        <span
          className={clsx('vcp-conn-badge', {
            'vcp-conn-badge--connected': vcp.connectionState === 'connected',
            'vcp-conn-badge--disconnected': vcp.connectionState === 'disconnected',
            'vcp-conn-badge--faulted': vcp.connectionState === 'faulted',
          })}
        >
          {STATE_ICONS[vcp.connectionState] || <Zap size={14} />}
          {vcp.connectionState}
        </span>
      </div>

      <div className="vcp-card-row">
        <span className="vcp-card-meta">CP ID</span>
        <span className="vcp-card-val">{vcp.chargePointId}</span>
      </div>

      <div className="vcp-card-row">
        <span className="vcp-card-meta">State</span>
        <span
          className={clsx('vcp-state-chip', {
            'vcp-state-chip--charging': vcp.actualState === 'charging',
            'vcp-state-chip--idle': vcp.actualState === 'idle' || vcp.actualState === 'available',
            'vcp-state-chip--faulted': vcp.actualState === 'faulted',
          })}
        >
          {vcp.actualState}
        </span>
      </div>

      {vcp.groupId && (
        <div className="vcp-card-row">
          <span className="vcp-card-meta">Group</span>
          <span className="vcp-card-val">{vcp.groupId}</span>
        </div>
      )}

      <div className="vcp-card-footer">Last seen {lastSeen}</div>
    </Link>
  )
}
