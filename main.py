#!/usr/bin/env python3
"""
PDF Merge Server — rasterizes each page via poppler pdftoppm then packs into PDF.
This is the only reliable approach for dynamic XFA PDFs (CAR/Lone Wolf forms).
"""
from flask import Flask, request, send_file, render_template, jsonify
from flask_cors import CORS
import subprocess, tempfile, os, io, glob, base64, json, requests as req_lib

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

    try:
        fill_mnr(data, output_path)
        with open(output_path, 'rb') as f:
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
        if os.path.exists(output_path):
            os.unlink(output_path)


STRUCTURED_PROMPT = """You are extracting data from a medical acupuncture intake form image.
Return a single JSON object with ONLY these keys (omit any you cannot find):

patientName, dateOfBirth, gender, phoneNumber, address, patientCityStateZip,
subscriberId, medicalRecordNumber, insurance, groupNumber,
primaryCarePhysician, employer,
medications, conditionsUnderCare, treatmentReceived,
underPhysicianCare, pregnant, pregnantWeeks,
currentHealthProblems, whenBegan, howHappened, symptomFrequency,
painLocation, averagePainLevel, worstPainLevel, currentPainLevel, dailyInterferencePainLevel,
reliefDuration, acupunctureProgress,
icd1, condition1, icd2, condition2, icd3, condition3, icd4, condition4,
activity1, measurements1, changes1,
activity2, measurements2, changes2,
activity3, measurements3, changes3,
height, weight, bloodPressure, tobaccoUse,
tongueSign, pulseSignRight, pulseSignLeft, otherComments

EXTRACTION RULES:
- gender: "M" or "F" only
- averagePainLevel, worstPainLevel, currentPainLevel, dailyInterferencePainLevel: number 0-10
- symptomFrequency: checked % range e.g. "81-90%" or "91-100%"
- underPhysicianCare: "Yes" or "No". If Yes append condition e.g. "Yes - Lower back pain"
- pregnant: "No" or "Yes"
- pregnantWeeks: number of weeks if pregnant
- tobaccoUse: "Yes" or "No"
- subscriberId and medicalRecordNumber are the same field (ID#) on this form
- bloodPressure: "systolic/diastolic" e.g. "120/70"
- reliefDuration: e.g. "3 days" or "2 hours"
- acupunctureProgress: "Good", "Fair", or "Poor"

Return ONLY valid JSON. No explanation, no markdown."""

@app.route('/mnr-claw')
def mnr_claw_ui():
    return render_template('mnr_claw.html')

@app.route('/mnr-claw/extract', methods=['POST'])
def mnr_extract():
    """Claude Vision extraction — accepts image or PDF file upload."""
    api_key = request.form.get('api_key', '').strip()
    if not api_key:
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Anthropic API key required'}), 400

    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file uploaded'}), 400

    filename = f.filename.lower()
    content_type = f.content_type or ''

    # If PDF, rasterize first page to JPEG for Claude
    if filename.endswith('.pdf') or 'pdf' in content_type:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            f.save(tmp.name)
            pdf_path = tmp.name
        try:
            img_prefix = pdf_path + '_page'
            result = subprocess.run(
                ['pdftoppm', '-r', '200', '-jpeg', '-l', '1', pdf_path, img_prefix],
                capture_output=True, timeout=30
            )
            page_files = sorted(glob.glob(img_prefix + '*.jpg') + glob.glob(img_prefix + '*.jpeg'))
            if not page_files:
                return jsonify({'error': 'Could not rasterize PDF'}), 500
            with open(page_files[0], 'rb') as img_f:
                img_bytes = img_f.read()
            media_type = 'image/jpeg'
            for pf in page_files:
                os.unlink(pf)
        finally:
            os.unlink(pdf_path)
    else:
        img_bytes = f.read()
        media_type = content_type if content_type.startswith('image/') else 'image/jpeg'

    b64 = base64.b64encode(img_bytes).decode()

    # Call Claude Vision
    try:
        resp = req_lib.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-5',
                'max_tokens': 2000,
                'messages': [{
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': STRUCTURED_PROMPT},
                        {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}}
                    ]
                }]
            },
            timeout=60
        )
    except Exception as e:
        return jsonify({'error': f'Claude API request failed: {e}'}), 500

    if not resp.ok:
        msg = resp.json().get('error', {}).get('message', resp.text)
        return jsonify({'error': f'Claude API error {resp.status_code}: {msg}'}), 500

    raw = resp.json()['content'][0]['text']
    cleaned = raw.strip()
    if cleaned.startswith('```'):
        cleaned = cleaned.split('\n', 1)[1].rsplit('```', 1)[0].strip()

    try:
        data = json.loads(cleaned)
    except Exception:
        return jsonify({'error': 'Claude returned invalid JSON', 'raw': cleaned[:500]}), 500

    return jsonify(data)


@app.route('/mnr-claw/generate', methods=['POST'])
def mnr_generate():
    """Build fill_mnr payload from form fields and return filled PDF."""
    from fill_mnr import fill_mnr

    body = request.get_json()
    if not body:
        return jsonify({'error': 'JSON body required'}), 400

    fields = body.get('fields', {})

    # Build fill_mnr payload
    payload = {}

    def f(key, pdf_key=None):
        v = fields.get(key, '').strip()
        if v:
            payload[pdf_key or key] = v

    f('patientName', 'Patient Name')
    f('dateOfBirth', 'Birthdate')
    f('address', 'Address')
    f('patientCityStateZip', 'CityStateZip')
    f('insurance', 'Health Plan')
    f('groupNumber', 'Group')
    f('employer', 'Employer')
    f('primaryCarePhysician', 'PCP Name')
    f('medications', 'Changes in Pain Medication Use')
    f('currentHealthProblems', 'Chief Complaint 1')
    f('whenBegan', 'Date of onset 1')
    f('howHappened', 'Cause of Condition 1')
    f('painLocation', 'Pain Location')
    f('reliefDuration', 'How long does relief last 1')
    f('tongueSign', 'Tongue Signs')
    f('pulseSignRight', 'Pulse Signs Rt')
    f('pulseSignLeft', 'Pulse Signs Lt')
    f('height', 'Height')
    f('weight', 'Weight')
    f('bloodPressure', 'Blood Pressure')
    f('acupunctureProgress', 'Response to most recent Treatment Plan')
    f('otherComments', 'Other Comments eg Responses to Care Barriers to Progress Patient Health History 1')
    f('icd1', 'ICD CODE 1'); f('condition1', 'Condition 1')
    f('icd2', 'ICD CODE 2'); f('condition2', 'Condition 2')
    f('icd3', 'ICD CODE 3'); f('condition3', 'Condition 3')
    f('icd4', 'ICD CODE 4'); f('condition4', 'Condition 4')
    f('activity1', 'Activity 0'); f('measurements1', 'Measurements 0'); f('changes1', 'How has it changed 0')
    f('activity2', 'Activity 1'); f('measurements2', 'Measurements 1'); f('changes2', 'How has it changed 1')
    f('activity3', 'Activity 2'); f('measurements3', 'Measurements 2'); f('changes3', 'How has it changed 2')

    # Pain levels
    for k, pk in [('averagePainLevel','Average pain level'),('worstPainLevel','Worst pain level'),
                  ('currentPainLevel','Current pain level'),('dailyInterferencePainLevel','Daily pain level')]:
        v = fields.get(k, '').strip()
        if v:
            payload[pk] = str(v)

    # Subscriber/Patient ID — always sync both
    sub_id = fields.get('subscriberId', '').strip()
    if sub_id:
        payload['Patient ID'] = sub_id
        payload['Subscriber ID'] = sub_id

    # Phone
    phone = fields.get('phoneNumber', '').replace('-','').replace('(','').replace(')','').replace(' ','')
    if len(phone) >= 10:
        payload['Patient Area code'] = phone[:3]
        payload['Patient Phone number'] = phone[3:6] + '-' + phone[6:10]

    # Symptom frequency checkboxes
    freq = fields.get('symptomFrequency', '').strip()
    if freq:
        payload['Symptom Frequency'] = freq  # fill_mnr handles checkbox matching

    # Radio buttons
    radio_select = []

    gender = fields.get('gender', '').strip()
    if gender in ('M', 'F'):
        radio_select.append({'field': 'Gender', 'on_state': gender})

    physician = fields.get('underPhysicianCare', 'No').strip()
    physCond = fields.get('physicianCondition', '').strip()
    if physician.lower().startswith('yes'):
        radio_select.append({'field': 'Physician Care', 'on_state': 'Yes'})
        if physCond:
            payload['Yes Being Cared for By a Medical Physician'] = physCond
    else:
        radio_select.append({'field': 'Physician Care', 'on_state': 'No'})

    pregnant = fields.get('pregnant', 'No').strip()
    if pregnant.lower().startswith('yes'):
        radio_select.append({'field': 'Required Is this patient pregnant', 'on_state': 'Yes'})
        weeks = fields.get('pregnantWeeks', '').strip()
        if weeks:
            payload['Pregnant weeks'] = weeks
    else:
        radio_select.append({'field': 'Required Is this patient pregnant', 'on_state': 'No'})

    tobacco = fields.get('tobaccoUse', 'No').strip()
    radio_select.append({'field': 'Tobacco Use', 'on_state': 'Yes_5' if tobacco.lower().startswith('yes') else 'No_6'})

    payload['_radio_select'] = radio_select

    # Generate PDF
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
        output_path = tf.name
    try:
        fill_mnr(payload, output_path)
        with open(output_path, 'rb') as pf:
            pdf_bytes = pf.read()
        return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf',
                         as_attachment=True, download_name='MNR_filled.pdf')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(output_path):
            os.unlink(output_path)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
