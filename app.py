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
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

load_dotenv()

from parser import parse_recall_pdf
from phone_lookup import lookup_patient_phones

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=['https://oncue.health', 'https://www.oncue.health', 'http://localhost:5173', 'http://localhost:3000'])

# ── Config ─────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID           = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN            = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_MESSAGING_SERVICE_SID = os.environ.get('TWILIO_MESSAGING_SERVICE_SID')  # MG7463d868c79cdf525c11ae3a1e0a69d1
CLOUD_SQL_URL                = os.environ.get('CLOUD_SQL_URL')   # postgresql://user:pass@host/dbname
MAX_FILE_SIZE_MB             = 25

# ── Database connection ─────────────────────────────────────────────────────
def get_db():
    """Get a Cloud SQL PostgreSQL connection."""
    if not CLOUD_SQL_URL:
        raise Exception('CLOUD_SQL_URL not configured')
    return psycopg2.connect(CLOUD_SQL_URL)

# ── Twilio client ────────────────────────────────────────────────────────────
_twilio_client = None
def get_twilio():
    """Lazily create a single shared Twilio REST client."""
    global _twilio_client
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise Exception('TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not configured')
    if _twilio_client is None:
        _twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _twilio_client


def log_conversation(cur, practice_id, patient_id, direction, body, twilio_sid=None, status=None):
    """
    Insert a row into sms_conversations.
    NOTE: verify these column names match your actual sms_conversations schema —
    adjust the INSERT below if your table differs.
    """
    cur.execute("""
        INSERT INTO sms_conversations (
            practice_id, patient_id, direction, body, twilio_sid, status, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
    """, (practice_id, patient_id, direction, body, twilio_sid, status))

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

# ── Send single SMS ──────────────────────────────────────────────────────────
@app.route('/sms/send', methods=['POST'])
def sms_send():
    """
    POST /sms/send
    Body: JSON { patient_id, message } — 'message' overrides the default template
    Sends a recall SMS to one patient via the Twilio Messaging Service and
    logs the outbound message to sms_conversations.
    """
    data       = request.get_json()
    patient_id = data.get('patient_id')
    message    = data.get('message')

    if not patient_id:
        return jsonify({'error': 'patient_id required'}), 400
    if not TWILIO_MESSAGING_SERVICE_SID:
        return jsonify({'error': 'TWILIO_MESSAGING_SERVICE_SID not configured'}), 500

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
        patient = cur.fetchone()

        if not patient:
            cur.close()
            conn.close()
            return jsonify({'error': 'Patient not found'}), 404

        to_number = patient.get('sms_number') or patient.get('phone')
        if not to_number:
            cur.close()
            conn.close()
            return jsonify({'error': 'Patient has no phone number on file'}), 400

        if not message:
            message = (
                f"Hi {patient.get('name', '').split(' ')[0] or 'there'}, this is your medical "
                f"practice via OnCue Health. You are due for a {patient.get('procedure', 'screening')}. "
                f"Reply YES to confirm interest in scheduling or STOP to opt out."
            )

        client = get_twilio()
        msg = client.messages.create(
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=to_number,
            body=message,
        )

        log_conversation(
            cur,
            practice_id=patient.get('practice_id'),
            patient_id=patient_id,
            direction='outbound',
            body=message,
            twilio_sid=msg.sid,
            status=msg.status,
        )
        cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('sms_sent', patient_id))
        conn.commit()
        cur.close()
        conn.close()

        log.info(f"Sent SMS to patient {patient_id} ({to_number}), Twilio SID {msg.sid}")
        return jsonify({'success': True, 'twilio_sid': msg.sid, 'status': msg.status})

    except Exception as e:
        log.error(f"SMS send failed for patient {patient_id}: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Send bulk SMS ─────────────────────────────────────────────────────────────
@app.route('/sms/send-bulk', methods=['POST'])
def sms_send_bulk():
    """
    POST /sms/send-bulk
    Body: JSON { patient_ids: [...], message } — 'message' overrides the default template
    Sends the same recall message to multiple patients. Returns per-patient results
    so partial failures don't block the whole batch.
    """
    data         = request.get_json()
    patient_ids  = data.get('patient_ids', [])
    message      = data.get('message')

    if not patient_ids:
        return jsonify({'error': 'patient_ids required'}), 400
    if not TWILIO_MESSAGING_SERVICE_SID:
        return jsonify({'error': 'TWILIO_MESSAGING_SERVICE_SID not configured'}), 500

    client  = get_twilio()
    results = []

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        for patient_id in patient_ids:
            cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
            patient = cur.fetchone()

            if not patient:
                results.append({'patient_id': patient_id, 'success': False, 'error': 'not found'})
                continue

            to_number = patient.get('sms_number') or patient.get('phone')
            if not to_number:
                results.append({'patient_id': patient_id, 'success': False, 'error': 'no phone on file'})
                continue

            body = message or (
                f"Hi {patient.get('name', '').split(' ')[0] or 'there'}, this is your medical "
                f"practice via OnCue Health. You are due for a {patient.get('procedure', 'screening')}. "
                f"Reply YES to confirm interest in scheduling or STOP to opt out."
            )

            try:
                msg = client.messages.create(
                    messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
                    to=to_number,
                    body=body,
                )
                log_conversation(
                    cur,
                    practice_id=patient.get('practice_id'),
                    patient_id=patient_id,
                    direction='outbound',
                    body=body,
                    twilio_sid=msg.sid,
                    status=msg.status,
                )
                cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('sms_sent', patient_id))
                results.append({'patient_id': patient_id, 'success': True, 'twilio_sid': msg.sid})
            except Exception as e:
                log.warning(f"Bulk SMS failed for patient {patient_id}: {e}")
                results.append({'patient_id': patient_id, 'success': False, 'error': str(e)})

        conn.commit()
        cur.close()
        conn.close()

        sent = sum(1 for r in results if r['success'])
        log.info(f"Bulk SMS: {sent}/{len(patient_ids)} sent successfully")
        return jsonify({'success': True, 'sent': sent, 'total': len(patient_ids), 'results': results})

    except Exception as e:
        log.error(f"Bulk SMS send failed: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ── Inbound SMS webhook ────────────────────────────────────────────────────────
@app.route('/sms/webhook', methods=['POST'])
def sms_webhook():
    """
    POST /sms/webhook
    Twilio inbound message webhook (application/x-www-form-urlencoded).
    Configure this URL on the Messaging Service's inbound settings in the
    Twilio Console: Messaging > Services > (your service) > Integration.

    Validates the request signature, logs the reply, and updates patient
    status for YES / STOP-type replies. Twilio itself already auto-handles
    STOP/START/HELP at the carrier level per your campaign's opt-out keywords —
    this handler is for your own app-level bookkeeping on top of that.
    """
    # Validate the request actually came from Twilio
    if TWILIO_AUTH_TOKEN:
        validator  = RequestValidator(TWILIO_AUTH_TOKEN)
        signature  = request.headers.get('X-Twilio-Signature', '')
        url        = request.url
        is_valid   = validator.validate(url, request.form, signature)
        if not is_valid:
            log.warning("Invalid Twilio signature on inbound webhook")
            return ('Invalid signature', 403)

    from_number = request.form.get('From', '')
    body        = (request.form.get('Body') or '').strip()
    message_sid = request.form.get('MessageSid', '')

    resp = MessagingResponse()

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute(
            "SELECT * FROM patients WHERE sms_number = %s OR phone = %s ORDER BY recall_date DESC LIMIT 1",
            (from_number, from_number),
        )
        patient = cur.fetchone()

        patient_id  = patient['id'] if patient else None
        practice_id = patient.get('practice_id') if patient else None

        log_conversation(
            cur,
            practice_id=practice_id,
            patient_id=patient_id,
            direction='inbound',
            body=body,
            twilio_sid=message_sid,
            status='received',
        )

        body_upper = body.upper()
        if patient_id:
            if body_upper in ('YES', 'Y', 'CONFIRM'):
                cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('confirmed', patient_id))
            elif body_upper in ('STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT', 'OPTOUT', 'REVOKE'):
                cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('opted_out', patient_id))
            elif body_upper in ('NO', 'N'):
                cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('declined', patient_id))
            else:
                cur.execute("UPDATE patients SET status = %s WHERE id = %s", ('replied', patient_id))

        conn.commit()
        cur.close()
        conn.close()

        log.info(f"Inbound SMS from {from_number}: '{body}' (patient_id={patient_id})")

    except Exception as e:
        log.error(f"Inbound webhook processing failed: {e}", exc_info=True)
        # Still return valid empty TwiML so Twilio doesn't retry endlessly

    return str(resp), 200, {'Content-Type': 'text/xml'}


# ── Get conversation history for a patient ───────────────────────────────────
@app.route('/sms/conversations', methods=['GET'])
def sms_conversations():
    """
    GET /sms/conversations?patient_id=xxx
    Returns SMS conversation history for a patient, oldest first.
    """
    patient_id = request.args.get('patient_id')
    if not patient_id:
        return jsonify({'error': 'patient_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM sms_conversations WHERE patient_id = %s ORDER BY created_at ASC",
            (patient_id,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error(f"Get conversations failed: {e}")
        return jsonify({'error': str(e)}), 500


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


# ── Get profile by Firebase UID ─────────────────────────────────────────────
@app.route('/profile', methods=['GET'])
def get_profile():
    """
    GET /profile?firebase_uid=xxx
    Returns profile + practice name for a given Firebase UID
    """
    firebase_uid = request.args.get('firebase_uid')
    if not firebase_uid:
        return jsonify({'error': 'firebase_uid required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.*, pr.name as practice_name
            FROM profiles p
            LEFT JOIN practices pr ON pr.id = p.practice_id
            WHERE p.firebase_uid = %s
            LIMIT 1
        """, (firebase_uid,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return jsonify({'error': 'No profile found'}), 404

        return jsonify(dict(row))

    except Exception as e:
        log.error(f"Get profile failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Get patients ─────────────────────────────────────────────────────────────
@app.route('/patients', methods=['GET'])
def get_patients():
    """
    GET /patients?practice_id=xxx&provider_id=xxx
    Returns patients for a practice or provider
    """
    practice_id = request.args.get('practice_id')
    provider_id = request.args.get('provider_id')

    if not practice_id and not provider_id:
        return jsonify({'error': 'practice_id or provider_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if practice_id:
            cur.execute("SELECT * FROM patients WHERE practice_id = %s ORDER BY recall_date", (practice_id,))
        else:
            cur.execute("SELECT * FROM patients WHERE provider_id = %s ORDER BY recall_date", (provider_id,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        return jsonify([dict(r) for r in rows])

    except Exception as e:
        log.error(f"Get patients failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Get providers ─────────────────────────────────────────────────────────────
@app.route('/providers', methods=['GET'])
def get_providers():
    """
    GET /providers?practice_id=xxx
    Returns providers for a practice
    """
    practice_id = request.args.get('practice_id')
    if not practice_id:
        return jsonify({'error': 'practice_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT id, name FROM profiles WHERE practice_id = %s AND role = 'provider'", (practice_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error(f"Get providers failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Update patient ─────────────────────────────────────────────────────────────
@app.route('/patients/update', methods=['POST'])
def update_patient():
    """
    POST /patients/update
    Body: JSON { id, ...fields }
    Updates a patient row
    """
    data = request.get_json()
    patient_id = data.get('id')
    if not patient_id:
        return jsonify({'error': 'id required'}), 400

    allowed = ['name','patient_no','dob','phone','phone_type','phone2','phone2_type',
               'sms_number','sms_capable','procedure','recall_date','status',
               'comments','flags','confidence','provider_id']

    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'No valid fields to update'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor()
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [patient_id]
        cur.execute(f"UPDATE patients SET {set_clause} WHERE id = %s", values)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        log.error(f"Update patient failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Add single patient ─────────────────────────────────────────────────────────
@app.route('/patients/add', methods=['POST'])
def add_patient():
    """
    POST /patients/add
    Body: JSON { practice_id, provider_id, name, ... }
    Adds a single patient
    """
    data = request.get_json()
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO patients (
                practice_id, provider_id, name, patient_no, dob,
                phone, phone_type, phone2, phone2_type, sms_number,
                sms_capable, procedure, recall_date, status,
                comments, flags, confidence
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """, (
            data.get('practice_id'),
            data.get('provider_id'),
            data.get('name','').strip(),
            data.get('patient_no') or None,
            data.get('dob') or None,
            data.get('phone',''),
            data.get('phone_type','unknown'),
            data.get('phone2') or None,
            data.get('phone2_type') or None,
            data.get('sms_number') or data.get('phone',''),
            data.get('sms_capable'),
            data.get('procedure','Colonoscopy'),
            data.get('recall_date') or None,
            data.get('status','ready'),
            data.get('comments') or None,
            data.get('flags',[]),
            data.get('confidence','high'),
        ))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify(dict(row))
    except Exception as e:
        log.error(f"Add patient failed: {e}")
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
