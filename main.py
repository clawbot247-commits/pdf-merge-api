#!/usr/bin/env python3
"""
PDF Merge Server — uses pypdf to preserve XFA/CAR form streams intact.
Falls back to Ghostscript for non-XFA PDFs that pypdf can't handle.
"""
from flask import Flask, request, send_file
from flask_cors import CORS
from pypdf import PdfWriter, PdfReader
import subprocess, tempfile, os, io

app = Flask(__name__)
CORS(app)

@app.route('/')
def health():
    return {'status': 'ok', 'service': 'PDF Merge API (pypdf + GS fallback)'}

@app.route('/merge', methods=['POST'])
def merge():
    files = request.files.getlist('files')
    if not files or len(files) < 2:
        return {'error': 'At least 2 files required'}, 400

    with tempfile.TemporaryDirectory() as tmpdir:
        input_paths = []
        for i, f in enumerate(files):
            path = os.path.join(tmpdir, f'input_{i:03d}.pdf')
            f.save(path)
            input_paths.append(path)

        output_path = os.path.join(tmpdir, 'merged.pdf')

        # Primary: pypdf — preserves XFA streams intact (works with CAR/Lone Wolf forms)
        try:
            writer = PdfWriter()
            for path in input_paths:
                reader = PdfReader(path)
                for page in reader.pages:
                    writer.add_page(page)
            with open(output_path, 'wb') as out:
                writer.write(out)
        except Exception as pypdf_err:
            # Fallback: Ghostscript (for non-XFA PDFs that pypdf can't handle)
            cmd = [
                'gs',
                '-dBATCH',
                '-dNOPAUSE',
                '-dQUIET',
                '-sDEVICE=pdfwrite',
                '-dCompatibilityLevel=1.7',
                f'-sOutputFile={output_path}',
            ] + input_paths

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                return {
                    'error': f'Merge failed. pypdf: {pypdf_err}. GS: {result.stderr}'
                }, 500

        with open(output_path, 'rb') as f:
            pdf_bytes = f.read()

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name='merged.pdf'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
