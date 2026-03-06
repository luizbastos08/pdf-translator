import os
import uuid
import time
import threading
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import fitz  # PyMuPDF
from flask import Flask, request, send_file, jsonify, render_template, Response
from deep_translator import GoogleTranslator
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

SUPPORTED_LANGUAGES = GoogleTranslator().get_supported_languages(as_dict=True)

# Store translation job progress: {job_id: {"progress": 0-100, "status": str, "error": str|None, "output_path": str, "output_filename": str}}
translation_jobs = {}


def detect_language(text: str) -> str:
    """Detect language using Google Translate auto-detection."""
    try:
        detected = GoogleTranslator(source="auto", target="en").translate(text[:200])
        # We use 'auto' source in translation, so detection is implicit
        return "auto"
    except Exception:
        return "auto"


BATCH_SEPARATOR = " \n|~|~|~|\n "
MAX_CHUNK = 4500
MAX_WORKERS = 4


def translate_batch(texts: list[str], source: str, target: str) -> list[str]:
    """Translate multiple texts in as few API calls as possible by batching with a separator."""
    if not texts:
        return []

    translator = GoogleTranslator(source=source, target=target)

    # Group texts into batches that fit within the API char limit
    batches = []  # list of (joined_text, count)
    current_texts = []
    current_len = 0

    for text in texts:
        added_len = len(text) + (len(BATCH_SEPARATOR) if current_texts else 0)
        if current_len + added_len > MAX_CHUNK and current_texts:
            batches.append((BATCH_SEPARATOR.join(current_texts), len(current_texts)))
            current_texts = []
            current_len = 0
        current_texts.append(text)
        current_len += added_len

    if current_texts:
        batches.append((BATCH_SEPARATOR.join(current_texts), len(current_texts)))

    # Translate batches in parallel
    results = [None] * len(batches)

    def translate_one_batch(idx, batch_text, count):
        try:
            result = translator.translate(batch_text)
            if result:
                parts = result.split(BATCH_SEPARATOR.strip())
                # Clean up whitespace from split
                parts = [p.strip() for p in parts]
                if len(parts) == count:
                    return idx, parts
                # Fallback: if separator was mangled, return as single block
                return idx, [result] + [""] * (count - 1)
            return idx, None
        except Exception:
            return idx, None

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(translate_one_batch, i, batch_text, count)
            for i, (batch_text, count) in enumerate(batches)
        ]
        for future in as_completed(futures):
            idx, parts = future.result()
            results[idx] = parts

    # Flatten results back to a list matching input order
    translated = []
    text_idx = 0
    for i, (_, count) in enumerate(batches):
        parts = results[i]
        for j in range(count):
            original = texts[text_idx]
            if parts and j < len(parts) and parts[j]:
                translated.append(parts[j])
            else:
                translated.append(original)
            text_idx += 1

    return translated


def get_pdf_font(font_name: str) -> str:
    """Map font name to a PDF base font."""
    name_lower = font_name.lower()
    if "bold" in name_lower and "italic" in name_lower:
        return "hebi"
    elif "bold" in name_lower:
        return "hebo"
    elif "italic" in name_lower:
        return "heit"
    return "helv"


def translate_pdf(input_path: str, output_path: str, source_lang: str, target_lang: str, job_id: str = None):
    """Translate a PDF preserving formatting, images, and layout."""
    doc = fitz.open(input_path)

    # Phase 1: Extract all translatable spans from all pages
    all_spans = []  # list of (page_num, span_info_dict)
    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        all_spans.append((page_num, span))

    total_spans = len(all_spans)
    if total_spans == 0:
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
        return

    # Phase 2: Batch translate all texts at once
    all_texts = [span["text"] for _, span in all_spans]

    if job_id:
        translation_jobs[job_id]["progress"] = 5
        translation_jobs[job_id]["eta_seconds"] = -1

    start_time = time.time()
    translated_texts = translate_batch(all_texts, source_lang, target_lang)

    if job_id:
        translation_jobs[job_id]["progress"] = 70

    # Phase 3: Apply changes to PDF (redact + insert) page by page
    # Group spans by page for batch redaction
    page_changes = defaultdict(list)

    for i, ((page_num, span), translated_text) in enumerate(zip(all_spans, translated_texts)):
        original_text = span["text"]
        if translated_text and translated_text != original_text:
            page_changes[page_num].append((span, translated_text))

    pages_done = 0
    total_pages_with_changes = len(page_changes)

    for page_num, changes in page_changes.items():
        page = doc[page_num]

        # Batch: add all redact annotations first
        for span, _ in changes:
            rect = fitz.Rect(span["bbox"])
            annot = page.add_redact_annot(rect)
            annot.set_colors(fill=(1, 1, 1))

        # Apply all redactions at once (much faster than per-span)
        page.apply_redactions()

        # Insert all translated texts
        for span, translated_text in changes:
            rect = fitz.Rect(span["bbox"])
            font_size = span["size"]
            font_color = span["color"]
            pdf_font = get_pdf_font(span["font"])

            r = ((font_color >> 16) & 0xFF) / 255.0
            g = ((font_color >> 8) & 0xFF) / 255.0
            b = (font_color & 0xFF) / 255.0

            text_width = fitz.get_text_length(translated_text, fontname=pdf_font, fontsize=font_size)
            available_width = rect.width

            if text_width > 0 and available_width > 0:
                adjusted_size = min(font_size, font_size * (available_width / text_width))
                adjusted_size = max(adjusted_size, font_size * 0.5)
            else:
                adjusted_size = font_size

            text_point = fitz.Point(rect.x0, rect.y1 - (rect.height - adjusted_size) / 2)
            page.insert_text(
                text_point,
                translated_text,
                fontname=pdf_font,
                fontsize=adjusted_size,
                color=(r, g, b),
            )

        pages_done += 1
        if job_id and total_pages_with_changes > 0:
            pdf_progress = int((pages_done / total_pages_with_changes) * 30)
            translation_jobs[job_id]["progress"] = 70 + pdf_progress
            elapsed = time.time() - start_time
            if elapsed > 0:
                total_estimated = elapsed / (0.7 + pdf_progress / 100)
                translation_jobs[job_id]["eta_seconds"] = max(0, int(total_estimated - elapsed))

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/languages", methods=["GET"])
def get_languages():
    return jsonify(SUPPORTED_LANGUAGES)


def run_translation_job(job_id, input_path, output_path, output_filename, source_lang, target_lang):
    """Run translation in a background thread."""
    try:
        translate_pdf(input_path, output_path, source_lang, target_lang, job_id=job_id)
        translation_jobs[job_id]["progress"] = 100
        translation_jobs[job_id]["eta_seconds"] = 0
        translation_jobs[job_id]["status"] = "done"
        translation_jobs[job_id]["output_path"] = output_path
        translation_jobs[job_id]["output_filename"] = output_filename
    except Exception as e:
        translation_jobs[job_id]["status"] = "error"
        translation_jobs[job_id]["error"] = str(e)
    finally:
        # Clean up input file
        if os.path.exists(input_path):
            os.remove(input_path)


@app.route("/api/translate", methods=["POST"])
def translate():
    if "file" not in request.files:
        return jsonify({"error": "Nenhum arquivo enviado"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Nenhum arquivo selecionado"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Apenas arquivos PDF sao aceitos"}), 400

    target_lang = request.form.get("target_lang")
    if not target_lang:
        return jsonify({"error": "Idioma de destino nao especificado"}), 400

    source_lang = request.form.get("source_lang", "auto")
    if not source_lang:
        source_lang = "auto"

    # Save uploaded file
    job_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
    output_filename = f"translated_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_{output_filename}")

    file.save(input_path)

    # Initialize job tracking
    translation_jobs[job_id] = {
        "progress": 0,
        "eta_seconds": -1,
        "status": "translating",
        "error": None,
        "output_path": output_path,
        "output_filename": output_filename,
    }

    # Start translation in background thread
    thread = threading.Thread(
        target=run_translation_job,
        args=(job_id, input_path, output_path, output_filename, source_lang, target_lang),
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def progress(job_id):
    """SSE endpoint for real-time progress updates."""
    def generate():
        while True:
            job = translation_jobs.get(job_id)
            if not job:
                yield f"data: {jsonify_str({'error': 'Job nao encontrado'})}\n\n"
                break

            data = {
                "progress": job["progress"],
                "eta_seconds": job["eta_seconds"],
                "status": job["status"],
            }

            if job["status"] == "error":
                data["error"] = job["error"]
                yield f"data: {jsonify_str(data)}\n\n"
                # Clean up job
                translation_jobs.pop(job_id, None)
                break

            if job["status"] == "done":
                yield f"data: {jsonify_str(data)}\n\n"
                break

            yield f"data: {jsonify_str(data)}\n\n"
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


def jsonify_str(data):
    """Convert dict to JSON string."""
    return json.dumps(data)


@app.route("/api/download/<job_id>")
def download(job_id):
    """Download the translated PDF."""
    job = translation_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job nao encontrado"}), 404

    if job["status"] != "done":
        return jsonify({"error": "Traducao ainda em andamento"}), 400

    output_path = job["output_path"]
    output_filename = job["output_filename"]

    if not os.path.exists(output_path):
        return jsonify({"error": "Arquivo traduzido nao encontrado"}), 404

    # Clean up job after sending file
    def cleanup():
        translation_jobs.pop(job_id, None)
        if os.path.exists(output_path):
            os.remove(output_path)

    response = send_file(
        output_path,
        as_attachment=True,
        download_name=output_filename,
        mimetype="application/pdf",
    )

    # Schedule cleanup after response
    response.call_on_close(cleanup)
    return response


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
