import { useNavigate } from 'react-router-dom'

export default function HomePage() {
  const navigate = useNavigate()

  return (
    <div style={{
      maxWidth: 800, margin: '0 auto', padding: '4rem 2rem', textAlign: 'center',
    }}>
      <h1>Pre-Assistant</h1>
      <p style={{ color: 'var(--muted)', fontSize: '1.1rem', marginBottom: '2rem' }}>
        AI 驱动的学术论文转演示文稿工具
      </p>
      <p style={{ color: 'var(--muted)', marginBottom: '2rem' }}>
        上传 PDF 论文，多智能体流水线将自动分析内容、设计视觉规范，
        并生成可编辑的 SVG 幻灯片，配合审阅引导的质量保证。
      </p>
      <button
        className="primary-button"
        onClick={() => navigate('/generate')}
        style={{ fontSize: '1.1rem' }}
      >
        开始使用
      </button>
    </div>
  )
}
