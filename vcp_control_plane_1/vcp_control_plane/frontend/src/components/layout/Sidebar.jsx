import { NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Zap,
  Server,
  BookOpen,
  FlaskConical,
  Radio,
} from 'lucide-react'
import p3Logo from '../../assets/p3-logo.svg'
import { version } from '../../../package.json'
import { useControlPlane } from '../../context/ControlPlaneContext.jsx'
import './Sidebar.css'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard', end: true },
  { to: '/fleet', icon: Server, label: 'Fleet' },
  { to: '/vcps', icon: Zap, label: 'VCPs' },
  { to: '/profiles', icon: BookOpen, label: 'Profiles' },
  { to: '/scenarios', icon: FlaskConical, label: 'Scenarios' },
  { to: '/events', icon: Radio, label: 'Live Events' },
]

export function Sidebar() {
  const { online, loading } = useControlPlane()
  const statusKey = loading ? 'pending' : online ? 'up' : 'down'

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <img src={p3Logo} alt="P3" className="sidebar-logo-img" />
        <span>VCP Control</span>
      </div>
      <nav className="sidebar-nav">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `sidebar-link${isActive ? ' sidebar-link--active' : ''}`
            }
          >
            <Icon size={18} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">
        <div className="sidebar-status">
          <span className={`sidebar-status-dot sidebar-status-dot--${statusKey}`} />
          <div>
            <div className="sidebar-status-label">Control Plane</div>
            <div className={`sidebar-status-value sidebar-status-value--${statusKey}`}>
              {loading ? 'Connecting…' : online ? 'Connected' : 'Offline'}
            </div>
          </div>
        </div>
        <div className="sidebar-version">
          VCP Fleet Simulator
          <br />
          v{version}
        </div>
      </div>
    </aside>
  )
}
