"""
OnCue Health — Parser API
Flask server that handles PDF upload, OCR parsing, and phone lookup
Migrated from Supabase to Google Cloud SQL (PostgreSQL) — July 2026
"""

import os
import json
import tempfile
import logging
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from parser import parse_recall_pdf
from phone_lookup import lookup_patient_phones

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=['https://oncue.health', 'https://www.oncue.health', 'http://localhost:5173', 'http://localhost:3000'])

# ── Config ─────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN  = os.environ.get('TWILIO_AUTH_TOKEN')
CLOUD_SQL_URL      = os.environ.get('CLOUD_SQL_URL')   # postgresql://user:pass@host/dbname
MAX_FILE_SIZE_MB   = 25

# ── Database connection ─────────────────────────────────────────────────────
def get_db():
    """Get a Cloud SQL PostgreSQL connection."""
    if not CLOUD_SQL_URL:
        raise Exception('CLOUD_SQL_URL not configured')
    return psycopg2.connect(CLOUD_SQL_URL)

# ── Health check ────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    db_ok = False
    try:
        conn = get_db()
        conn.close()
        db_ok = True
    except Exception as e:
        log.warning(f"DB health check failed: {e}")

    return jsonify({
        'status': 'ok',
        'twilio': bool(TWILIO_ACCOUNT_SID),
        'database': db_ok,
    })

# ── Parse PDF endpoint ──────────────────────────────────────────────────────
@app.route('/parse', methods=['POST'])
def parse():
    """
    POST /parse
    Body: multipart/form-data with 'file' (PDF or CSV)
           + 'practice_id' (UUID)
           + 'provider_id' (UUID)
           + 'lookup_phones' (bool, default true)
    Returns: JSON with parsed patients and stats
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400

    practice_id  = request.form.get('practice_id')
    provider_id  = request.form.get('provider_id')
    do_lookup    = request.form.get('lookup_phones', 'true').lower() == 'true'

    # Check file size
    file.seek(0, 2)
    size_mb = file.tell() / (1024 * 1024)
    file.seek(0)
    if size_mb > MAX_FILE_SIZE_MB:
        return jsonify({'error': f'File too large ({size_mb:.1f}MB, max {MAX_FILE_SIZE_MB}MB)'}), 400

    # Save to temp file
    suffix = '.pdf' if file.filename.lower().endswith('.pdf') else '.csv'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        log.info(f"Parsing {file.filename} ({size_mb:.1f}MB) for practice {practice_id}")

        if suffix == '.csv':
            result = parse_recall_csv(tmp_path)
        else:
            result = parse_recall_pdf(tmp_path)

        patients  = result['patients']
        stats     = result['stats']
        errors    = result['errors']

        # Phone lookup via Twilio
        if do_lookup and TWILIO_ACCOUNT_SID and patients:
            log.info(f"Running Twilio Lookup on {len(patients)} patients...")
            looked_up = []
            lookup_errors = 0
            for i, patient in enumerate(patients):
                try:
                    enriched = lookup_patient_phones(patient, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                    looked_up.append(enriched)
                except Exception as e:
                    log.warning(f"Lookup failed for patient {i}: {e}")
                    looked_up.append({**patient, 'sms_capable': None, 'sms_number': patient.get('phone'), 'phone_type': 'unknown'})
                    lookup_errors += 1
            patients = looked_up
            stats['sms_capable']      = sum(1 for p in patients if p.get('sms_capable') is True)
            stats['landline_only']    = sum(1 for p in patients if p.get('sms_capable') is False)
            stats['lookup_errors']    = lookup_errors
            stats['two_phones_found'] = sum(1 for p in patients if p.get('phone2'))
            log.info(f"Lookup complete: {stats['sms_capable']} SMS-capable, {stats['landline_only']} landline only")
        else:
            for p in patients:
                p['sms_capable']     = None
                p['sms_number']      = p.get('phone')
                p['sms_number_type'] = 'unknown'
                p['phone_type']      = 'unknown'
                p['phone2_type']     = 'unknown' if p.get('phone2') else None

        for p in patients:
            p['practice_id'] = practice_id
            p['provider_id'] = provider_id

        return jsonify({
            'success':       True,
            'patients':      patients,
            'stats':         stats,
            'practice_name': result.get('practice_name'),
            'report_date':   result.get('report_date'),
            'errors':        errors,
        })

    except Exception as e:
        log.error(f"Parse failed: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500

    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

# ── Save patients endpoint ───────────────────────────────────────────────────
@app.route('/save', methods=['POST'])
def save_patients():
    """
    POST /save
    Body: JSON { patients: [...], practice_id, provider_id }
    Saves parsed patients to Cloud SQL
    """
    data        = request.get_json()
    patients    = data.get('patients', [])
    practice_id = data.get('practice_id')
    provider_id = data.get('provider_id')

    if not patients:
        return jsonify({'error': 'No patients provided'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor()

        insert_sql = """
            INSERT INTO patients (
                practice_id, provider_id, name, patient_no, dob,
                phone, phone_type, phone2, phone2_type, sms_number,
                sms_capable, procedure, recall_date, status,
                comments, flags, confidence
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s
            )
        """

        rows = []
        for p in patients:
            rows.append((
                practice_id or p.get('practice_id'),
                provider_id or p.get('provider_id'),
                p.get('name', '').strip(),
                p.get('patient_no', '').strip() or None,
                p.get('dob') or None,
                p.get('sms_number') or p.get('phone', ''),
                p.get('sms_number_type') or p.get('phone_type', 'unknown'),
                p.get('phone2') or None,
                p.get('phone2_type') or None,
                p.get('sms_number') or p.get('phone', ''),
                p.get('sms_capable'),
                p.get('procedure', 'Colonoscopy'),
                p.get('recall_date') or None,
                p.get('status', 'ready'),
                p.get('comments', '').strip() or None,
                p.get('flags', []),
                p.get('confidence', 'high'),
            ))

        cur.executemany(insert_sql, rows)
        conn.commit()
        saved = cur.rowcount if cur.rowcount > 0 else len(rows)

        cur.close()
        conn.close()

        log.info(f"Saved {saved} patients to Cloud SQL")
        return jsonify({'success': True, 'saved': saved})

    except Exception as e:
        log.error(f"Cloud SQL save failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Create user endpoint ────────────────────────────────────────────────────
@app.route('/create-user', methods=['POST'])
def create_user():
    """
    POST /create-user
    Body: JSON { name, email, password, role, practice_id }
    Creates a Firebase auth user + profile row in Cloud SQL
    """
    data        = request.get_json()
    name        = data.get('name', '').strip()
    email       = data.get('email', '').strip()
    password    = data.get('password', '')
    role        = data.get('role', 'provider')
    practice_id = data.get('practice_id')

    if not name or not email or not password:
        return jsonify({'error': 'name, email, and password are required'}), 400
    if role not in ('provider', 'admin', 'superadmin'):
        return jsonify({'error': 'Invalid role'}), 400

    try:
        # Create Firebase auth user
        import firebase_admin
        from firebase_admin import auth as firebase_auth, credentials

        # Initialize Firebase app if not already done
        if not firebase_admin._apps:
            service_account = json.loads(os.environ.get('FIREBASE_SERVICE_ACCOUNT', '{}'))
            cred = credentials.Certificate(service_account)
            firebase_admin.initialize_app(cred)

        firebase_user = firebase_auth.create_user(
            email=email,
            password=password,
            display_name=name,
        )
        user_id = firebase_user.uid
        log.info(f"Created Firebase user {email} with uid {user_id}")

        # Insert profile into Cloud SQL
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO profiles (id, name, email, role, practice_id, active)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                role = EXCLUDED.role,
                practice_id = EXCLUDED.practice_id,
                active = EXCLUDED.active
        """, (user_id, name, email, role, practice_id, True))
        conn.commit()
        cur.close()
        conn.close()

        log.info(f"Created profile for {email} ({role}) in practice {practice_id}")
        return jsonify({'success': True, 'user_id': user_id, 'email': email})

    except Exception as e:
        log.error(f"Create user failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Delete practice endpoint ────────────────────────────────────────────────
@app.route('/delete-practice', methods=['POST'])
def delete_practice():
    """
    POST /delete-practice
    Body: JSON { practice_id }
    Deletes a practice and its profiles (only if no patients exist)
    """
    data        = request.get_json()
    practice_id = data.get('practice_id')

    if not practice_id:
        return jsonify({'error': 'practice_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor()

        # Safety check — don't delete if patients exist
        cur.execute("SELECT COUNT(*) FROM patients WHERE practice_id = %s", (practice_id,))
        patient_count = cur.fetchone()[0]
        if patient_count > 0:
            cur.close()
            conn.close()
            return jsonify({'error': f'Cannot delete — practice has {patient_count} patients. Remove patients first.'}), 400

        # Delete profiles first (foreign key)
        cur.execute("DELETE FROM profiles WHERE practice_id = %s", (practice_id,))
        # Delete practice
        cur.execute("DELETE FROM practices WHERE id = %s", (practice_id,))
        conn.commit()

        cur.close()
        conn.close()

        log.info(f"Deleted practice {practice_id}")
        return jsonify({'success': True})

    except Exception as e:
        log.error(f"Delete practice failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── CSV parser helper ───────────────────────────────────────────────────────
def parse_recall_csv(csv_path):
    """Parse a CSV recall export"""
    import csv
    patients = []

    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)

        for row in reader:
            row_lower = {k.lower().strip(): v for k, v in row.items()}

            name = (row_lower.get('patient name') or row_lower.get('name') or
                    row_lower.get('patient') or '')
            phone = (row_lower.get('home phone') or row_lower.get('phone') or
                     row_lower.get('cell') or row_lower.get('mobile') or '')
            phone2 = (row_lower.get('work phone') or row_lower.get('phone2') or '')
            patient_no = (row_lower.get('patient no') or row_lower.get('patient #') or
                          row_lower.get('mrn') or row_lower.get('id') or '')
            recall_date = (row_lower.get('recall date') or row_lower.get('date') or '')
            procedure = (row_lower.get('procedure') or row_lower.get('recall type') or 'Colonoscopy')
            dob = (row_lower.get('dob') or row_lower.get('date of birth') or row_lower.get('birth date') or '')

            if not name:
                continue

            from parser import clean_phone, detect_flags
            p1 = clean_phone(phone)
            p2 = clean_phone(phone2)
            phones = []
            if p1: phones.append({'number': p1, 'label': 'home'})
            if p2: phones.append({'number': p2, 'label': 'work'})

            comments = row_lower.get('comments', '') or row_lower.get('notes', '') or ''
            flags = detect_flags(comments + ' ' + procedure)

            patients.append({
                'name':        name.strip(),
                'patient_no':  patient_no.strip(),
                'dob':         dob.strip() or None,
                'phone':       p1,
                'phone2':      p2,
                'phone_label': 'home',
                'phone2_label':'work',
                'phones':      phones,
                'recall_date': recall_date.strip(),
                'procedure':   procedure.strip(),
                'flags':       flags,
                'comments':    comments.strip(),
                'confidence':  'high' if name and p1 else 'low',
            })

    low  = [p for p in patients if p['confidence'] == 'low']
    high = [p for p in patients if p['confidence'] == 'high']

    return {
        'patients':      patients,
        'practice_name': None,
        'report_date':   None,
        'stats': {
            'total':           len(patients),
            'high_confidence': len(high),
            'low_confidence':  len(low),
            'errors':          0,
        },
        'errors': [],
    }


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=False)
