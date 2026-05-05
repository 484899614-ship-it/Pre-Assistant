# Pre-Assistant

AI-powered academic paper to presentation converter with multi-agent pipeline.

## Features

- Upload academic paper PDF and generate editable PowerPoint presentations
- Multi-agent pipeline: research agent, strategist agent, and SVG executor agent
- Support multiple LLM providers (OpenAI, DeepSeek, Doubao, Anthropic)
- Interactive outline confirmation before slide generation
- Static and visual critic for SVG quality assurance
- Real-time progress tracking via WebSocket
- Slide refinement and regeneration support
- High-precision PDF parsing with PaddleOCR (optional)
- Beautiful SVG rendering with resvg (optional)

## Architecture

```
PDF Paper
    |
    v
[Research Agent] -- extracts content, figures, and structure
    |
    v
[Strategist Agent] -- generates slide outline
    |
    v
[SVG Executor Agent] -- generates SVG slides page by page
    |                        |
    |                   [Static Critic] -- layout/style validation (no LLM)
    |                        |
    |                   [Visual Critic] -- visual quality check (optional)
    |
    v
[SVG -> DrawingML Converter] -- native PPTX shapes
    |
    v
output.pptx
```

## Requirements

- Python 3.11+
- Node.js 18+ and npm
- At least one LLM provider API key

## Quick Start

### Backend

```bash
pip install -r requirements.txt
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 --reload --reload-dir backend
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Access

- Frontend: http://localhost:3000
- Backend API: http://127.0.0.1:8001

## Configuration

Create a `.env` file in the project root:

```env
# Default LLM provider (openai / deepseek / doubao / anthropic)
DEFAULT_LLM_PROVIDER=doubao
DEFAULT_LLM_MODEL=doubao-seed-2-0-lite-260215

# API Keys (provide at least one)
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
DOUBAO_API_KEY=
DOUBAO_BASE_URL=
ANTHROPIC_API_KEY=
```

## Optional Dependencies

- **PaddleOCR / PaddlePaddle** -- high-precision formula and layout detection in PDFs
- **resvg-py** -- high-quality SVG rendering for visual critic and fallback export

## Acknowledgments

This project references the following open-source projects in product design and engineering:

- [PPTAgent](https://github.com/icip-cas/PPTAgent)
- [paper-ppt-agent](https://github.com/CRui5in/paper-ppt-agent)
