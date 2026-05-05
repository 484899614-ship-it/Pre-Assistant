import type { PreviewSlide } from '../../lib/types'

interface SlideViewerProps {
  slide: PreviewSlide | null
}

export function SlideViewer({ slide }: SlideViewerProps) {
  if (!slide) {
    return (
      <div style={{
        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: '#999', background: '#fafafa', borderRadius: 8,
      }}>
        Select a slide to preview
      </div>
    )
  }

  return (
    <div
      dangerouslySetInnerHTML={{ __html: slide.content }}
      style={{
        flex: 1,
        background: 'white',
        border: '1px solid #ddd',
        borderRadius: 8,
        overflow: 'hidden',
      }}
    />
  )
}
