import { useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './App.css'

const EXAMPLES = [
  'How does traffic reach my application?',
  'What is the current CPU and memory usage?',
  'How many pods are running?',
  'Where can I change the replica count?',
]

async function api(path, body) {
  const res = await fetch(`/api/${path}`, body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : undefined)
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }))
  if (!res.ok && !('connected' in data)) throw new Error(data.error || `HTTP ${res.status}`)
  return data
}

function AwsStatus({ status, connecting, onConnect }) {
  if (!status) return <div className="status">Checking AWS connection…</div>
  return (
    <div className={`status ${status.connected ? 'ok' : 'off'}`}>
      <span className="dot" />
      {status.connected
        ? <span title={status.arn}>Connected as <b>{status.arn.split('/').slice(-2).join('/')}</b> · {status.cluster} ({status.region})</span>
        : <span>Not connected to AWS</span>}
      <button onClick={onConnect} disabled={connecting}>
        {connecting ? 'Waiting for sign-in in browser…' : status.connected ? 'Reconnect' : 'Connect to AWS'}
      </button>
    </div>
  )
}

export default function App() {
  const [status, setStatus] = useState(null)
  const [connecting, setConnecting] = useState(false)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bottom = useRef(null)

  useEffect(() => {
    api('aws/status').then(setStatus).catch((e) => setStatus({ connected: false, error: e.message }))
  }, [])
  useEffect(() => bottom.current?.scrollIntoView({ behavior: 'smooth' }), [messages, busy])

  async function connect() {
    setConnecting(true)
    try {
      setStatus(await api('aws/connect', {}))
    } catch (e) {
      setStatus((s) => ({ ...s, connected: false, error: e.message }))
    } finally {
      setConnecting(false)
    }
  }

  async function send(text) {
    const question = text.trim()
    if (!question || busy) return
    const history = [...messages, { role: 'user', content: question }]
    setMessages(history)
    setInput('')
    setBusy(true)
    try {
      const data = await api('chat', { messages: history.filter((m) => !m.error) })
      setMessages([...history, { role: 'assistant', content: data.reply, tools: data.tool_calls }])
    } catch (e) {
      setMessages([...history, { role: 'assistant', content: e.message, error: true }])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header>
        <h1>EKS Assistant <span className="badge">read-only</span></h1>
        <AwsStatus status={status} connecting={connecting} onConnect={connect} />
        {status && !status.connected && status.error && <p className="hint">{status.error}</p>}
      </header>

      <main className="messages">
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask about the infrastructure and its live status.</p>
            <div className="examples">
              {EXAMPLES.map((q) => <button key={q} onClick={() => send(q)} disabled={busy}>{q}</button>)}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role} ${m.error ? 'error' : ''}`}>
            {m.role === 'assistant' ? <Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown> : m.content}
            {m.tools?.length > 0 && (
              <details className="tools">
                <summary>Data sources: {[...new Set(m.tools.map((t) => t.tool))].join(', ')}</summary>
                <ul>
                  {m.tools.map((t, j) => (
                    <li key={j}>
                      <code>{t.tool}({Object.keys(t.args).length ? JSON.stringify(t.args) : ''})</code>
                      {t.error && <span className="tool-error"> — {t.error}</span>}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant pending">Looking at your cluster…</div>}
        <div ref={bottom} />
      </main>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input) }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about your setup, e.g. why is a pod restarting?"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>Send</button>
      </form>
    </div>
  )
}
