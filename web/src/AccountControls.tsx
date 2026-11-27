import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'

export type Account = { username: string; display_name: string }
type Session = { account: Account | null; remembered?: Account | null }

function PasswordField({ name, label, autoComplete = 'new-password', autoFocus = false }: {
  name: 'password' | 'confirm' | 'current_password'; label: string
  autoComplete?: 'current-password' | 'new-password'; autoFocus?: boolean
}) {
  const id = useId()
  const [visible, setVisible] = useState(false)
  const [capsLock, setCapsLock] = useState(false)
  const action = visible ? '隐藏' : '显示'
  return <div className="registration-password">
    <label htmlFor={id}>{label}</label>
    <div className="registration-password-row">
      <input id={id} name={name} type={visible ? 'text' : 'password'} autoComplete={autoComplete}
        placeholder={name === 'confirm' ? '再次输入密码' : '输入' + label} autoFocus={autoFocus}
        aria-describedby={capsLock ? id + '-caps' : undefined}
        onKeyDown={event => setCapsLock(event.getModifierState('CapsLock'))}
        onKeyUp={event => setCapsLock(event.getModifierState('CapsLock'))} onBlur={() => setCapsLock(false)}
        spellCheck={false} autoCapitalize="none" required />
      <button type="button" className="account-password-toggle" aria-label={action + label} aria-controls={id} aria-pressed={visible}
        onClick={() => setVisible(value => !value)}>{action}</button>
    </div>
    {capsLock && <small id={id + '-caps'} className="account-field-hint" role="status">大写锁定已开启</small>}
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
  const [sessionError, setSessionError] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLElement>(null)
  const sessionVersion = useRef(0)
  const changingPassword = !!account && account.username === editingPasswordFor

  useEffect(() => { setEditingPasswordFor(null) }, [account?.username])

  useEffect(() => {
    const request = new AbortController()
    const version = sessionVersion.current
    setLoading(true)
    setSessionError(false)
    void accountRequest('session', { signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]) })
      .then((data) => {
        if (request.signal.aborted || version !== sessionVersion.current) return
        setAccount(data.account)
        setRemembered(data.remembered ?? null)
      })
      .catch(() => { if (!request.signal.aborted) setSessionError(true) })
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
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // Enter the dialog even when it opens in a remembered-account or loading state.
    if (!panel.current?.contains(document.activeElement)) panel.current?.focus()
    return () => {
      document.body.style.overflow = overflow
      if (previousFocus?.isConnected) previousFocus.focus()
      else trigger.current?.focus()
    }
  }, [open])

  useEffect(() => {
    if (!open || loading || !panel.current || panel.current.contains(document.activeElement) && document.activeElement !== panel.current) return
    const firstControl = panel.current.querySelector<HTMLElement>('input:not(:disabled):not([readonly])')
      || panel.current.querySelector<HTMLElement>('button:not(:disabled)')
    firstControl?.focus()
  }, [open, loading, registering, changingPassword, usePassword, account?.username])

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
      event.currentTarget.querySelector<HTMLInputElement>('[name="confirm"]')?.focus()
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
  const heading = changingPassword ? '修改密码' : account ? '我的账号' : registering ? '创建你的账号' : '欢迎回到工作台'
  const description = changingPassword ? '使用原密码验证后，设置新的登录密码。' : account ? '管理登录方式，查看当前账号信息。'
    : registering ? '用自己的英文缩写，为作品留下署名。' : '登录后，查看团队素材与自己的作品集。'
  const locked = busy || loading
  return <>
    <button ref={trigger} className="toolbar-button account-entry" type="button" disabled={!!quickEntry && (busy || loading)}
      onClick={() => { setError(''); if (quickEntry) void quickLogin(); else setOpen(true) }}
      aria-label={account ? `账号 ${account.username}` : undefined} title={account?.username}>
      {account?.username || (quickEntry ? `登录 ${quickEntry.username}` : '登录')}
    </button>
    {quickEntry && <button className="toolbar-button" type="button" disabled={busy}
      onClick={() => { setUsePassword(true); setError(''); setOpen(true) }}>其他账号</button>}
    {open && createPortal(<div className="lightbox account-overlay" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) close() }}>
      <section ref={panel} tabIndex={-1} className="lightbox-panel account-panel" role="dialog" aria-modal="true"
        aria-labelledby="account-title" aria-describedby="account-description"
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) { event.stopPropagation(); close() }
          if (event.key !== 'Tab') return
          const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]'))
            .filter(element => element.getClientRects().length > 0)
          const first = controls[0], last = controls[controls.length - 1]
          if (!first) { event.preventDefault(); return }
          if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
            event.preventDefault(); last.focus()
          } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) {
            event.preventDefault(); first.focus()
          }
        }}>
        <header className="account-header">
          <span className="account-brand">FRAME<span>/</span>FOOD</span>
          <button className="lightbox-close" type="button" disabled={busy} autoFocus={!!account} onClick={close} aria-label="关闭账号窗口">×</button>
        </header>
        <div className="account-body">
          <div className="account-intro">
            <h2 id="account-title">{heading}</h2>
            <p id="account-description">{description}</p>
          </div>
          {!account && <div className="account-tabs" role="group" aria-label="登录或注册">
            <button type="button" aria-pressed={!registering} disabled={busy}
              onClick={() => { setRegistering(false); setError('') }}>登录</button>
            <button type="button" aria-pressed={registering} disabled={busy}
              onClick={() => { setRegistering(true); setError('') }}>注册账号</button>
          </div>}
          {loading && <p className="account-loading" role="status"><span className="account-spinner" aria-hidden="true" />正在确认登录状态…</p>}
          {sessionError && <div className="account-error account-session-error" role="alert">
            <span>暂时无法读取登录状态</span>
            <button type="button" className="account-text-button" disabled={locked} onClick={() => refresh(value => value + 1)}>重新连接</button>
          </div>}
          {error && <p className="account-error" role="alert">{error}</p>}
          {changedUsername && !account && !registering && <p className="account-success" role="status">密码已修改，所有设备已退出。请使用新密码重新登录。</p>}
          {changingPassword ? <form key={`change-${editingPasswordFor}`} onSubmit={submit} aria-busy={busy}>
            <fieldset className="account-form" disabled={locked}>
              <label>账号<input name="username" autoComplete="username" value={editingPasswordFor || ''} readOnly /></label>
              <PasswordField name="current_password" label="原密码" autoComplete="current-password" autoFocus />
              <PasswordField name="password" label="新密码" />
              <PasswordField name="confirm" label="确认新密码" />
              <p className="account-notice">修改后，所有设备都需使用新密码重新登录。再次勾选「记住账号」即可恢复快捷登录。</p>
              <button className="account-primary" type="submit">{busy && <span className="account-spinner" aria-hidden="true" />}{busy ? '正在修改…' : '确认修改密码'}</button>
            </fieldset>
            <button className="account-back" type="button" disabled={busy} onClick={() => { setEditingPasswordFor(null); setError('') }}>← 返回我的账号</button>
          </form> : account ? <div className="account-profile" aria-busy={busy}>
            <div className="account-identity">
              <span className="account-avatar" aria-hidden="true">{account.username.slice(0, 2).toUpperCase()}</span>
              <div><strong>{account.username}</strong><span>{account.display_name !== account.username ? account.display_name : '当前登录账号'}</span></div>
            </div>
            <dl className="account-details">
              <div><dt>素材空间</dt><dd>团队共享</dd></div>
              <div><dt>本机快捷登录</dt><dd>{remembered?.username === account.username ? '已记住账号 · 30 天' : '本次未启用'}</dd></div>
            </dl>
            <button className="account-secondary" type="button" disabled={locked}
              onClick={() => { setEditingPasswordFor(account.username); setError('') }}>修改密码<span aria-hidden="true">→</span></button>
            <div className="account-signout">
              {remembered && <button className="account-secondary" type="button" disabled={locked}
                onClick={() => void changeSession('logout?forget=false')}>退出并保留快捷登录</button>}
              <button className="account-text-button account-danger" type="button" disabled={locked}
                onClick={() => void changeSession('logout')}>{remembered ? '退出并忘记此账号' : '退出登录'}</button>
            </div>
            {busy && <p className="account-loading" role="status">正在更新登录状态…</p>}
          </div> : remembered && !usePassword && !registering ? <div className="account-quick-login" aria-busy={busy}>
            <div className="account-identity">
              <span className="account-avatar" aria-hidden="true">{remembered.username.slice(0, 2).toUpperCase()}</span>
              <div><strong>{remembered.username}</strong><span>此浏览器已记住账号，可以直接登录</span></div>
            </div>
            <button className="account-primary" type="button" disabled={locked} onClick={quickLogin}>
              {busy && <span className="account-spinner" aria-hidden="true" />}{busy ? '正在登录…' : `登录 ${remembered.username}`}
            </button>
            <button className="account-back" type="button" disabled={busy} onClick={() => setUsePassword(true)}>使用其他账号</button>
          </div> : <form key={registering ? 'register' : 'login'} onSubmit={submit} aria-busy={busy}>
            <fieldset className="account-form" disabled={locked}>
              <div className="account-field">
                <label>账号
                  <input name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} defaultValue={registering ? '' : changedUsername}
                    placeholder="输入英文缩写，例如 zs" pattern={registering ? '[A-Za-z]+' : undefined}
                    aria-describedby={registering ? 'account-username-hint' : undefined}
                    title={registering ? '仅限英文字母，大小写视为同一账号' : undefined} maxLength={32} autoFocus required />
                </label>
                {registering && <small className="account-field-hint" id="account-username-hint">使用本人英文缩写，最多 32 个字母，大小写通用。</small>}
              </div>
              <PasswordField name="password" label="密码" autoComplete={registering ? 'new-password' : 'current-password'} />
              {registering && <PasswordField name="confirm" label="确认密码" />}
              <label className="remember-device"><input type="checkbox" name="remember" defaultChecked /><span>在此浏览器记住账号 30 天<small>下次点击账号即可快捷登录</small></span></label>
              <button className="account-primary" type="submit">
                {busy && <span className="account-spinner" aria-hidden="true" />}
                {busy ? registering ? '正在注册…' : '正在登录…' : registering ? '注册并登录' : '登录工作台'}
                {!busy && <span className="account-button-arrow" aria-hidden="true">→</span>}
              </button>
            </fieldset>
            {remembered && !registering && <div className="account-remembered-actions">
              <button className="account-text-button" type="button" disabled={locked} onClick={() => { setUsePassword(false); setError('') }}>快捷登录 {remembered.username}</button>
              <button className="account-text-button" type="button" disabled={locked} onClick={() => void changeSession('logout')}>忘记此账号</button>
            </div>}
          </form>}
          {!changingPassword && <p className="account-footnote">账号用于作品署名，可在合成空间站按账号查找。</p>}
        </div>
      </section>
    </div>, document.body)}
  </>
}
