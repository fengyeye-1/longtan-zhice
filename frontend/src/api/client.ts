const BASE = ''

const SESSION_KEY = 'ltzs_sid'
const TOKEN_KEY = 'ltzs_token'

function getSessionId(): string {
  try {
    return localStorage.getItem(SESSION_KEY) || ''
  } catch {
    return ''
  }
}

function setSessionId(id: string) {
  try {
    if (id) localStorage.setItem(SESSION_KEY, id)
  } catch { /* */ }
}

export function getProjectToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setProjectToken(token: string | null | undefined) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch { /* */ }
}

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = {}
  const sid = getSessionId()
  if (sid) h['X-Session-Id'] = sid
  const tok = getProjectToken()
  if (tok) h['X-Project-Token'] = tok
  return h
}

function captureAuth(res: Response) {
  const sid = res.headers.get('X-Session-Id')
  if (sid) setSessionId(sid)
  // 空 token 必须清掉旧凭证，否则切回公开示范后会把后续请求又带到旧企业
  if (res.headers.has('X-Project-Token')) {
    setProjectToken(res.headers.get('X-Project-Token'))
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = {
    ...authHeaders(),
    ...(init?.headers || {}),
  }
  const res = await fetch(BASE + path, { ...init, headers })
  captureAuth(res)
  if (!res.ok) {
    let detail: any = res.statusText
    try {
      const j = await res.json()
      detail = j.detail || j.message || j.errors || j
    } catch {
      try { detail = await res.text() } catch { /* */ }
    }
    const msg = typeof detail === 'string'
      ? detail
      : (detail?.message || (Array.isArray(detail?.errors) ? detail.errors.join('; ') : JSON.stringify(detail)))
    throw new Error(msg)
  }
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) {
    const data: any = await res.json()
    if (Object.prototype.hasOwnProperty.call(data, 'project_token')) {
      setProjectToken(data.project_token)
    } else if (data?.access_token) {
      setProjectToken(data.access_token)
    }
    if (data?.session_id) setSessionId(data.session_id)
    return data
  }
  return (await res.text()) as T
}

export const api = {
  health: () => req<{ ok: boolean; version?: string; build_id?: string; service?: string }>('/api/health'),
  session: () => req<any>('/api/session'),
  cases: () => req<{ cases: any[]; active: string; session_id?: string; project_token?: string }>('/api/cases'),
  activate: (id: string) =>
    req<any>(`/api/cases/${id}/activate`, { method: 'POST' }),
  createEmptyEnterprise: (name?: string) =>
    req<any>('/api/cases/empty', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name || '新企业项目' }),
    }),
  results: () => req<any>('/api/results'),
  case: () => req<any>('/api/case'),
  templates: () => req<any>('/api/templates'),
  updateScenario: (body: Record<string, number | boolean | null | string>) =>
    req<any>('/api/scenario', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  addDevice: (body: Record<string, unknown>) =>
    req<any>('/api/devices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateDevice: (id: string, body: Record<string, unknown>) =>
    req<any>(`/api/devices/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteDevice: (id: string) =>
    req<any>(`/api/devices/${id}`, { method: 'DELETE' }),
  restoreDevice: (id: string) =>
    req<any>(`/api/devices/${id}/restore`, { method: 'POST' }),
  recycleBin: () =>
    req<{ items: any[]; count: number }>('/api/devices/recycle-bin'),
  // 内部兜底（硬删场景）；用户界面走回收站，不再调用
  restoreDemoDevices: () =>
    req<any>('/api/demo/restore-devices', { method: 'POST' }),
  addRetrofit: (body: Record<string, unknown>) =>
    req<any>('/api/retrofits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateRetrofit: (id: string, body: Record<string, unknown>) =>
    req<any>(`/api/retrofits/${encodeURIComponent(id)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  deleteRetrofit: (id: string) =>
    req<any>(`/api/retrofits/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  copyRetrofit: (id: string) =>
    req<any>(`/api/retrofits/${encodeURIComponent(id)}/copy`, { method: 'POST' }),
  revertRetrofit: (id: string) =>
    req<any>(`/api/retrofits/${encodeURIComponent(id)}/revert`, { method: 'POST' }),
  equipmentResolve: (deviceType?: string, name?: string) => {
    const sp = new URLSearchParams()
    if (deviceType) sp.set('device_type', deviceType)
    if (name) sp.set('name', name)
    return req<any>(`/api/equipment/resolve?${sp.toString()}`)
  },
  compareSchemes: (body: Record<string, unknown>) =>
    req<any>('/api/schemes/compare', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  equipmentTypes: () => req<{ types: any[]; display_options: any[] }>('/api/equipment/types'),
  equipmentMethods: (typeCode: string) =>
    req<any>(`/api/equipment/types/${encodeURIComponent(typeCode)}/methods`),
  equipmentCalculate: (body: Record<string, unknown>) =>
    req<any>('/api/equipment/assessments/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  references: (q?: { type_code?: string; method_code?: string }) => {
    const sp = new URLSearchParams()
    if (q?.type_code) sp.set('type_code', q.type_code)
    if (q?.method_code) sp.set('method_code', q.method_code)
    const s = sp.toString()
    return req<any>(`/api/references${s ? '?' + s : ''}`)
  },
  selectReference: (body: Record<string, unknown>) =>
    req<any>('/api/references/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  addCustomParam: (body: Record<string, unknown>) =>
    req<any>('/api/custom-params', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  previewUpload: async (kind: string, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return req<any>(`/api/upload/preview?kind=${kind}`, { method: 'POST', body: fd })
  },
  commitUpload: async (kind: string, file: File, mappings: { user_column: string; standard_field: string }[], opts?: { energyPricePolicy?: 'keep_manual' | 'use_imported' }) => {
    const fd = new FormData()
    fd.append('file', file)
    const q = encodeURIComponent(JSON.stringify(mappings))
    const policy = opts?.energyPricePolicy || 'keep_manual'
    return req<any>(`/api/upload/commit?kind=${kind}&mappings_json=${q}&energy_price_policy=${encodeURIComponent(policy)}`, { method: 'POST', body: fd })
  },
  saveVersion: (label?: string) =>
    req<any>(`/api/versions/save${label ? `?label=${encodeURIComponent(label)}` : ''}`, { method: 'POST' }),
  versions: () => req<{ versions: any[] }>('/api/versions'),
  restoreVersion: (id: string) =>
    req<any>(`/api/versions/${id}/restore`, { method: 'POST' }),
  resetDemo: () => {
    setProjectToken(null)
    return req<any>('/api/reset-demo', { method: 'POST' })
  },
  exportReportUrl: '/api/export/report',
  exportCsvUrl: '/api/export/csv',
  templateUrl: (kind: string) => `/api/templates/${kind}/download`,
  procurementCatalog: (q?: { equipment_category?: string; project_id?: string }) => {
    const sp = new URLSearchParams()
    if (q?.equipment_category) sp.set('equipment_category', q.equipment_category)
    if (q?.project_id) sp.set('project_id', q.project_id)
    const s = sp.toString()
    return req<any>(`/api/procurement/catalog${s ? '?' + s : ''}`)
  },
  applyQuote: (body: Record<string, unknown>) =>
    req<any>('/api/procurement/apply-quote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  addUserQuote: (body: Record<string, unknown>) =>
    req<any>('/api/procurement/user-quotes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  reportPreview: (body: Record<string, unknown>) =>
    req<any>('/api/reports/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  reportExport: async (body: Record<string, unknown>) => {
    const headers = {
      ...authHeaders(),
      'Content-Type': 'application/json',
    }
    const res = await fetch(BASE + '/api/reports/export', {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    })
    captureAuth(res)
    if (!res.ok) {
      let msg = res.statusText
      try {
        const j = await res.json()
        msg = typeof j?.detail === 'string' ? j.detail : (j?.message || JSON.stringify(j?.detail || j))
      } catch {
        try { msg = await res.text() } catch { /* */ }
      }
      throw new Error(msg)
    }
    const text = await res.text()
    const saved = (res.headers.get('X-Archive-Saved') || '').toLowerCase() === 'true'
    const errorCode = res.headers.get('X-Archive-Error-Code') || ''
    return { text, archiveSaved: saved, archiveErrorCode: errorCode }
  },
}
