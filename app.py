import os
import uuid
import time
import threading
import json
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, send_file, jsonify, render_template, Response
from deep_translator import GoogleTranslator
from werkzeug.utils import secure_filename
from pdf2docx import Converter
from docx import Document

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

SUPPORTED_LANGUAGES = GoogleTranslator().get_supported_languages(as_dict=True)

# Store translation job progress
translation_jobs = {}

MAX_CHUNK = 4500
MAX_WORKERS = 4


def translate_texts(texts: list[str], source: str, target: str) -> list[str]:
    """Translate a list of texts, batching where possible but falling back to individual translation."""
    if not texts:
        return []

    translator = GoogleTranslator(source=source, target=target)
    results = [""] * len(texts)

    # Try to translate individually for reliability - batch caused too many issues
    # with separator mangling. Use threading for speed.
    def translate_one(idx, text):
        try:
            result = translator.translate(text)
            return idx, result if result else text
        except Exception:
            return idx, text

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(translate_one, i, text)
            for i, text in enumerate(texts)
        ]
        for future in as_completed(futures):
            idx, translated = future.result()
            results[idx] = translated

    return results


def pdf_to_docx(pdf_path: str, docx_path: str):
    """Convert PDF to DOCX using pdf2docx."""
    cv = Converter(pdf_path)
    cv.convert(docx_path)
    cv.close()


def docx_to_pdf(docx_path: str, pdf_path: str):
    """Convert DOCX to PDF using LibreOffice."""
    output_dir = os.path.dirname(pdf_path)
    result = subprocess.run(
        [
            "libreoffice",
            "--headless",
            "--convert-to", "pdf",
            "--outdir", output_dir,
            docx_path,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr}")

    # LibreOffice outputs with the same basename but .pdf extension
    lo_output = os.path.join(
        output_dir,
        os.path.splitext(os.path.basename(docx_path))[0] + ".pdf"
    )
    if lo_output != pdf_path:
        os.rename(lo_output, pdf_path)


def translate_docx(docx_path: str, source_lang: str, target_lang: str, job_id: str = None):
    """Translate all text in a DOCX file while preserving formatting."""
    doc = Document(docx_path)

    # Collect all translatable text runs from paragraphs and tables
    all_runs = []  # (run_object, original_text)

    # From paragraphs
    for paragraph in doc.paragraphs:
        for run in paragraph.runs:
            text = run.text.strip()
            if text:
                all_runs.append((run, run.text))

    # From tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        text = run.text.strip()
                        if text:
                            all_runs.append((run, run.text))

    # From headers and footers
    for section in doc.sections:
        for header_footer in [section.header, section.footer]:
            if header_footer is not None:
                for paragraph in header_footer.paragraphs:
                    for run in paragraph.runs:
                        text = run.text.strip()
                        if text:
                            all_runs.append((run, run.text))

    total_runs = len(all_runs)
    if total_runs == 0:
        doc.save(docx_path)
        return

    if job_id:
        translation_jobs[job_id]["progress"] = 10
        translation_jobs[job_id]["status"] = "translating"

    # Translate in chunks to show progress
    chunk_size = 50
    start_time = time.time()

    for chunk_start in range(0, total_runs, chunk_size):
        chunk_end = min(chunk_start + chunk_size, total_runs)
        chunk_runs = all_runs[chunk_start:chunk_end]
        chunk_texts = [text for _, text in chunk_runs]

        translated = translate_texts(chunk_texts, source_lang, target_lang)

        for (run, original_text), new_text in zip(chunk_runs, translated):
            if new_text and new_text != original_text:
                # Preserve leading/trailing whitespace from original
                leading = len(original_text) - len(original_text.lstrip())
                trailing = len(original_text) - len(original_text.rstrip())
                prefix = original_text[:leading] if leading else ""
                suffix = original_text[-trailing:] if trailing else ""
                run.text = prefix + new_text.strip() + suffix

        if job_id:
            progress = 10 + int((chunk_end / total_runs) * 70)
            translation_jobs[job_id]["progress"] = progress
            elapsed = time.time() - start_time
            if chunk_end > 0 and elapsed > 0:
                rate = chunk_end / elapsed
                remaining = (total_runs - chunk_end) / rate
                translation_jobs[job_id]["eta_seconds"] = max(0, int(remaining + 15))  # +15s for PDF conversion

    doc.save(docx_path)


def translate_pdf(input_path: str, output_path: str, source_lang: str, target_lang: str, job_id: str = None):
    """Translate a PDF: PDF -> DOCX -> translate -> PDF."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        docx_path = os.path.join(tmp_dir, "document.docx")

        # Step 1: PDF -> DOCX
        if job_id:
            translation_jobs[job_id]["progress"] = 2
            translation_jobs[job_id]["status"] = "converting"

        pdf_to_docx(input_path, docx_path)

        if job_id:
            translation_jobs[job_id]["progress"] = 10

        # Step 2: Translate DOCX content
        translate_docx(docx_path, source_lang, target_lang, job_id)

        if job_id:
            translation_jobs[job_id]["progress"] = 85
            translation_jobs[job_id]["status"] = "generating_pdf"

        # Step 3: DOCX -> PDF
        docx_to_pdf(docx_path, output_path)


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

    job_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
    output_filename = f"translated_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, f"{job_id}_{output_filename}")

    file.save(input_path)

    translation_jobs[job_id] = {
        "progress": 0,
        "eta_seconds": -1,
        "status": "starting",
        "error": None,
        "output_path": output_path,
        "output_filename": output_filename,
    }

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
                "eta_seconds": job.get("eta_seconds", -1),
                "status": job["status"],
            }

            if job["status"] == "error":
                data["error"] = job["error"]
                yield f"data: {jsonify_str(data)}\n\n"
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

    response.call_on_close(cleanup)
    return response


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
