/**
 * Thin client for the flashcard site's REST API, authenticated with a
 * long-lived bearer token (generated in the site's Settings > Even glasses).
 *
 * Only the endpoints the glasses review loop needs are wrapped here:
 *   GET  /api/languages          - which languages have romanization
 *   GET  /api/cards/due          - the study session (due reviews + new cards)
 *   POST /api/cards/{id}/review  - grade a face; awards XP + syncs streak/quests
 *   GET  /api/streak             - streak + XP totals (for the summary screen)
 */

export type Quality = 'again' | 'hard' | 'good' | 'easy'
export type Face = 'source' | 'target' | 'pronunciation'

export interface DueCard {
  card_id: number
  face: Face
  source_text: string
  target_text: string
  romanization: string | null
  target_lang: string
  notes: string | null
}

export interface StudySession {
  cards: DueCard[]
  review_count: number
  new_count: number
  daily_new_used: number
  daily_new_limit: number
}

export interface LanguageInfo {
  code: string
  name: string
  logographic: boolean // true when the language has romanization (non-Latin)
}

export interface ReviewResult {
  success: boolean
  xp: number
}

export interface Streak {
  streak: number
  points: number
  points_today: number
  daily_goal: number
}

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message)
    this.name = 'ApiError'
  }
}

export class ApiClient {
  private readonly base: string

  constructor(baseUrl: string, private readonly token: string) {
    // Tolerate trailing slashes / missing scheme in a hand-entered URL.
    let b = baseUrl.trim().replace(/\/+$/, '')
    if (b && !/^https?:\/\//i.test(b)) b = 'https://' + b
    this.base = b
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    let res: Response
    try {
      res = await fetch(this.base + path, {
        ...init,
        headers: {
          Authorization: `Bearer ${this.token}`,
          'Content-Type': 'application/json',
          ...(init?.headers || {}),
        },
      })
    } catch (e) {
      throw new ApiError(`Can't reach the site (${(e as Error).message}).`)
    }
    if (res.status === 401) {
      throw new ApiError('Token rejected — regenerate it in Settings.', 401)
    }
    if (!res.ok) {
      throw new ApiError(`Request failed (${res.status}).`, res.status)
    }
    return (await res.json()) as T
  }

  /** Set of language codes that have romanization (non-Latin scripts). */
  async romanizableLangs(): Promise<Set<string>> {
    const data = await this.req<{ languages: LanguageInfo[] }>('/api/languages')
    return new Set(data.languages.filter((l) => l.logographic).map((l) => l.code))
  }

  dueSession(): Promise<StudySession> {
    return this.req<StudySession>('/api/cards/due')
  }

  review(cardId: number, face: Face, quality: Quality): Promise<ReviewResult> {
    return this.req<ReviewResult>(`/api/cards/${cardId}/review`, {
      method: 'POST',
      body: JSON.stringify({ face, quality }),
    })
  }

  streak(): Promise<Streak> {
    return this.req<Streak>('/api/streak')
  }
}
