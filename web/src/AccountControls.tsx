import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'

export type Account = { username: string; display_name: string }
type Session = { account: Account | null; remembered?: Account | null }

function PasswordField({ name, label }: { name: 'password' | 'confirm' | 'current_password'; label: string }) {
  const id = useId()
  const [visible, setVisible] = useState(false)
  const action = visible ? '隐藏' : '显示'
  return <div className="registration-password">
    <label htmlFor={id}>{label}</label>
    <div className="registration-password-row">
      <input id={id} name={name} type={visible ? 'text' : 'password'} autoComplete={name === 'current_password' ? 'current-password' : 'new-password'}
        spellCheck={false} autoCapitalize="none" required />
      <button type="button" className="toolbar-button" aria-label={action + label} aria-controls={id}
        onClick={() => setVisible(value => !value)}>{action}</button>
    </div>
  </div>
}

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
  const [editingPasswordFor, setEditingPasswordFor] = useState<string | null>(null)
  const [changedUsername, setChangedUsername] = useState('')
  const [remembered, setRemembered] = useState<Account | null>(null)
  const [usePassword, setUsePassword] = useState(false)
  const [revision, refresh] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const trigger = useRef<HTMLButtonElement>(null)
  const sessionVersion = useRef(0)
  const changingPassword = !!account && account.username === editingPasswordFor

  useEffect(() => { setEditingPasswordFor(null) }, [account?.username])

  useEffect(() => {
    const request = new AbortController()
    const version = sessionVersion.current
    setLoading(true)
    void accountRequest('session', { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) })
      .then((data) => {
        if (request.signal.aborted || version !== sessionVersion.current) return
        setAccount(data.account)
        setRemembered(data.remembered ?? null)
      })
      .catch(() => { if (!request.signal.aborted && open) setError('无法读取登录状态，请重试') })
      .finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => { request.abort() }
  }, [open, revision, setAccount])

  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === 'workbench-session-change') refresh(value => value + 1)
    }
    const restored = (event: PageTransitionEvent) => {
      if (event.persisted) refresh(value => value + 1)
    }
    const visible = () => {
      if (document.visibilityState === 'visible') refresh(value => value + 1)
    }
    window.addEventListener('storage', changed)
    window.addEventListener('pageshow', restored)
    document.addEventListener('visibilitychange', visible)
    return () => {
      window.removeEventListener('storage', changed)
      window.removeEventListener('pageshow', restored)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [])

  const applySession = (data: Session) => {
    sessionVersion.current += 1
    setAccount(data.account)
    setRemembered(data.remembered ?? null)
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
    setEditingPasswordFor(null)
    setChangedUsername('')
    setUsePassword(false)
    setError('')
    trigger.current?.focus()
  }
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || loading) return
    const fields = new FormData(event.currentTarget)
    if ((registering || changingPassword) && fields.get('confirm') !== fields.get('password')) {
      setError('两次输入的密码不一致')
      return
    }
    setBusy(true)
    setError('')
    try {
      if (changingPassword && account) {
        const data = await accountRequest('change-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: account.username, current_password: fields.get('current_password'), new_password: fields.get('password') }),
        })
        applySession(data)
        setChangedUsername(account.username)
        setEditingPasswordFor(null)
        setUsePassword(true)
        setRegistering(false)
        return
      }
      const data = await accountRequest(registering ? 'register' : 'login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: fields.get('username'), password: fields.get('password'), remember_device: fields.has('remember') }),
      })
      applySession(data)
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '账号操作失败，请重试')
      if (changingPassword) refresh(value => value + 1)
    } finally { setBusy(false) }
  }
  const changeSession = async (endpoint: string) => {
    if (busy || loading) return
    setBusy(true)
    setError('')
    try {
      const data = await accountRequest(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
      applySession(data)
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '账号操作失败，请重试')
      setOpen(true)
      refresh(value => value + 1)
    } finally { setBusy(false) }
  }

  const quickLogin = () => remembered && changeSession(`resume/${encodeURIComponent(remembered.username)}`)
  const quickEntry = !account && remembered
  return <>
    <button ref={trigger} className="toolbar-button account-entry" type="button" disabled={!!quickEntry && (busy || loading)}
      onClick={() => { setError(''); if (quickEntry) void quickLogin(); else setOpen(true) }}
      aria-label={account ? `账号 ${account.username}` : undefined} title={account?.username}>
      {account?.username || (quickEntry ? `登录 ${quickEntry.username}` : '登录')}
    </button>
    {quickEntry && <button className="toolbar-button" type="button" disabled={busy}
      onClick={() => { setUsePassword(true); setError(''); setOpen(true) }}>其他账号</button>}
    {open && createPortal(<div className="lightbox account-overlay" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) close() }}>
      <section className="lightbox-panel account-panel" role="dialog" aria-modal="true" aria-labelledby="account-title"
        onKeyDown={(event) => { if (event.key === 'Escape' && !busy) close() }}>
        <header className="lightbox-bar">
          <h2 id="account-title">{changingPassword ? '修改密码' : account ? '我的账号' : registering ? '注册账号' : '登录工作台'}</h2>
          <button className="lightbox-close" type="button" disabled={busy} autoFocus={!!account} onClick={close} aria-label="关闭账号窗口">×</button>
        </header>
        <div className="account-body">
          {error && <p className="account-error" role="alert">{error}</p>}
          {changedUsername && !account && !registering && <p className="account-notice" role="status">密码已修改，所有设备已退出。请使用新密码重新登录。</p>}
          {changingPassword ? <form key={`change-${editingPasswordFor}`} className="account-form" onSubmit={submit}>
            <label>账号<input name="username" autoComplete="username" value={editingPasswordFor || ''} readOnly /></label>
            <PasswordField name="current_password" label="原密码" />
            <PasswordField name="password" label="新密码" />
            <PasswordField name="confirm" label="确认新密码" />
            <p className="account-notice">修改后，所有设备都需用新密码重新登录。登录时勾选「记住账号」可恢复本机快捷登录。</p>
            <button className="compose-button" type="submit" disabled={busy || loading}>{busy ? '正在修改…' : '确认修改密码'}</button>
            <button className="toolbar-button" type="button" disabled={busy} onClick={() => { setEditingPasswordFor(null); setError('') }}>返回我的账号</button>
          </form> : account ? <div className="account-profile">
            <strong>{account.username}</strong>
            {account.display_name !== account.username && <span>{account.display_name}</span>}
            <button className="toolbar-button" type="button" disabled={busy || loading}
              onClick={() => { setEditingPasswordFor(account.username); setError('') }}>修改密码</button>
            {remembered && <button className="toolbar-button" type="button" disabled={busy || loading}
              onClick={() => void changeSession('logout?forget=false')}>退出并保留快捷登录</button>}
            <button className="toolbar-button" type="button" disabled={busy || loading}
              onClick={() => void changeSession('logout')}>{remembered ? '退出并忘记此账号' : '退出登录'}</button>
          </div> : remembered && !usePassword && !registering ? <div className="account-form">
            <button className="compose-button" type="button" disabled={busy || loading} onClick={quickLogin}>登录 {remembered.username}</button>
            <button className="toolbar-button" type="button" disabled={busy} onClick={() => setUsePassword(true)}>使用其他账号</button>
          </div> : <form key={registering ? 'register' : 'login'} className="account-form" onSubmit={submit}>
            <label>{registering ? '账号（本人英文缩写）' : '账号'}
              <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} defaultValue={registering ? '' : changedUsername}
                placeholder={registering ? '例如 zs（张三）' : undefined} pattern={registering ? '[A-Za-z]+' : undefined}
                title={registering ? '仅限英文字母，大小写视为同一账号' : undefined} maxLength={32} autoFocus required />
            </label>
            {registering ? <>
              <PasswordField name="password" label="密码" />
              <PasswordField name="confirm" label="确认密码" />
            </> : <label>密码<input name="password" type="password" autoComplete="current-password" required /></label>}
            <label className="remember-device"><input type="checkbox" name="remember" defaultChecked />在此浏览器记住账号 30 天</label>
            <button className="compose-button" type="submit" disabled={busy || loading}>{registering ? '注册并登录' : busy ? '正在登录…' : '登录'}</button>
            <button className="toolbar-button" type="button" disabled={busy} onClick={() => { setRegistering(!registering); setError('') }}>{registering ? '已有账号，去登录' : '注册账号'}</button>
            {remembered && !registering && <button className="toolbar-button" type="button" disabled={busy || loading}
              onClick={() => void changeSession('logout')}>忘记 {remembered.username}</button>}
          </form>}
        </div>
      </section>
    </div>, document.body)}
  </>
}
