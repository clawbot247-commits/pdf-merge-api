#!/usr/bin/env python3
"""
PDF Merge Server — rasterizes each page via poppler pdftoppm then packs into PDF.
This is the only reliable approach for dynamic XFA PDFs (CAR/Lone Wolf forms).
"""
from flask import Flask, request, send_file
from flask_cors import CORS
import subprocess, tempfile, os, io, glob

app = Flask(__name__)
CORS(app)

@app.route('/')
def health():
    return {'status': 'ok', 'service': 'PDF Merge API (pdftoppm + img2pdf + gs compress)'}

@app.route('/merge', methods=['POST'])
def merge():
    files = request.files.getlist('files')
    if not files or len(files) < 2:
        return {'error': 'At least 2 files required'}, 400

    with tempfile.TemporaryDirectory() as tmpdir:
        all_images = []

        for i, f in enumerate(files):
            pdf_path = os.path.join(tmpdir, f'input_{i:03d}.pdf')
            f.save(pdf_path)

            img_prefix = os.path.join(tmpdir, f'pages_{i:03d}')

            # Rasterize PDF pages to grayscale PNG (120 DPI — forms are B&W, gray saves ~3x vs color)
            result = subprocess.run(
                ['pdftoppm', '-r', '120', '-gray', '-png', pdf_path, img_prefix],
                capture_output=True, text=True, timeout=60
            )

            if result.returncode != 0:
                return {
                    'error': f'Failed to rasterize file {f.filename}: {result.stderr}'
                }, 500

            # Collect pages in order
            pages = sorted(glob.glob(f'{img_prefix}-*.png'))
            if not pages:
                return {'error': f'No pages rendered from {f.filename}'}, 500

            all_images.extend(pages)

        if not all_images:
            return {'error': 'No pages to merge'}, 500

        output_path = os.path.join(tmpdir, 'merged.pdf')

        # Pack all images into a single PDF
        raw_path = os.path.join(tmpdir, 'raw.pdf')
        result = subprocess.run(
            ['img2pdf'] + all_images + ['-o', raw_path],
            capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            return {'error': f'img2pdf failed: {result.stderr}'}, 500

        # Compress with Ghostscript (/ebook = ~150 DPI JPEG, readable + compact)
        result = subprocess.run(
            ['gs', '-dBATCH', '-dNOPAUSE', '-dQUIET',
             '-sDEVICE=pdfwrite', '-dPDFSETTINGS=/ebook',
             f'-sOutputFile={output_path}', raw_path],
            capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            # GS failed — fall back to uncompressed
            output_path = raw_path

        with open(output_path, 'rb') as out:
            pdf_bytes = out.read()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name='merged.pdf'
    )

@app.route('/fill-mnr', methods=['POST'])
def fill_mnr_endpoint():
    """
    Accepts JSON body with MNR field data + _radio_select.
    Returns a filled, flattened (but editable) MNR PDF.
    """
    from fill_mnr import fill_mnr

    data = request.get_json()
    if not data:
        return {'error': 'JSON body required'}, 400

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
        output_path = tf.name

    flat_path = output_path + '_flat.pdf'
    try:
        fill_mnr(data, output_path)

        # Ghostscript flatten — removes all interactive form fields so every
        # PDF viewer (browser, WhatsApp, iOS) renders the filled values correctly.
        gs_result = subprocess.run([
            'gs', '-dNOPAUSE', '-dBATCH', '-dQUIET',
            '-sDEVICE=pdfwrite',
            '-dPrinted=false',
            f'-sOutputFile={flat_path}',
            output_path
        ], capture_output=True)

        final_path = flat_path if gs_result.returncode == 0 else output_path

        with open(final_path, 'rb') as f:
            pdf_bytes = f.read()
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name='MNR_filled.pdf'
        )
    except Exception as e:
        return {'error': str(e)}, 500
    finally:
        for p in [output_path, flat_path]:
            if os.path.exists(p):
                os.unlink(p)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
