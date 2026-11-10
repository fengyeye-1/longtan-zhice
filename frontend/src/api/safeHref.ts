/** 前端二次校验：仅允许 http/https 可点击外链。 */
export function safeHref(url?: string | null): string {
  if (!url) return ''
  const text = String(url).trim()
  if (!text) return ''
  try {
    const parsed = new URL(text)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return text
  } catch {
    return ''
  }
  return ''
}
