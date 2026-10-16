import { createContext, useContext } from 'react'
import { useFleetSummary } from '../hooks/useFleetSummary.js'

/**
 * Shares control-plane reachability (derived from /fleet/summary) with the
 * app chrome — topbar status pill, notifications and sidebar footer.
 */
const ControlPlaneContext = createContext({ online: false, loading: true, summary: null, error: null })

export function ControlPlaneProvider({ children }) {
  const { summary, loading, error } = useFleetSummary(15_000)
  const online = !loading && !error && !!summary

  return (
    <ControlPlaneContext.Provider value={{ online, loading, summary, error }}>
      {children}
    </ControlPlaneContext.Provider>
  )
}

export function useControlPlane() {
  return useContext(ControlPlaneContext)
}
