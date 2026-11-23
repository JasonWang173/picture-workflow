import { useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'

export type Account = { username: string; display_name: string }
type Session = { account: Account | null }

async function accountRequest(endpoint: string, init?: RequestInit): Promise<Session> {
  const response = await fetch(`/api/auth/${endpoint}`, { signal: AbortSignal.timeout(15_000), ...init })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data || typeof data !== 'object' || !('account' in data)) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : '账号服务暂时无法连接，请重试')
  }
  return data
}

export function AccountControls({ account, setAccount, open, setOpen }: {
  account: Account | null; setAccount: (value: Account | null) => void
  open: boolean; setOpen: (value: boolean) => void
}) {
  const [registering, setRegistering] = useState(false)
  const [revision, refresh] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const request = new AbortController()
    setLoading(true)
    void accountRequest('session', { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) })
      .then((data) => { if (!request.signal.aborted) setAccount(data.account) })
      .catch(() => { if (!request.signal.aborted && open) setError('无法读取登录状态，请重试') })
      .finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => { request.abort() }
  }, [open, revision, setAccount])

  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === 'workbench-session-change') refresh(value => value + 1)
    }
    window.addEventListener('storage', changed)
    return () => window.removeEventListener('storage', changed)
  }, [])

  const applySession = (value: Account | null) => {
    setAccount(value)
    try { localStorage.setItem('workbench-session-change', String(Date.now())) } catch { /* Storage may be disabled. */ }
  }

  useEffect(() => {
    if (!open) return
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = overflow }
  }, [open])

  const close = () => {
    setOpen(false)
    setRegistering(false)
    setError('')
    trigger.current?.focus()
  }
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || loading) return
    const fields = new FormData(event.currentTarget)
    if (registering && fields.get('confirm') !== fields.get('password')) {
      setError('两次输入的密码不一致')
      return
    }
    setBusy(true)
    setError('')
    try {
      const data = await accountRequest(registering ? 'register' : 'login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: fields.get('username'), password: fields.get('password') }),
      })
      applySession(data.account)
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '登录失败，请重试')
    } finally { setBusy(false) }
  }
  const signOut = async () => {
    if (busy || loading) return
    setBusy(true)
    setError('')
    try {
      const data = await accountRequest('logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
      applySession(data.account)
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '退出失败，请重试')
    } finally { setBusy(false) }
  }

  return <>
    <button ref={trigger} className="toolbar-button account-entry" type="button" onClick={() => { setError(''); setOpen(true) }}
      aria-label={account ? `账号 ${account.username}` : '登录'} title={account?.username}>
      {account?.username || '登录'}
    </button>
    {open && createPortal(<div className="lightbox account-overlay" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) close() }}>
      <section className="lightbox-panel account-panel" role="dialog" aria-modal="true" aria-labelledby="account-title"
        onKeyDown={(event) => { if (event.key === 'Escape' && !busy) close() }}>
        <header className="lightbox-bar">
          <h2 id="account-title">{account ? '我的账号' : registering ? '注册账号' : '登录工作台'}</h2>
          <button className="lightbox-close" type="button" disabled={busy} autoFocus={!!account} onClick={close} aria-label="关闭账号窗口">×</button>
        </header>
        <div className="account-body">
          {error && <p className="account-error" role="alert">{error}</p>}
          {account ? <div className="account-profile">
            <strong>{account.username}</strong>
            {account.display_name !== account.username && <span>{account.display_name}</span>}
            <button className="toolbar-button" type="button" disabled={busy || loading} onClick={signOut}>退出登录</button>
          </div> : <form key={registering ? 'register' : 'login'} className="account-form" onSubmit={submit}>
            <label>{registering ? '账号（本人英文缩写）' : '账号'}
              <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
                placeholder={registering ? '例如 zs（张三）' : undefined} pattern={registering ? '[A-Za-z]+' : undefined}
                title={registering ? '仅限英文字母，大小写视为同一账号' : undefined} maxLength={32} autoFocus required />
            </label>
            <label>密码<input name="password" type="password" autoComplete={registering ? 'new-password' : 'current-password'} placeholder={registering ? '15–128 个字符' : undefined} minLength={registering ? 15 : 1} maxLength={128} required /></label>
            {registering && <label>确认密码<input name="confirm" type="password" autoComplete="new-password" maxLength={128} required /></label>}
            <button className="compose-button" type="submit" disabled={busy || loading}>{registering ? '注册并登录' : busy ? '正在登录…' : '登录'}</button>
            <button className="toolbar-button" type="button" disabled={busy} onClick={() => { setRegistering(!registering); setError('') }}>{registering ? '已有账号，去登录' : '注册账号'}</button>
          </form>}
        </div>
      </section>
    </div>, document.body)}
  </>
}
