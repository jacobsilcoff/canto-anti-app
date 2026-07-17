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
}

export interface LanguageInfo {
  code: string
  logographic: boolean
}

export interface Label {
  id: number
  name: string
  card_count: number
}

export interface ReviewResult {
  success: boolean
  xp: number
}

export interface Streak {
  streak: number
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
    let base = baseUrl.trim().replace(/\/+$/, '')
    if (base && !/^https?:\/\//i.test(base)) base = `https://${base}`
    this.base = base
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response
    try {
      response = await fetch(this.base + path, {
        ...init,
        headers: {
          Authorization: `Bearer ${this.token}`,
          'Content-Type': 'application/json',
          ...(init?.headers || {}),
        },
      })
    } catch (error) {
      throw new ApiError(`Can't reach the site (${(error as Error).message}).`)
    }
    if (response.status === 401) {
      throw new ApiError('Token rejected — generate a new one in Settings.', 401)
    }
    if (!response.ok) {
      throw new ApiError(`Request failed (${response.status}).`, response.status)
    }
    return (await response.json()) as T
  }

  async romanizableLangs(): Promise<Set<string>> {
    const data = await this.request<{ languages: LanguageInfo[] }>('/api/languages')
    return new Set(data.languages.filter((lang) => lang.logographic).map((lang) => lang.code))
  }

  async listLabels(): Promise<Label[]> {
    const data = await this.request<{ labels: Label[] }>('/api/labels')
    return data.labels || []
  }

  dueSession(labelIds: number[] = []): Promise<StudySession> {
    const ids = labelIds.filter(Number.isFinite)
    const query = ids.length ? `?label_ids=${ids.join(',')}` : ''
    return this.request<StudySession>(`/api/cards/due${query}`)
  }

  review(cardId: number, face: Face, quality: Quality): Promise<ReviewResult> {
    return this.request<ReviewResult>(`/api/cards/${cardId}/review`, {
      method: 'POST',
      body: JSON.stringify({ face, quality }),
    })
  }

  streak(): Promise<Streak> {
    return this.request<Streak>('/api/streak')
  }
}
