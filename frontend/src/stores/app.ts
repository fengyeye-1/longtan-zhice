import { reactive } from 'vue'
import { api } from '../api/client'

export const state = reactive({
  loading: false,
  error: '' as string,
  results: null as any,
  cases: [] as any[],
  activeId: '' as string,
  versions: [] as any[],
})

export async function refreshAll() {
  state.loading = true
  state.error = ''
  try {
    const [results, cases, versions] = await Promise.all([
      api.results(),
      api.cases(),
      api.versions(),
    ])
    state.results = results
    state.cases = cases.cases
    state.activeId = cases.active
    state.versions = versions.versions
  } catch (e: any) {
    state.error = e.message || String(e)
  } finally {
    state.loading = false
  }
}

export function applyPayload(payload: any) {
  if (payload?.results) state.results = payload.results
  if (payload?.case?.case_id) state.activeId = payload.case.case_id
  else if (payload?.case_id) state.activeId = payload.case_id
  else if (payload?.results?.case?.case_id) state.activeId = payload.results.case.case_id
}

export function clearPrivatePageCache() {
  state.results = null
  state.cases = []
  state.activeId = ''
  state.versions = []
  state.error = ''
}
