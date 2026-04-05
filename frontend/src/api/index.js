import axios from 'axios'

// ──── Simple Toast Notification System ────
// Provides user-visible error/success messages without requiring a library.
const toast = {
  _container: null,
  _getContainer() {
    if (!this._container) {
      this._container = document.createElement('div')
      this._container.id = 'mirofish-toast-container'
      Object.assign(this._container.style, {
        position: 'fixed',
        top: '20px',
        right: '20px',
        zIndex: '10000',
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        maxWidth: '420px',
      })
      document.body.appendChild(this._container)
    }
    return this._container
  },
  show(message, type = 'error', duration = 5000) {
    const container = this._getContainer()
    const el = document.createElement('div')
    const colors = {
      error:   { bg: '#fef2f2', border: '#fca5a5', text: '#991b1b', icon: '✕' },
      warning: { bg: '#fffbeb', border: '#fcd34d', text: '#92400e', icon: '⚠' },
      success: { bg: '#f0fdf4', border: '#86efac', text: '#166534', icon: '✓' },
      info:    { bg: '#eff6ff', border: '#93c5fd', text: '#1e40af', icon: 'ℹ' },
    }
    const c = colors[type] || colors.error
    Object.assign(el.style, {
      padding: '12px 16px',
      borderRadius: '8px',
      border: `1px solid ${c.border}`,
      backgroundColor: c.bg,
      color: c.text,
      fontSize: '14px',
      fontFamily: 'system-ui, sans-serif',
      boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
      cursor: 'pointer',
      opacity: '0',
      transform: 'translateX(20px)',
      transition: 'all 0.3s ease',
    })
    el.textContent = `${c.icon}  ${message}`
    el.onclick = () => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300) }
    container.appendChild(el)
    requestAnimationFrame(() => { el.style.opacity = '1'; el.style.transform = 'translateX(0)' })
    setTimeout(() => {
      el.style.opacity = '0'
      el.style.transform = 'translateX(20px)'
      setTimeout(() => el.remove(), 300)
    }, duration)
  },
  error(msg)   { this.show(msg, 'error') },
  warning(msg) { this.show(msg, 'warning') },
  success(msg) { this.show(msg, 'success') },
  info(msg)    { this.show(msg, 'info') },
}

export { toast }

// Default HTTP timeout for most API calls (ms)
const DEFAULT_TIMEOUT_MS = 300000

// Ontology upload + LLM analysis can exceed 5+ minutes on local GPUs / LM Studio
export const ONTOLOGY_REQUEST_TIMEOUT_MS =
  Number(import.meta.env.VITE_ONTOLOGY_TIMEOUT_MS) || 35 * 60 * 1000

const service = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://localhost:5001',
  timeout: DEFAULT_TIMEOUT_MS,
  headers: {
    'Content-Type': 'application/json'
  }
})

service.interceptors.request.use(
  config => config,
  error => {
    console.error('Request error:', error)
    return Promise.reject(error)
  }
)

service.interceptors.response.use(
  response => {
    const res = response.data

    if (!res.success && res.success !== undefined) {
      const errorMsg = res.error || res.message || 'Unknown error'
      console.error('API Error:', errorMsg)
      toast.error(errorMsg)
      return Promise.reject(new Error(errorMsg))
    }

    return res
  },
  error => {
    console.error('Response error:', error)

    const status = error.response?.status
    const data = error.response?.data
    const serverMsg =
      data != null
        ? (typeof data === 'string' ? data : data.error || data.message || data.detail)
        : null

    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      toast.error('Request timed out. The server may be busy — please try again.')
    } else if (error.message === 'Network Error') {
      toast.error('Network error — cannot reach the server. Check your connection.')
    } else if (status != null && status >= 500) {
      toast.error(`Server error: ${serverMsg || 'Internal server error'}`)
    } else if (status != null && status >= 400) {
      toast.warning(serverMsg || error.message)
    } else {
      toast.error(serverMsg || error.message || 'An unexpected error occurred')
    }

    const err = new Error(serverMsg || error.message || 'Request failed')
    err.response = error.response
    return Promise.reject(err)
  }
)

const IDEMPOTENT_METHODS = new Set(['get', 'head', 'options'])

export const requestWithRetry = async (requestFn, maxRetries = 3, delay = 1000) => {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await requestFn()
    } catch (error) {
      if (i === maxRetries - 1) throw error

      const method = error.config?.method?.toLowerCase()
      if (method && !IDEMPOTENT_METHODS.has(method)) {
        console.warn(`Not retrying ${method.toUpperCase()} request (non-idempotent)`)
        throw error
      }

      console.warn(`Request failed, retrying (${i + 1}/${maxRetries})...`)
      await new Promise(resolve => setTimeout(resolve, delay * Math.pow(2, i)))
    }
  }
}

export default service
