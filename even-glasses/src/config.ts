/**
 * Configuration persistence via localStorage.
 *
 * The Even Hub SDK doesn't provide a built-in settings system, so we store the
 * site URL, API token, and chosen study scope in the WebView's localStorage.
 * The config + deck-picker screens are shown on the phone before the glasses
 * review loop starts.
 */

const KEY_BASE_URL = 'canto_base_url'
const KEY_TOKEN = 'canto_api_token'
const KEY_DECK_LABELS = 'canto_deck_labels'

/**
 * Default site the plugin talks to — users only need to paste a token; the URL
 * stays editable behind an "Advanced" control.
 *
 * Points at the DEV site for now because the CORS support the (cross-origin)
 * plugin requires is only deployed there — production lacks it until develop is
 * merged to main. Flip this to https://canto-anki.silcoff-labs.ca once CORS
 * ships to prod. (Note the host is `canto-anki`, not `canto-ank`.)
 */
export const DEFAULT_BASE_URL = 'https://dev.canto-anki.silcoff-labs.ca'

export interface Config {
  baseUrl: string
  token: string
}

/**
 * A token is all that's strictly required — the URL falls back to the
 * production default when unset, so pasting a token is enough to be configured.
 */
export function loadConfig(): Config | null {
  const token = localStorage.getItem(KEY_TOKEN)
  if (!token) return null
  const baseUrl = localStorage.getItem(KEY_BASE_URL) || DEFAULT_BASE_URL
  return { baseUrl, token }
}

/** The saved URL, or the production default when the user never set one. */
export function loadBaseUrl(): string {
  return localStorage.getItem(KEY_BASE_URL) || DEFAULT_BASE_URL
}

export function saveConfig(cfg: Config): void {
  localStorage.setItem(KEY_BASE_URL, cfg.baseUrl.trim())
  localStorage.setItem(KEY_TOKEN, cfg.token.trim())
}

export function clearConfig(): void {
  localStorage.removeItem(KEY_BASE_URL)
  localStorage.removeItem(KEY_TOKEN)
}

/**
 * The label ids the review is scoped to. An empty array = "all due cards".
 * Persisted so glasses-only launches reuse the last-picked deck.
 */
export function loadDeckLabels(): number[] {
  try {
    const raw = localStorage.getItem(KEY_DECK_LABELS)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((n) => typeof n === 'number') : []
  } catch {
    return []
  }
}

export function saveDeckLabels(labelIds: number[]): void {
  localStorage.setItem(KEY_DECK_LABELS, JSON.stringify(labelIds))
}
