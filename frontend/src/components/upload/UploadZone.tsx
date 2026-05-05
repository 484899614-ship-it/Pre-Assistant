import { useCallback, useState } from 'react'

interface UploadZoneProps {
  onFile: (file: File) => void
  loading: boolean
  uploaded: boolean
}

export function UploadZone({ onFile, loading, uploaded }: UploadZoneProps) {
  const [dragOver, setDragOver] = useState(false)

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file?.type === 'application/pdf') {
      onFile(file)
    }
  }, [onFile])

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      style={{
        padding: '2rem',
        border: dragOver ? '2px dashed #1A365D' : '2px dashed #ccc',
        borderRadius: 8,
        background: dragOver ? '#f0f7ff' : '#fafafa',
        textAlign: 'center',
        transition: 'all 0.2s',
      }}
    >
      <input
        type="file"
        accept=".pdf"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) onFile(f)
        }}
        style={{ marginBottom: '1rem' }}
      />
      <p style={{ color: '#888', fontSize: '0.9rem' }}>
        {uploaded ? 'File uploaded and extracted' : 'Drag & drop a PDF file here, or click to select'}
      </p>
      {loading && <p style={{ color: '#1A365D' }}>Processing...</p>}
    </div>
  )
}
