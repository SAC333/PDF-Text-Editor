from __future__ import annotations
import logging, os, re, uuid
from pathlib import Path
from typing import Any
import pymupdf as fitz, httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("papercraft.pdf")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
app = FastAPI(title="PaperCraft Local PDF Editor API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def get_path(document_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", document_id): raise HTTPException(400, "Invalid document id")
    path = DATA_DIR / f"{document_id}.pdf"
    if not path.exists(): raise HTTPException(404, "Document not found")
    return path

def info(path: Path, document_id: str) -> dict[str, Any]:
    try:
        with fitz.open(path) as pdf:
            return {"id": document_id, "filename": path.name, "page_count": len(pdf), "pages": [{"page_number": i, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2)} for i,p in enumerate(pdf)]}
    except Exception as e: raise HTTPException(422, f"Could not read PDF: {e}") from e

def open_page(path: Path, page_number: int):
    pdf = fitz.open(path)
    if page_number < 0 or page_number >= len(pdf): pdf.close(); raise HTTPException(404, "Page number out of range")
    return pdf, pdf[page_number]

def save_doc(pdf: fitz.Document, path: Path):
    tmp = path.with_suffix('.tmp.pdf')
    try:
        pdf.save(tmp, garbage=4, deflate=True); pdf.close(); os.replace(tmp, path)
    finally: tmp.unlink(missing_ok=True)

BASE14_FONTS = {
    ('sans', False, False): 'helv', ('sans', True, False): 'hebo',
    ('sans', False, True): 'heit', ('sans', True, True): 'hebi',
    ('serif', False, False): 'tiro', ('serif', True, False): 'tibo',
    ('serif', False, True): 'tiit', ('serif', True, True): 'tibi',
    ('mono', False, False): 'cour', ('mono', True, False): 'cobo',
    ('mono', False, True): 'coit', ('mono', True, True): 'cobi',
}

def font_family(name: str) -> str:
    n = (name or '').lower()
    if any(k in n for k in ('courier', 'mono', 'consol', 'menlo')): return 'mono'
    if any(k in n for k in ('times', 'georgia', 'garamond', 'serif', 'book')): return 'serif'
    return 'sans'

def pick_builtin_font(name: str, flags: int) -> str:
    return BASE14_FONTS.get((font_family(name), bool(flags & 16), bool(flags & 2)), 'helv')

def dominant_span(page: fitz.Page, rect: fitz.Rect) -> dict[str, Any] | None:
    try: data = page.get_text('dict', clip=rect)
    except Exception: return None
    best, best_area = None, 0.0
    for b in data.get('blocks', []):
        for line in b.get('lines', []):
            for span in line.get('spans', []):
                sb = fitz.Rect(span.get('bbox', rect))
                area = max(0.0, min(sb.x1, rect.x1) - max(sb.x0, rect.x0)) * max(0.0, min(sb.y1, rect.y1) - max(sb.y0, rect.y0))
                if area > best_area: best_area, best = area, span
    return best

def span_color(span: dict[str, Any] | None) -> tuple[float, float, float]:
    try:
        c = int(span.get('color', 0))
        return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)
    except Exception: return (0.0, 0.0, 0.0)

def _try_insert(page: fitz.Page, box: fitz.Rect, text: str, size: float, fontname: str, color) -> float:
    return page.insert_textbox(box, text, fontsize=round(size, 2), fontname=fontname, color=color, align=fitz.TEXT_ALIGN_LEFT)

def free_right_edge(page: fitz.Page, rect: fitz.Rect) -> float:
    limit = page.rect.x1 - 2
    try: data = page.get_text('dict')
    except Exception: return rect.x1
    for b in data.get('blocks', []):
        if b.get('type') != 0: continue
        for line in b.get('lines', []):
            for span in line.get('spans', []):
                if not span.get('text', '').strip(): continue
                sb = fitz.Rect(span['bbox'])
                same_line = sb.y1 > rect.y0 + 1 and sb.y0 < rect.y1 - 1
                if same_line and sb.x0 >= rect.x1 - 0.5: limit = min(limit, sb.x0 - 2)
    return max(rect.x1, limit)

def line_baseline_y(page: fitz.Page, rect: fitz.Rect) -> float | None:
    try: data = page.get_text('rawdict', clip=rect)
    except Exception: return None
    ys = [ch['origin'][1] for b in data.get('blocks', []) for line in b.get('lines', []) for span in line.get('spans', []) for ch in span.get('chars', [])]
    if not ys: return None
    return sorted(ys)[len(ys) // 2]

def embed_original_font(page: fitz.Page, doc: fitz.Document, span_font_name: str, text: str) -> tuple[str, fitz.Font] | None:
    if not span_font_name: return None
    norm = re.sub(r'[^A-Za-z0-9]', '', span_font_name).lower()
    try:
        for entry in page.get_fonts(full=True):
            xref, basefont, fname = entry[0], entry[3], entry[4]
            candidates = {basefont or '', fname or '', (basefont or '').split('+')[-1]}
            cands_norm = {re.sub(r'[^A-Za-z0-9]', '', c).lower() for c in candidates}
            if not norm or norm not in cands_norm: continue
            try: _, _, _, buffer = doc.extract_font(xref)
            except Exception: continue
            if not buffer: continue
            font = fitz.Font(fontbuffer=buffer)
            if not all(font.has_glyph(ord(ch)) for ch in text): continue
            fontname = re.sub(r'[^A-Za-z0-9]', '', (basefont or fname or '').split('+')[-1])[:62] or f'fitzfont{xref}'
            page.insert_font(fontname=fontname, fontbuffer=buffer)
            return fontname, font
    except Exception:
        logger.warning('embed_original_font fell back for %r', span_font_name, exc_info=True)
    return None

def insert_single_line(page: fitz.Page, rect: fitz.Rect, text: str, start_size: float, fontname: str, color, baseline: float | None, font: fitz.Font | None = None) -> tuple[float, fitz.Rect, list[Any]] | None:
    font = font or fitz.Font(fontname)
    width_per_unit = font.text_length(text, fontsize=1.0)
    available = max(0.0, free_right_edge(page, fitz.Rect(rect)) - rect.x0)
    size = max(5.0, min(72.0, float(start_size)))
    if width_per_unit > 0 and available > 0 and size * width_per_unit > available:
        size = max(5.0, available / width_per_unit)
    if size * width_per_unit > available + 1 and width_per_unit > 0:
        return None
    if baseline is None:
        baseline = rect.y0 + (rect.y1 - rect.y0) * 0.75
    page.insert_text((rect.x0, baseline), text, fontsize=round(size, 2), fontname=fontname, color=color)
    width = width_per_unit * size
    box = fitz.Rect(rect.x0, rect.y0, rect.x0 + (width if width > 4 else 4), max(rect.y1, rect.y0 + size))
    return round(size, 2), box, [(round(size, 2), 'single-line')]

def insert_fitted(page: fitz.Page, rect: fitz.Rect, text: str, start_size: float, fontname: str, color, baseline: float | None = None, font: fitz.Font | None = None) -> tuple[float | None, fitz.Rect | None, list[Any]]:
    min_size, attempts = 5.0, []
    if '\n' not in text and '\r' not in text:
        single = insert_single_line(page, rect, text, start_size, fontname, color, baseline, font)
        if single is not None: return single
    tight = fitz.Rect(rect)
    tight.x1 = max(tight.x1, tight.x0 + 4)
    tight.y1 = max(tight.y1, tight.y0 + min_size)
    wide = fitz.Rect(tight)
    wide.x1 = max(wide.x1, free_right_edge(page, tight))
    boxes = [('wide', wide), ('tight', tight)] if wide.x1 > tight.x1 + 1 else [('tight', tight)]
    size = max(min_size, min(72.0, float(start_size)))
    while size >= min_size - 1e-9:
        for kind, box in boxes:
            rc = _try_insert(page, box, text, size, fontname, color)
            attempts.append((round(size, 2), kind, round(rc, 2)))
            if rc >= 0: return round(size, 2), box, attempts
        size -= 0.5
    grow = fitz.Rect(tight)
    max_x1, max_y1 = page.rect.x1 - 2, page.rect.y1 - 2
    while True:
        grew = False
        if grow.x1 < max_x1: grow.x1 = min(max_x1, grow.x1 + 6); grew = True
        elif grow.y1 < max_y1: grow.y1 = min(max_y1, grow.y1 + 6); grew = True
        if not grew: break
        rc = _try_insert(page, grow, text, min_size, fontname, color)
        attempts.append(('expand', round(rc, 2)))
        if rc >= 0: return min_size, grow, attempts
    return None, None, attempts

def text_signature(text: str) -> str:
    return re.sub(r'\s+', '', text)[:32]

def verify_signature(path: Path, page_number: int, signature: str) -> bool:
    if not signature: return True
    try:
        with fitz.open(path) as doc:
            if page_number >= len(doc): return False
            return signature in re.sub(r'\s+', '', doc[page_number].get_text())
    except Exception: return False

class ReplaceText(BaseModel):
    page_number: int = Field(ge=0)
    rect: list[float] = Field(min_length=4, max_length=4)
    text: str = Field(max_length=5000)
class AddText(BaseModel):
    page_number: int = Field(ge=0); x: float; y: float
    text: str = Field(min_length=1, max_length=5000)
    font_size: float = Field(default=12, ge=4, le=96)
    color: list[float] = Field(default=[0,0,0], min_length=3, max_length=3)
class AddRectangle(BaseModel):
    page_number: int = Field(ge=0); x: float; y: float
    width: float = Field(gt=0); height: float = Field(gt=0)
    color: list[float] = Field(default=[0.1,0.4,0.9], min_length=3, max_length=3); fill: bool = False
class AISuggest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    selected_text: str = Field(default="", max_length=6000)

@app.get('/api/health')
def health(): return {"status":"ok", "service":"papercraft-local-pdf-editor", "ai_configured": bool(os.getenv('AI_API_KEY'))}

@app.post('/api/documents')
async def upload_pdf(file: UploadFile = File(...)):
    if not (file.filename or '').lower().endswith('.pdf'): raise HTTPException(415, 'Please upload a .pdf file')
    content = await file.read()
    if not content: raise HTTPException(400, 'Uploaded file is empty')
    if len(content) > MAX_UPLOAD_MB*1024*1024: raise HTTPException(413, f'File exceeds {MAX_UPLOAD_MB} MB limit')
    if not content.startswith(b'%PDF-'): raise HTTPException(415, 'File does not appear to be a PDF')
    did = uuid.uuid4().hex; path = DATA_DIR/f'{did}.pdf'; path.write_bytes(content)
    try:
        with fitz.open(path) as pdf:
            if not len(pdf): raise ValueError('PDF has no pages')
    except Exception as e:
        path.unlink(missing_ok=True); raise HTTPException(422, f'Invalid or unreadable PDF: {e}') from e
    return info(path, did)

@app.get('/api/documents/{document_id}')
def document_info(document_id: str):
    p=get_path(document_id); return info(p, document_id)

@app.get('/api/documents/{document_id}/pages/{page_number}/preview')
def preview(document_id: str, page_number: int):
    pdf,page=open_page(get_path(document_id),page_number)
    try: return Response(page.get_pixmap(matrix=fitz.Matrix(1.5,1.5), alpha=False).tobytes('png'), media_type='image/png', headers={'Cache-Control':'no-store, max-age=0'})
    finally: pdf.close()

@app.get('/api/documents/{document_id}/pages/{page_number}/blocks')
def blocks(document_id: str, page_number: int):
    pdf,page=open_page(get_path(document_id),page_number)
    try:
        result=[]
        for b in page.get_text('dict')['blocks']:
            if b.get('type') != 0: continue
            for line in b.get('lines',[]):
                spans=[s for s in line.get('spans',[]) if s.get('text','').strip()]
                if not spans: continue
                text=''.join(s.get('text','') for s in spans).strip()
                if not text: continue
                x0=min(float(s['bbox'][0]) for s in spans); y0=min(float(s['bbox'][1]) for s in spans)
                x1=max(float(s['bbox'][2]) for s in spans); y1=max(float(s['bbox'][3]) for s in spans)
                result.append({'id':f'p{page_number}-b{len(result)}','text':text,'rect':[round(v,2) for v in (x0,y0,x1,y1)]})
        return {'page_number':page_number,'blocks':result}
    finally: pdf.close()

@app.post('/api/documents/{document_id}/replace-text')
def replace_text(document_id: str, req: ReplaceText):
    path=get_path(document_id); pdf,page=open_page(path,req.page_number)
    tmp=path.with_suffix('.tmp.pdf')
    try:
        rect=fitz.Rect(*req.rect)
        if rect.is_empty or not rect.is_valid or not page.rect.intersects(rect): raise HTTPException(400,'Invalid text rectangle')
        rect.normalize(); rect=rect & page.rect
        span=dominant_span(page,rect)
        original_text=(span or {}).get('text','') or ''
        start_size=float((span or {}).get('size') or max(6.0,min(24.0,rect.height*.9)))
        baseline=line_baseline_y(page,rect)
        color=span_color(span)
        logger.info("replace-text doc=%s page=%d rect=%s original=%r original_len=%d replacement_len=%d start_size=%.2f color=%s",
                    document_id, req.page_number, [round(v,2) for v in rect], original_text[:80], len(original_text), len(req.text), start_size, color)
        page.add_redact_annot(rect, fill=(1,1,1))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE, text=fitz.PDF_REDACT_TEXT_REMOVE)
        embedded=embed_original_font(page,pdf,(span or {}).get('font',''),req.text)
        if embedded is not None:
            fontname, measure_font = embedded
            font_source=f'embedded:{fontname}'
        else:
            fontname=pick_builtin_font((span or {}).get('font',''),int((span or {}).get('flags',0)))
            measure_font=None
            font_source=fontname
        logger.info("replace-text font doc=%s page=%d used=%s", document_id, req.page_number, font_source)
        replaced=bool(req.text.strip()); placement=None
        if replaced:
            size,box,attempts=insert_fitted(page,rect,req.text,start_size,fontname,color,baseline,measure_font)
            logger.info("replace-text insert doc=%s page=%d attempts=%s chosen_size=%s box=%s", document_id, req.page_number, attempts, size, [round(v,2) for v in box] if box else None)
            if size is None:
                raise HTTPException(422,'Replacement text is too long to fit on the page without discarding content. Shorten it or select a larger region.')
            placement={'font':font_source,'font_size':size,'box':[round(v,2) for v in box]}
        pdf.save(tmp, garbage=4, deflate=True)
        signature=text_signature(req.text) if replaced else ''
        verified=verify_signature(tmp, req.page_number, signature)
        logger.info("replace-text verify doc=%s page=%d replaced=%s verified=%s signature_len=%d", document_id, req.page_number, replaced, verified, len(signature))
        if replaced and not verified:
            raise HTTPException(422,'Replacement text could not be verified in the saved PDF; the original document was left unchanged.')
        pdf.close(); os.replace(tmp, path)
    except HTTPException:
        try: pdf.close()
        except Exception: pass
        tmp.unlink(missing_ok=True); raise
    except Exception as e:
        try: pdf.close()
        except Exception: pass
        tmp.unlink(missing_ok=True)
        logger.exception("replace-text failed doc=%s page=%d", document_id, req.page_number)
        raise HTTPException(422,f'Text replacement failed: {e}') from e
    return {'status':'ok','page_number':req.page_number,'replaced':replaced,'verified':verified,'original_text':original_text[:200],'font':placement['font'] if placement else None,'font_size':placement['font_size'] if placement else None,'message':'Text region replaced and verified.' if replaced else 'Text region cleared.'}

@app.post('/api/documents/{document_id}/add-text')
def add_text(document_id: str, req: AddText):
    path=get_path(document_id); pdf,page=open_page(path,req.page_number)
    try:
        if not (0<=req.x<=page.rect.width and 0<=req.y<=page.rect.height): raise HTTPException(400,'Text position is outside the page')
        color=tuple(max(0,min(1,float(v))) for v in req.color)
        page.insert_text((req.x,req.y),req.text,fontsize=req.font_size,fontname='helv',color=color); save_doc(pdf,path)
    except HTTPException: pdf.close(); raise
    except Exception as e: pdf.close(); raise HTTPException(422,f'Could not add text: {e}') from e
    return {'status':'ok','message':'Text added'}

@app.post('/api/documents/{document_id}/add-rectangle')
def add_rectangle(document_id: str, req: AddRectangle):
    path=get_path(document_id); pdf,page=open_page(path,req.page_number)
    try:
        rect=fitz.Rect(req.x,req.y,req.x+req.width,req.y+req.height)
        if not page.rect.contains(rect): raise HTTPException(400,'Rectangle is outside the page')
        color=tuple(max(0,min(1,float(v))) for v in req.color)
        page.draw_rect(rect,color=color,fill=color if req.fill else None,width=1.5); save_doc(pdf,path)
    except HTTPException: pdf.close(); raise
    except Exception as e: pdf.close(); raise HTTPException(422,f'Could not draw rectangle: {e}') from e
    return {'status':'ok','message':'Rectangle added'}

@app.get('/api/documents/{document_id}/export')
def export_pdf(document_id: str): return FileResponse(get_path(document_id),media_type='application/pdf',filename='edited-document.pdf',headers={'Cache-Control':'no-store, max-age=0'})

@app.post('/api/ai/suggest')
async def ai_suggest(req: AISuggest):
    key=os.getenv('AI_API_KEY','').strip()
    if not key: raise HTTPException(503,'AI helper is not configured. Set AI_API_KEY in backend/.env; PDF editing does not require AI.')
    base=os.getenv('AI_BASE_URL','https://api.x.ai/v1').rstrip('/')
    model=os.getenv('AI_MODEL','grok-2-latest')
    payload={'model':model,'messages':[{'role':'system','content':'You assist with PDF editing. Give concise, practical suggestions. Do not claim you modified the PDF; this endpoint only suggests text.'},{'role':'user','content':f"Instruction: {req.instruction}\nSelected PDF text (if any):\n{req.selected_text}"}],'temperature':0.2}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response=await client.post(f'{base}/chat/completions',headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},json=payload)
        if response.status_code >= 400: raise HTTPException(502,f'AI provider returned HTTP {response.status_code}: {response.text[:500]}')
        data=response.json(); return {'suggestion':data['choices'][0]['message']['content'],'model':model}
    except HTTPException: raise
    except Exception as e: raise HTTPException(502,f'AI request failed: {e}') from e
