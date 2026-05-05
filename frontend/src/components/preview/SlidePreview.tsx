import type { PreviewSlide } from '../../lib/types'

interface SlidePreviewProps {
  slides: PreviewSlide[]
  selected: number
  onSelect: (index: number) => void
}

export function SlidePreview({ slides, selected, onSelect }: SlidePreviewProps) {
  if (slides.length === 0) return null

  return (
    <div style={{ width: 220, flexShrink: 0, overflowY: 'auto', maxHeight: '80vh' }}>
      {slides.map((slide, i) => (
        <div
          key={i}
          onClick={() => onSelect(i)}
          style={{
            padding: '0.5rem',
            marginBottom: '0.5rem',
            border: i === selected ? '2px solid #1A365D' : '1px solid #ddd',
            borderRadius: 6,
            cursor: 'pointer',
            background: i === selected ? '#f0f7ff' : 'white',
            fontSize: '0.8rem',
          }}
        >
          <div style={{ fontWeight: 500 }}>Slide {slide.index}</div>
          <div style={{ color: '#666', marginTop: '0.2rem' }}>{slide.name}</div>
        </div>
      ))}
    </div>
  )
}
