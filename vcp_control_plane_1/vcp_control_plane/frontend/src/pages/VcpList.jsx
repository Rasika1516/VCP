import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Header } from '../components/layout/Header.jsx'
import { VcpCard } from '../components/vcps/VcpCard.jsx'
import { vcpsApi } from '../api/vcps.js'
import './VcpList.css'

export default function VcpList() {
  const [vcps, setVcps] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [searchParams] = useSearchParams()
  const [search, setSearch] = useState(searchParams.get('q') || '')
  const [groupFilter, setGroupFilter] = useState('')

  const PAGE_SIZE = 48

  // Global topbar search navigates here with ?q=
  useEffect(() => { setSearch(searchParams.get('q') || '') }, [searchParams])

  async function load(p = page) {
    setLoading(true)
    try {
      const data = await vcpsApi.list({
        page: p,
        pageSize: PAGE_SIZE,
        ...(groupFilter ? { groupId: groupFilter } : {}),
      })
      setVcps(data.items)
      setTotal(data.total)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load(1) }, [groupFilter]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(page) }, [page]) // eslint-disable-line react-hooks/exhaustive-deps

  const filtered = search
    ? vcps.filter(
        (v) =>
          v.vcpId.includes(search) ||
          v.chargePointId?.includes(search) ||
          v.groupId?.includes(search)
      )
    : vcps

  const totalPages = Math.ceil(total / PAGE_SIZE)

  return (
    <div className="page">
      <Header title="VCP Instances" />
      <div className="vcp-list-toolbar">
        <input
          className="toolbar-search"
          placeholder="Filter by ID, CP ID, group…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <input
          className="toolbar-search"
          placeholder="Group ID filter"
          value={groupFilter}
          onChange={(e) => { setGroupFilter(e.target.value); setPage(1) }}
        />
        <span className="toolbar-count">{total} VCPs</span>
      </div>

      {error && <div className="page-error">{error}</div>}

      {loading ? (
        <div className="page-loading">Loading VCPs…</div>
      ) : (
        <div className="vcp-grid">
          {filtered.map((vcp) => (
            <VcpCard key={vcp.vcpId} vcp={vcp} />
          ))}
          {filtered.length === 0 && (
            <div className="vcp-empty">No VCPs found.</div>
          )}
        </div>
      )}

      {totalPages > 1 && (
        <div className="pagination">
          <button disabled={page === 1} onClick={() => setPage(page - 1)}>←</button>
          <span>Page {page} of {totalPages}</span>
          <button disabled={page === totalPages} onClick={() => setPage(page + 1)}>→</button>
        </div>
      )}
    </div>
  )
}
