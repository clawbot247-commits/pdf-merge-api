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
    return {'status': 'ok', 'service': 'PDF Merge API (pdftoppm + img2pdf)'}

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

            # Rasterize PDF pages to PNG images (150 DPI — good quality, reasonable size)
            result = subprocess.run(
                ['pdftoppm', '-r', '150', '-png', pdf_path, img_prefix],
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
        result = subprocess.run(
            ['img2pdf'] + all_images + ['-o', output_path],
            capture_output=True, text=True, timeout=120
        )

        if result.returncode != 0:
            return {'error': f'img2pdf failed: {result.stderr}'}, 500

        with open(output_path, 'rb') as out:
            pdf_bytes = out.read()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name='merged.pdf'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
