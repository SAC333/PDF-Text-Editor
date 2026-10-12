# PaperCraft — Local PDF Editor MVP

A runnable local web application: React + TypeScript + Vite frontend, FastAPI backend, PyMuPDF PDF processing. Supports PDF upload, page previews, text-block detection/selection, replacing a text region, adding text, drawing rectangles, and exporting a PDF. Includes optional AI assistance configuration for an OpenAI-compatible API (for example xAI/Grok) but the core editor works without AI.

## Requirements
- Python 3.10+
- Node.js 20+ and npm
- Internet connection for first-time `npm install` / `pip install`

## Start on Windows
Extract the ZIP, then open two terminals in the extracted `pdf_editor_local` folder.

**Terminal 1 — backend**
```powershell
cd backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**Terminal 2 — frontend**
```powershell
cd frontend
npm install
copy .env.example .env
npm run dev
```
Open http://localhost:5173.

## Start on macOS/Linux
Backend:
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```
Frontend in another terminal:
```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

## How to use
1. Click **Open PDF** and choose a PDF.
2. Select **Edit text**, then click a blue outlined text region (or choose it from the right-side list).
3. Edit the replacement field and click **Replace text**. This replaces the region with a white background and inserts new text; font/layout fidelity can vary.
4. Use **Add text** and click the page to place text, or use **Rectangle** and drag on the page to draw a rectangle.
5. Click **Export PDF** to download the edited PDF.

## Optional AI configuration
The editor does not require an AI model for PDF editing. If you want the optional AI helper endpoint, configure `AI_API_KEY`, `AI_BASE_URL`, and `AI_MODEL` in `backend/.env`. For an xAI-compatible API, use the current endpoint/model values from your xAI dashboard/docs. Example shape:
```env
AI_API_KEY=your_key_here
AI_BASE_URL=https://api.x.ai/v1
AI_MODEL=grok-2-latest
```
Do not paste API keys into the frontend, source code, or chat. Keep the key only in `backend/.env`. The AI helper currently offers general document-editing suggestions, not automatic layout-preserving PDF reconstruction. If the key is not configured, the rest of the app still works.

## API
- `GET /api/health`
- `POST /api/documents`
- `GET /api/documents/{id}`
- `GET /api/documents/{id}/pages/{page}/preview`
- `GET /api/documents/{id}/pages/{page}/blocks`
- `POST /api/documents/{id}/replace-text`
- `POST /api/documents/{id}/add-text`
- `POST /api/documents/{id}/add-rectangle`
- `GET /api/documents/{id}/export`
- `POST /api/ai/suggest` (optional AI helper)

## Limitations and security
This is a local development MVP, not a production PDF editor. PDF content is often positioned drawing instructions rather than editable paragraphs, so exact font/layout preservation is not guaranteed. Scanned PDFs need OCR (not included yet). Highlight, image replacement, true eraser, redaction UI, conversion, page rearrangement, authentication, and production hardening are not implemented. Do not expose the development server to the public internet. Files are stored in `backend/data/`; remove them manually when done.
