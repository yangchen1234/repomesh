// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { escapeHtml, resultMarkup } from './main.ts'

describe('dashboard rendering', () => {
  it('escapes repository content', () => {
    expect(escapeHtml('<script>')).toBe('&lt;script&gt;')
  })

  it('renders file and exact lines', () => {
    const html = resultMarkup({
      file_path: 'src/app.py', symbol_name: 'run', start_line: 10, end_line: 18,
      content: 'def run(): pass', score: 0.42, lexical_score: 0.2,
      vector_score: 0.8, rrf_score: 0.03, retrieval_method: 'hybrid'
    })
    expect(html).toContain('src/app.py:10-18')
    expect(html).toContain('RRF 0.03000')
  })
})
