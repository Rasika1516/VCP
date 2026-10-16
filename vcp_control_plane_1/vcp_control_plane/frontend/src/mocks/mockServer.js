/**
 * Mock backend — FOR UI TESTING ONLY.
 *
 * Intercepts every `fetch` to `/api/v1/...` and the `/events/ws` WebSocket and
 * answers from in-memory fake data, so the whole UI can be exercised without
 * the real control plane running.
 *
 * Enable:  keep `import './mocks/mockServer.js'` in src/main.jsx
 * Disable: comment that one line out — nothing else in the app depends on this file.
 *
 * Non-API requests (Vite assets, HMR WebSocket, fonts) pass through untouched.
 */

const API_PREFIX = '/api/v1'
const LATENCY_MS = 300

// ── Fake data ────────────────────────────────────────────────────────────────

const WORKERS = ['worker-0', 'worker-1', 'worker-2']
const GROUPS = ['group-north', 'group-south', 'group-depot']
const CONN_STATES = ['connected', 'connected', 'connected', 'disconnected', 'faulted']
const ACTUAL_STATES = ['idle', 'idle', 'charging', 'charging', 'faulted', 'unknown']

const minutesAgo = (m) => new Date(Date.now() - m * 60_000).toISOString()
const pick = (arr, i) => arr[i % arr.length]
const rand = (arr) => arr[Math.floor(Math.random() * arr.length)]
let idSeq = 1000
const nextId = (prefix) => `${prefix}-${++idSeq}`

const profiles = [
  { profileId: 'abb-terra-54', vendor: 'ABB', model: 'Terra 54', version: '1.2.0', ocppVersion: '1.6', status: 'active' },
  { profileId: 'alfen-eve-single', vendor: 'Alfen', model: 'Eve Single Pro', version: '2.0.1', ocppVersion: '1.6', status: 'active' },
  { profileId: 'kempower-s-series', vendor: 'Kempower', model: 'S-Series', version: '3.1.0', ocppVersion: '2.0.1', status: 'active' },
  { profileId: 'legacy-generic', vendor: 'Generic', model: 'AC 22kW', version: '0.9.0', ocppVersion: '1.6', status: 'deprecated' },
]

const vcps = Array.from({ length: 60 }, (_, i) => {
  const n = String(i + 1).padStart(3, '0')
  const profile = pick(profiles.slice(0, 3), i)
  const actualState = pick(ACTUAL_STATES, i)
  return {
    vcpId: `vcp-${n}`,
    chargePointId: `CP-${n}`,
    profileId: profile.profileId,
    ocppVersion: profile.ocppVersion,
    workerId: pick(WORKERS, i),
    groupId: pick(GROUPS, i),
    desiredState: 'running',
    actualState,
    connectionState: pick(CONN_STATES, i),
    lastSeen: minutesAgo(i % 30),
    tags: i % 4 === 0 ? ['demo', 'dc-fast'] : ['demo'],
    connectorState: {
      1: { status: actualState === 'charging' ? 'Charging' : 'Available', powerKw: actualState === 'charging' ? 22 : 0 },
      2: { status: 'Available', powerKw: 0 },
    },
  }
})

const commandsByVcp = {}
vcps.slice(0, 10).forEach((v, i) => {
  commandsByVcp[v.vcpId] = [
    { commandId: nextId('cmd'), commandType: 'reset', status: 'completed', issuedAt: minutesAgo(5 + i) },
    { commandId: nextId('cmd'), commandType: 'get_configuration', status: i % 3 === 0 ? 'failed' : 'completed', issuedAt: minutesAgo(20 + i) },
  ]
})

const scenarios = [
  {
    scenarioId: 'boot-storm',
    name: 'Boot Storm',
    description: 'All targeted VCPs reboot and send BootNotification at the same time.',
    parameters: [
      { name: 'rampSeconds', description: 'Spread the boots over this many seconds', default: 10 },
    ],
  },
  {
    scenarioId: 'charging-session',
    name: 'Charging Session',
    description: 'Start a transaction, send meter values, then stop.',
    parameters: [
      { name: 'durationMinutes', description: 'Session length', default: 15 },
      { name: 'powerKw', description: 'Charging power', default: 22 },
    ],
  },
  {
    scenarioId: 'network-flap',
    name: 'Network Flap',
    description: 'Drop and restore the WebSocket connection repeatedly.',
    parameters: [{ name: 'cycles', description: 'Number of disconnect cycles', default: 3 }],
  },
]

const executions = [
  { executionId: nextId('exec'), scenarioId: 'charging-session', targetScope: 'group', targetCount: 20, status: 'running', startTime: minutesAgo(2) },
  { executionId: nextId('exec'), scenarioId: 'boot-storm', targetScope: 'fleet', targetCount: 60, status: 'completed', startTime: minutesAgo(45) },
  { executionId: nextId('exec'), scenarioId: 'network-flap', targetScope: 'single', targetCount: 1, status: 'failed', startTime: minutesAgo(90) },
]

function countBy(items, key) {
  return items.reduce((acc, item) => {
    acc[item[key]] = (acc[item[key]] || 0) + 1
    return acc
  }, {})
}

function fleetSummary() {
  return {
    vcpCount: vcps.length,
    workerCount: WORKERS.length,
    connectionStateCounts: countBy(vcps, 'connectionState'),
    actualStateCounts: countBy(vcps, 'actualState'),
    shards: WORKERS.map((workerId, shardIndex) => ({
      shardIndex,
      workerId,
      reachable: shardIndex !== 2, // show one unreachable worker
      vcpCount: vcps.filter((v) => v.workerId === workerId).length,
    })),
  }
}

function addCommand(vcpId, body) {
  const cmd = {
    commandId: nextId('cmd'),
    commandType: body.commandType,
    status: 'pending',
    issuedAt: new Date().toISOString(),
  }
  ;(commandsByVcp[vcpId] ||= []).unshift(cmd)
  setTimeout(() => { cmd.status = 'completed' }, 3000)
  return cmd
}

// ── Routes ───────────────────────────────────────────────────────────────────

class HttpError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
  }
}

function findVcp(id) {
  const vcp = vcps.find((v) => v.vcpId === id)
  if (!vcp) throw new HttpError(404, `VCP ${id} not found`)
  return vcp
}

// Each route: [method, regex on path after /api/v1, handler(match, ctx)]
const routes = [
  // Fleet
  ['GET', /^\/fleet\/summary$/, () => fleetSummary()],
  ['GET', /^\/fleet\/workers$/, () => fleetSummary().shards],
  ['POST', /^\/fleet\/commands$/, (_, { body }) => {
    const targets = body.groupId ? vcps.filter((v) => v.groupId === body.groupId) : vcps
    const commandIds = targets.map((v) => addCommand(v.vcpId, body).commandId)
    return { dispatched: commandIds.length, commandIds }
  }],

  // VCPs
  ['GET', /^\/vcps$/, (_, { query }) => {
    const page = Number(query.get('page') || 1)
    const pageSize = Number(query.get('pageSize') || 50)
    const groupId = query.get('groupId')
    const filtered = groupId ? vcps.filter((v) => v.groupId.includes(groupId)) : vcps
    return {
      items: filtered.slice((page - 1) * pageSize, page * pageSize),
      total: filtered.length,
      page,
      pageSize,
    }
  }],
  ['POST', /^\/vcps$/, (_, { body }) => {
    const vcp = { ...vcps[0], ...body, vcpId: body.vcpId || nextId('vcp'), lastSeen: null }
    vcps.push(vcp)
    return vcp
  }],
  ['GET', /^\/vcps\/([^/]+)$/, ([, id]) => findVcp(id)],
  ['PATCH', /^\/vcps\/([^/]+)$/, ([, id], { body }) => Object.assign(findVcp(id), body)],
  ['DELETE', /^\/vcps\/([^/]+)$/, ([, id]) => {
    vcps.splice(vcps.indexOf(findVcp(id)), 1)
    return null
  }],
  ['GET', /^\/vcps\/([^/]+)\/commands$/, ([, id], { query }) => {
    findVcp(id)
    return (commandsByVcp[id] || []).slice(0, Number(query.get('limit') || 50))
  }],
  ['POST', /^\/vcps\/([^/]+)\/commands$/, ([, id], { body }) => {
    findVcp(id)
    return addCommand(id, body)
  }],

  // Profiles
  ['GET', /^\/profiles$/, () => ({ items: profiles, total: profiles.length })],
  ['POST', /^\/profiles$/, (_, { body }) => {
    const p = { status: 'active', ...body }
    profiles.push(p)
    return p
  }],
  ['POST', /^\/profiles\/import-yaml$/, (_, { body }) => {
    const file = body instanceof FormData ? body.get('file') : null
    const name = (file?.name || 'uploaded.yaml').replace(/\.ya?ml$/i, '')
    const p = { profileId: name, vendor: 'Imported', model: name, version: '1.0.0', ocppVersion: '1.6', status: 'active' }
    profiles.push(p)
    return p
  }],
  ['POST', /^\/profiles\/import-dir$/, () => ({ imported: 0 })],
  ['GET', /^\/profiles\/([^/]+)$/, ([, id]) => {
    const p = profiles.find((x) => x.profileId === id)
    if (!p) throw new HttpError(404, `Profile ${id} not found`)
    return p
  }],
  ['DELETE', /^\/profiles\/([^/]+)$/, ([, id]) => {
    const p = profiles.find((x) => x.profileId === id)
    if (!p) throw new HttpError(404, `Profile ${id} not found`)
    p.status = 'deprecated'
    return null
  }],

  // Scenarios
  ['GET', /^\/scenarios$/, () => scenarios],
  ['POST', /^\/scenarios\/run$/, (_, { body }) => {
    const targetCount =
      body.targetScope === 'single' ? 1
        : body.targetScope === 'group' ? vcps.filter((v) => v.groupId === body.targetGroupId).length
          : vcps.length
    const exec = {
      executionId: nextId('exec'),
      scenarioId: body.scenarioId,
      targetScope: body.targetScope,
      targetCount,
      status: 'running',
      startTime: new Date().toISOString(),
    }
    executions.unshift(exec)
    setTimeout(() => { if (exec.status === 'running') exec.status = 'completed' }, 30_000)
    return exec
  }],
  ['GET', /^\/scenarios\/executions$/, () => ({ items: executions, total: executions.length })],
  ['GET', /^\/scenarios\/executions\/([^/]+)$/, ([, id]) => {
    const e = executions.find((x) => x.executionId === id)
    if (!e) throw new HttpError(404, `Execution ${id} not found`)
    return e
  }],
  ['POST', /^\/scenarios\/executions\/([^/]+)\/cancel$/, ([, id]) => {
    const e = executions.find((x) => x.executionId === id)
    if (!e) throw new HttpError(404, `Execution ${id} not found`)
    e.status = 'cancelled'
    return e
  }],
]

// ── fetch interception ───────────────────────────────────────────────────────

const realFetch = window.fetch.bind(window)

function jsonResponse(status, data) {
  if (data === null) return new Response(null, { status: 204 })
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

window.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url, window.location.origin)
  const idx = url.pathname.indexOf(API_PREFIX)
  if (idx === -1) return realFetch(input, init)

  const path = url.pathname.slice(idx + API_PREFIX.length)
  const method = (init.method || 'GET').toUpperCase()
  let body = init.body
  if (typeof body === 'string') {
    try { body = JSON.parse(body) } catch (_) {}
  }

  await new Promise((r) => setTimeout(r, LATENCY_MS))

  for (const [m, re, handler] of routes) {
    const match = m === method && path.match(re)
    if (!match) continue
    try {
      const data = handler(match, { body: body || {}, query: url.searchParams })
      console.debug('[mock]', method, path, data)
      return jsonResponse(200, data)
    } catch (err) {
      return jsonResponse(err.status || 500, { detail: err.message })
    }
  }
  console.warn('[mock] no route for', method, path)
  return jsonResponse(404, { detail: `Mock: no route for ${method} ${path}` })
}

// ── WebSocket interception (/events/ws only) ─────────────────────────────────

const RealWebSocket = window.WebSocket
const EVENT_TYPES = ['status_notification', 'meter_values', 'heartbeat_ack', 'boot_notification', 'transaction_started', 'transaction_stopped']

function fakeFrame(vcp) {
  const event = rand(EVENT_TYPES)
  const payload = {
    status_notification: { connectorId: 1, status: rand(['Available', 'Preparing', 'Charging', 'Faulted']) },
    meter_values: { connectorId: 1, energyWh: Math.round(Math.random() * 50_000), powerKw: +(Math.random() * 22).toFixed(1) },
    heartbeat_ack: {},
    boot_notification: { vendor: 'ABB', model: 'Terra 54' },
    transaction_started: { transactionId: Math.floor(Math.random() * 10_000), idTag: 'RFID-1234' },
    transaction_stopped: { reason: 'Local' },
  }[event]
  return { event, vcpId: vcp.vcpId, groupId: vcp.groupId, ts: new Date().toISOString(), payload }
}

class MockWebSocket {
  constructor(url, protocols) {
    if (!String(url).includes('/events/ws')) return new RealWebSocket(url, protocols)

    const params = new URL(url).searchParams
    this.filterVcp = params.get('vcpId')
    this.filterGroup = params.get('groupId')
    this.filterEvents = params.get('events')?.split(',').map((s) => s.trim()).filter(Boolean)

    this.url = url
    this.readyState = 0
    this.onopen = this.onmessage = this.onclose = this.onerror = null

    setTimeout(() => {
      this.readyState = 1
      this.onopen?.({})
      this.timer = setInterval(() => this._emit(), 1500)
    }, 200)
  }

  _emit() {
    let pool = vcps
    if (this.filterVcp) pool = pool.filter((v) => v.vcpId.includes(this.filterVcp))
    if (this.filterGroup) pool = pool.filter((v) => v.groupId.includes(this.filterGroup))
    if (pool.length === 0) return
    const frame = fakeFrame(rand(pool))
    if (this.filterEvents?.length && !this.filterEvents.includes(frame.event)) return
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  send() {}

  close() {
    if (this.readyState >= 2) return
    clearInterval(this.timer)
    this.readyState = 3
    // Note: useWebSocket reconnects on close; that is expected behaviour.
    this.onclose?.({})
  }
}
MockWebSocket.CONNECTING = 0
MockWebSocket.OPEN = 1
MockWebSocket.CLOSING = 2
MockWebSocket.CLOSED = 3

window.WebSocket = MockWebSocket

console.info('%c[mock] Mock backend ENABLED — comment out the import in src/main.jsx to use the real API', 'color:#e67e22;font-weight:bold')
