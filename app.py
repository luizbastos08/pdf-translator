import os
import uuid
import fitz  # PyMuPDF
from flask import Flask, request, send_file, jsonify, render_template
from deep_translator import GoogleTranslator
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
OUTPUT_FOLDER = os.path.join(os.path.dirname(__file__), "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

SUPPORTED_LANGUAGES = GoogleTranslator().get_supported_languages(as_dict=True)


def detect_language(text: str) -> str:
    """Detect language using Google Translate auto-detection."""
    try:
        detected = GoogleTranslator(source="auto", target="en").translate(text[:200])
        # We use 'auto' source in translation, so detection is implicit
        return "auto"
    except Exception:
        return "auto"


def translate_text_chunked(text: str, source: str, target: str) -> str:
    """Translate text, splitting into chunks if needed (Google Translate has a 5000 char limit)."""
    if not text or not text.strip():
        return text

    max_chunk = 4500
    translator = GoogleTranslator(source=source, target=target)

    if len(text) <= max_chunk:
        try:
            result = translator.translate(text)
            return result if result else text
        except Exception:
            return text

    # Split by sentences/newlines for longer texts
    chunks = []
    current_chunk = ""
    sentences = text.replace("\n", "\n\x00").split("\x00")

    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= max_chunk:
            current_chunk += sentence
        else:
            if current_chunk:
                chunks.append(current_chunk)
            # If a single sentence is too long, split by words
            if len(sentence) > max_chunk:
                words = sentence.split(" ")
                current_chunk = ""
                for word in words:
                    if len(current_chunk) + len(word) + 1 <= max_chunk:
                        current_chunk += (" " if current_chunk else "") + word
                    else:
                        if current_chunk:
                            chunks.append(current_chunk)
                        current_chunk = word
            else:
                current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)

    translated_parts = []
    for chunk in chunks:
        try:
            result = translator.translate(chunk)
            translated_parts.append(result if result else chunk)
        except Exception:
            translated_parts.append(chunk)

    return "".join(translated_parts)


def translate_pdf(input_path: str, output_path: str, source_lang: str, target_lang: str):
    """Translate a PDF preserving formatting, images, and layout."""
    doc = fitz.open(input_path)

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]

        for block in blocks:
            if block["type"] != 0:  # Skip non-text blocks (images, etc.)
                continue

            for line in block["lines"]:
                for span in line["spans"]:
                    original_text = span["text"]
                    if not original_text.strip():
                        continue

                    translated_text = translate_text_chunked(
                        original_text, source_lang, target_lang
                    )

                    if translated_text == original_text:
                        continue

                    # Get span properties
                    rect = fitz.Rect(span["bbox"])
                    font_size = span["size"]
                    font_color = span["color"]
                    font_flags = span["flags"]

                    # Determine font name
                    font_name = span["font"]
                    # Map to a base font that supports most characters
                    if "bold" in font_name.lower() and "italic" in font_name.lower():
                        pdf_font = "hebi"  # Helvetica Bold Italic
                    elif "bold" in font_name.lower():
                        pdf_font = "hebo"  # Helvetica Bold
                    elif "italic" in font_name.lower():
                        pdf_font = "heit"  # Helvetica Italic
                    else:
                        pdf_font = "helv"  # Helvetica

                    # Convert integer color to RGB tuple
                    r = ((font_color >> 16) & 0xFF) / 255.0
                    g = ((font_color >> 8) & 0xFF) / 255.0
                    b = (font_color & 0xFF) / 255.0

                    # Redact original text (white out the area)
                    annot = page.add_redact_annot(rect)
                    annot.set_colors(fill=(1, 1, 1))  # White fill
                    page.apply_redactions()

                    # Calculate font size to fit text in the same area
                    text_width = fitz.get_text_length(translated_text, fontname=pdf_font, fontsize=font_size)
                    available_width = rect.width

                    if text_width > 0 and available_width > 0:
                        adjusted_size = min(font_size, font_size * (available_width / text_width))
                        adjusted_size = max(adjusted_size, font_size * 0.5)  # Don't go below 50% of original
                    else:
                        adjusted_size = font_size

                    # Insert translated text
                    text_point = fitz.Point(rect.x0, rect.y1 - (rect.height - adjusted_size) / 2)
                    page.insert_text(
                        text_point,
                        translated_text,
                        fontname=pdf_font,
                        fontsize=adjusted_size,
                        color=(r, g, b),
                    )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/languages", methods=["GET"])
def get_languages():
    return jsonify(SUPPORTED_LANGUAGES)


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
    file_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    input_path = os.path.join(UPLOAD_FOLDER, f"{file_id}_{filename}")
    output_filename = f"translated_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, f"{file_id}_{output_filename}")

    file.save(input_path)

    try:
        translate_pdf(input_path, output_path, source_lang, target_lang)
        return send_file(
            output_path,
            as_attachment=True,
            download_name=output_filename,
            mimetype="application/pdf",
        )
    except Exception as e:
        return jsonify({"error": f"Erro ao traduzir: {str(e)}"}), 500
    finally:
        # Clean up files
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
