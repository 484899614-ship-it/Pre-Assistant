import { useNavigate } from 'react-router-dom'

export default function HomePage() {
  const navigate = useNavigate()

  return (
    <div style={{
      maxWidth: 800, margin: '0 auto', padding: '4rem 2rem', textAlign: 'center',
    }}>
      <h1>Pre-Assistant</h1>
      <p style={{ color: '#666', fontSize: '1.1rem', marginBottom: '2rem' }}>
        AI-powered academic paper to presentation converter
      </p>
      <p style={{ color: '#888', marginBottom: '2rem' }}>
        Upload a PDF paper, and our multi-agent pipeline will analyze the content,
        design a visual specification, and generate editable SVG slides with
        critic-guided quality assurance.
      </p>
      <button
        onClick={() => navigate('/generate')}
        style={{
          padding: '0.8rem 2rem', fontSize: '1.1rem',
          background: '#1A365D', color: 'white', border: 'none',
          borderRadius: 8, cursor: 'pointer',
        }}
      >
        Start
      </button>
    </div>
  )
}
