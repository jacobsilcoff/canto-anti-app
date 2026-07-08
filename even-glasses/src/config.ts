/**
 * Configuration persistence via localStorage.
 *
 * The Even Hub SDK doesn't provide a built-in settings system, so we store the
 * site URL and API token in the WebView's localStorage. The config screen is
 * shown on the phone before the glasses review loop starts.
 */

const KEY_BASE_URL = 'canto_base_url'
const KEY_TOKEN = 'canto_api_token'

export interface Config {
  baseUrl: string
  token: string
}

export function loadConfig(): Config | null {
  const baseUrl = localStorage.getItem(KEY_BASE_URL)
  const token = localStorage.getItem(KEY_TOKEN)
  if (!baseUrl || !token) return null
  return { baseUrl, token }
}

export function saveConfig(cfg: Config): void {
  localStorage.setItem(KEY_BASE_URL, cfg.baseUrl.trim())
  localStorage.setItem(KEY_TOKEN, cfg.token.trim())
}

export function clearConfig(): void {
  localStorage.removeItem(KEY_BASE_URL)
  localStorage.removeItem(KEY_TOKEN)
}
