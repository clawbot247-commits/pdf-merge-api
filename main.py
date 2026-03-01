from flask import Flask, request, send_file
from flask_cors import CORS
from pypdf import PdfWriter, PdfReader
import io

app = Flask(__name__)
CORS(app)

@app.route('/')
def health():
    return {'status': 'ok', 'service': 'PDF Merge API'}

@app.route('/merge', methods=['POST'])
def merge():
    files = request.files.getlist('files')
    if not files or len(files) < 2:
        return {'error': 'At least 2 files required'}, 400

    writer = PdfWriter()
    for f in files:
        try:
            reader = PdfReader(io.BytesIO(f.read()))
            for page in reader.pages:
                writer.add_page(page)
        except Exception as e:
            return {'error': f'Failed to read {f.filename}: {str(e)}'}, 400

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return send_file(out, mimetype='application/pdf',
                     as_attachment=True, download_name='merged.pdf')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
