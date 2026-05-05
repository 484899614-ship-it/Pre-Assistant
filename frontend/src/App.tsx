import { Routes, Route, Navigate, Link, useLocation, useNavigate } from 'react-router-dom'
import { LayoutDashboard, Presentation, Sun, Moon, ChevronLeft, Trash2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { getHistory, deleteSession } from './lib/api'
import type { HistoryItem } from './lib/api'
import HomePage from './pages/HomePage'
import GeneratePage from './pages/GeneratePage'
import ResultPage from './pages/ResultPage'
import PresenterPage from './pages/PresenterPage'

function Header() {
  const location = useLocation()
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    return (localStorage.getItem('theme') as 'dark' | 'light') || 'dark'
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  // On sub-pages, brand link goes to fresh generate page; on home, stays on home
  const brandTarget = location.pathname === '/' ? '/' : '/generate?fresh=1'

  return (
    <header className="app-header">
      <Link to={brandTarget} className="brand-mark">
        <span className="brand-kicker">AI 驱动</span>
        <span className="brand-name">Pre-Assistant</span>
      </Link>
      <button
        className="theme-toggle"
        onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}
        title={theme === 'dark' ? '切换亮色' : '切换暗色'}
      >
        {theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
      </button>
    </header>
  )
}

function Sidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [deleting, setDeleting] = useState<string | null>(null)

  const loadHistory = useCallback(() => {
    getHistory().then(res => setHistory(res.items)).catch(() => {})
  }, [])

  useEffect(() => { loadHistory() }, [loadHistory])

  // Refresh history when navigating
  useEffect(() => { loadHistory() }, [location.pathname, loadHistory])

  const navLinks: { to: string; icon: React.ReactNode; label: string; disabled?: boolean }[] = [
    { to: '/generate?fresh=1', icon: <LayoutDashboard size={18} />, label: '生成工作台' },
    { to: '/presenter', icon: <Presentation size={18} />, label: '演讲者模式' },
  ]

  async function handleDelete(sessionId: string) {
    setDeleting(sessionId)
    try {
      await deleteSession(sessionId)
      setHistory(prev => prev.filter(h => h.session.session_id !== sessionId))
    } catch { /* ignore */ }
    setDeleting(null)
  }

  const statusLabel: Record<string, string> = {
    complete: '已完成',
    error: '失败',
    pending: '等待中',
    cancelled: '已取消',
  }

  return (
    <aside className="sidebar">
      {/* Toggle */}
      <button className="sidebar-toggle" onClick={() => navigate('/')} title="返回首页">
        <ChevronLeft size={18} />
      </button>

      {/* Navigation */}
      <div className="sidebar-section">
          <span className="sidebar-label">导航</span>
          {navLinks.map(link => (
            <Link key={link.to} to={link.disabled ? '#' : link.to} style={{ textDecoration: 'none' }}>
              <div
                className={`sidebar-link ${location.pathname === link.to ? 'sidebar-link-active' : ''}`}
                style={link.disabled ? { opacity: 0.4, cursor: 'not-allowed' } : {}}
                onClick={link.disabled ? (e) => e.preventDefault() : undefined}
              >
                {link.icon}
                <span className="sidebar-link-label">{link.label}</span>
              </div>
            </Link>
          ))}
        </div>

      {/* History */}
      <div className="sidebar-section sidebar-history-section">
          <span className="sidebar-label">最近记录</span>
          <div className="history-list">
            {history.length === 0 && (
              <p style={{ color: 'var(--muted)', fontSize: '0.82rem', padding: '0.5rem' }}>
                暂无记录
              </p>
            )}
            {history.filter(item => item.session.source_type === 'pdf').map(item => {
              const latestJob = item.jobs[0]
              // Determine link target: completed job → result; pending/running job → generate with job;
              // no job but has session → generate fresh; otherwise no link
              const linkTarget = latestJob
                ? (latestJob.status === 'complete'
                    ? `/result/${latestJob.job_id}`
                    : `/generate?fresh=1`)
                : `/generate?fresh=1`
              return (
                <div key={item.session.session_id} className="history-record">
                  <Link
                    to={linkTarget}
                    style={{ textDecoration: 'none' }}
                  >
                    <div className="history-card">
                      <div className="history-card-header">
                        <span className="history-name">{item.session.file_name}</span>
                        <span className="history-status">
                          {latestJob ? (statusLabel[latestJob.status] || latestJob.status) : '未生成'}
                        </span>
                      </div>
                      <div className="history-card-meta">
                        <span>{(item.session.file_size / 1024).toFixed(0)} KB</span>
                        <span>{latestJob?.total_slides ? `${latestJob.slides_completed}/${latestJob.total_slides}页` : ''}</span>
                      </div>
                    </div>
                  </Link>
                  <button
                    className="history-delete-btn"
                    onClick={(e) => { e.preventDefault(); handleDelete(item.session.session_id) }}
                    disabled={deleting === item.session.session_id}
                    title="删除"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              )
            })}
          </div>
        </div>
    </aside>
  )
}

export default function App() {
  const location = useLocation()
  const showSidebar = location.pathname !== '/'

  return (
    <div className="app-shell">
      <Header />
      <div className={showSidebar ? 'app-body-with-sidebar' : 'app-body-no-sidebar'}>
        {showSidebar && <Sidebar />}
        <main className="app-content">
          <div className={showSidebar ? 'app-content-inner-wide' : 'app-content-inner-centered'}>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/generate" element={<GeneratePage />} />
              <Route path="/result/:jobId" element={<ResultPage />} />
              <Route path="/presenter" element={<PresenterPage />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  )
}
