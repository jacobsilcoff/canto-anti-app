const KEY_BASE_URL = 'canto_base_url'
const KEY_TOKEN = 'canto_api_token'
const KEY_DECK_LABELS = 'canto_deck_labels'

export const DEFAULT_BASE_URL = 'https://canto-anki.silcoff-labs.ca'

export interface Config {
  baseUrl: string
  token: string
}

export function loadConfig(): Config | null {
  const token = localStorage.getItem(KEY_TOKEN)?.trim()
  if (!token) return null
  return {
    baseUrl: localStorage.getItem(KEY_BASE_URL)?.trim() || DEFAULT_BASE_URL,
    token,
  }
}

export function loadBaseUrl(): string {
  return localStorage.getItem(KEY_BASE_URL)?.trim() || DEFAULT_BASE_URL
}

export function saveConfig(config: Config): void {
  localStorage.setItem(KEY_BASE_URL, config.baseUrl.trim())
  localStorage.setItem(KEY_TOKEN, config.token.trim())
}

export function clearConfig(): void {
  localStorage.removeItem(KEY_TOKEN)
}

export function loadDeckLabels(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(KEY_DECK_LABELS) || '[]')
    return Array.isArray(value)
      ? value.filter((item): item is number => typeof item === 'number' && Number.isFinite(item))
      : []
  } catch {
    return []
  }
}

export function saveDeckLabels(labelIds: number[]): void {
  localStorage.setItem(KEY_DECK_LABELS, JSON.stringify(labelIds))
}
