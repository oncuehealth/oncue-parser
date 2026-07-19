"""
OnCue Health — Parser API
Flask server that handles PDF upload, OCR parsing, and phone lookup
Migrated from Supabase to Google Cloud SQL (PostgreSQL) — July 2026
"""

import os
import re
import json
import requests
from functools import wraps
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_auth_requests
import tempfile
import logging
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator

load_dotenv()

from parser import parse_recall_pdf
from phone_lookup import lookup_patient_phones

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
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
    """Insert a row into sms_conversations."""
    cur.execute("""
        INSERT INTO sms_conversations (
            practice_id, patient_id, direction, body, twilio_sid, status, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
    """, (practice_id, patient_id, direction, body, twilio_sid, status))


# ── Message template system ─────────────────────────────────────────────────
# Default opening message. Practices can override this via /templates.
# Placeholders: {patient_name} {doctor_name} {practice_name} {procedure}
DEFAULT_TEMPLATE = (
    "Hi {patient_name}, this is Dr. {doctor_name} from {practice_name}. We see that you are "
    "due for your {procedure}. We would love to help you get it scheduled in an expedited and "
    "efficient manner. Are you able to answer some questions so that we can get it scheduled? "
    "If you would like a call instead of using our automated HIPAA-compliant text platform, "
    "please text CALL."
)

def get_template_for_practice(cur, practice_id):
    """Look up a practice's custom template, falling back to the default."""
    if practice_id:
        cur.execute("SELECT template_text FROM message_templates WHERE practice_id = %s", (practice_id,))
        row = cur.fetchone()
        if row:
            return row['template_text']
    return DEFAULT_TEMPLATE


# ── Auth: verify Firebase ID tokens and enforce role-based scoping ──────────
FIREBASE_PROJECT_ID = os.environ.get('FIREBASE_PROJECT_ID', 'oncue-health-26c99')
_google_auth_request = google_auth_requests.Request()

def verify_firebase_token(token):
    """
    Verifies a Firebase ID token against Google's public certs (no service
    account needed) and returns the decoded claims, including the uid.
    Raises an exception if the token is missing, expired, or invalid.
    """
    decoded = google_id_token.verify_firebase_token(token, _google_auth_request, audience=FIREBASE_PROJECT_ID)
    return decoded


def get_bearer_token():
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return None
    return auth_header.split(' ', 1)[1]


def require_auth(f):
    """
    Verifies the caller's Firebase ID token AND that they have an active
    profile row in Cloud SQL. Attaches the profile to request.oncue_user
    for the route to use for scoping (role, practice_id, id, etc).
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = get_bearer_token()
        if not token:
            return jsonify({'error': 'Missing Authorization header'}), 401
        try:
            decoded = verify_firebase_token(token)
        except Exception as e:
            log.warning(f"Token verification failed: {e}")
            return jsonify({'error': 'Invalid or expired token'}), 401

        firebase_uid = decoded.get('uid') or decoded.get('user_id') or decoded.get('sub')
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT id, name, email, role, practice_id, active FROM profiles WHERE firebase_uid = %s", (firebase_uid,))
            profile = cur.fetchone()
            cur.close()
            conn.close()
        except Exception as e:
            log.error(f"Profile lookup failed during auth: {e}")
            return jsonify({'error': 'Auth check failed'}), 500

        if not profile or not profile.get('active'):
            return jsonify({'error': 'No active profile found for this account'}), 403

        request.oncue_user = profile
        return f(*args, **kwargs)
    return wrapper


def require_role(*roles):
    """Stack after @require_auth to additionally restrict by role."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if request.oncue_user['role'] not in roles:
                return jsonify({'error': 'Not authorized for this action'}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def patient_in_scope(cur, patient_id, user):
    """
    Checks whether the given patient belongs to the caller's scope:
    superadmin -> any patient, admin -> same practice, provider -> assigned to them.
    Returns the patient row (dict) if in scope, else None.
    """
    cur.execute("SELECT * FROM patients WHERE id = %s", (patient_id,))
    patient = cur.fetchone()
    if not patient:
        return None
    if user['role'] == 'superadmin':
        return patient
    if user['role'] == 'admin' and patient.get('practice_id') == user['practice_id']:
        return patient
    if user['role'] == 'provider' and patient.get('provider_id') == user['id']:
        return patient
    return None


def render_template(template_text, patient_name=None, doctor_name=None, practice_name=None, procedure=None):
    # Provider names are often stored as "Dr. Jane Smith" already. Since templates
    # typically write "this is Dr. {doctor_name}", strip any leading "Dr."/"Dr" so
    # we don't end up with "Dr. Dr. Jane Smith".
    clean_doctor_name = doctor_name
    if clean_doctor_name:
        clean_doctor_name = re.sub(r'^\s*dr\.?\s+', '', clean_doctor_name, flags=re.IGNORECASE)

    return (
        template_text
        .replace('{patient_name}', patient_name or 'there')
        .replace('{doctor_name}', clean_doctor_name or 'your provider')
        .replace('{practice_name}', practice_name or 'your medical practice')
        .replace('{procedure}', procedure or 'screening')
    )


# ── Conversation flow (asked in order after patient replies YES) ────────────
# Each entry: (conversation_state key, question text, patients column the answer is saved to)
QUESTION_FLOW = [
    ('q_dob',          "Can you confirm your date of birth (MM/DD/YYYY)?",                                                    'dob_confirmed'),
    ('q_callback',     "What's the best callback number to reach you at?",                                                    'callback_number'),
    ('q_address',      "What is your current mailing address?",                                                               'updated_address'),
    ('q_allergies',    "Do you have any medication allergies? Reply NONE if not.",                                            'allergies'),
    ('q_bloodthinner', "Are you currently taking any blood thinners or GLP-1 medications (e.g. Ozempic, Wegovy, Trulicity)? Reply NONE if not.", 'blood_thinner_glp1'),
    ('q_insurance',    "What insurance do you have? Please include the provider name and member ID if you have it handy.",    'insurance_info'),
    ('q_preference',   "Do you have a preference for appointment date or time?",                                              'scheduling_preference'),
]
QUESTION_STATES  = [q[0] for q in QUESTION_FLOW]
QUESTION_TEXT    = {q[0]: q[1] for q in QUESTION_FLOW}
QUESTION_COLUMN  = {q[0]: q[2] for q in QUESTION_FLOW}

CALL_REQUEST_KEYWORDS = ('CALL', 'PHONE', 'SPEAK', 'TALK')
YES_KEYWORDS          = ('YES', 'Y', 'SURE', 'OK', 'OKAY')
NO_KEYWORDS           = ('NO', 'N')
STOP_KEYWORDS         = ('STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT', 'OPTOUT', 'REVOKE')

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
@require_auth
def sms_send():
    """
    POST /sms/send
    Body: JSON { patient_id, message } — 'message' overrides the default template
    Sends a recall SMS to one patient via the Twilio Messaging Service and
    logs the outbound message to sms_conversations.
    Caller must have this patient in their scope.
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
        cur.execute("""
            SELECT p.*, pr.name AS doctor_name, prac.name AS practice_name
            FROM patients p
            LEFT JOIN profiles pr   ON pr.id   = p.provider_id
            LEFT JOIN practices prac ON prac.id = p.practice_id
            WHERE p.id = %s
        """, (patient_id,))
        patient = cur.fetchone()

        if not patient:
            cur.close()
            conn.close()
            return jsonify({'error': 'Patient not found'}), 404

        u = request.oncue_user
        in_scope = (u['role']=='superadmin') or (u['role']=='admin' and patient.get('practice_id')==u['practice_id']) or (u['role']=='provider' and patient.get('provider_id')==u['id'])
        if not in_scope:
            cur.close()
            conn.close()
            return jsonify({'error': 'Not authorized for this patient'}), 403

        to_number = patient.get('sms_number') or patient.get('phone')
        if not to_number:
            cur.close()
            conn.close()
            return jsonify({'error': 'Patient has no phone number on file'}), 400

        if not message:
            template = get_template_for_practice(cur, patient.get('practice_id'))
            first_name = (patient.get('name') or '').split(' ')[0]
            message = render_template(
                template,
                patient_name=first_name,
                doctor_name=patient.get('doctor_name'),
                practice_name=patient.get('practice_name'),
                procedure=patient.get('procedure'),
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
        cur.execute(
            "UPDATE patients SET status = %s, conversation_state = %s, last_contacted_at = NOW() WHERE id = %s",
            ('sms_sent', 'awaiting_consent', patient_id),
        )
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
@require_auth
def sms_send_bulk():
    """
    POST /sms/send-bulk
    Body: JSON { patient_ids: [...], message } — 'message' overrides the default template
    Sends the same recall message to multiple patients. Returns per-patient results
    so partial failures don't block the whole batch. Any patient outside the
    caller's scope is skipped with an authorization error rather than silently sent.
    """
    data         = request.get_json()
    patient_ids  = data.get('patient_ids', [])
    message      = data.get('message')
    u            = request.oncue_user

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
            cur.execute("""
                SELECT p.*, pr.name AS doctor_name, prac.name AS practice_name
                FROM patients p
                LEFT JOIN profiles pr    ON pr.id   = p.provider_id
                LEFT JOIN practices prac ON prac.id = p.practice_id
                WHERE p.id = %s
            """, (patient_id,))
            patient = cur.fetchone()

            if not patient:
                results.append({'patient_id': patient_id, 'success': False, 'error': 'not found'})
                continue

            in_scope = (u['role']=='superadmin') or (u['role']=='admin' and patient.get('practice_id')==u['practice_id']) or (u['role']=='provider' and patient.get('provider_id')==u['id'])
            if not in_scope:
                results.append({'patient_id': patient_id, 'success': False, 'error': 'not authorized'})
                continue

            to_number = patient.get('sms_number') or patient.get('phone')
            if not to_number:
                results.append({'patient_id': patient_id, 'success': False, 'error': 'no phone on file'})
                continue

            if message:
                body = message
            else:
                template = get_template_for_practice(cur, patient.get('practice_id'))
                first_name = (patient.get('name') or '').split(' ')[0]
                body = render_template(
                    template,
                    patient_name=first_name,
                    doctor_name=patient.get('doctor_name'),
                    practice_name=patient.get('practice_name'),
                    procedure=patient.get('procedure'),
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
                cur.execute(
                    "UPDATE patients SET status = %s, conversation_state = %s, last_contacted_at = NOW() WHERE id = %s",
                    ('sms_sent', 'awaiting_consent', patient_id),
                )
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
            log.warning(f"Invalid Twilio signature on inbound webhook. url={url} signature_present={bool(signature)}")
            return ('Invalid signature', 403)

    from_number = request.form.get('From', '')
    body        = (request.form.get('Body') or '').strip()
    message_sid = request.form.get('MessageSid', '')
    body_upper  = body.upper()

    resp = MessagingResponse()
    reply_text = None

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
        state       = (patient.get('conversation_state') if patient else None) or 'not_started'

        log_conversation(
            cur,
            practice_id=practice_id,
            patient_id=patient_id,
            direction='inbound',
            body=body,
            twilio_sid=message_sid,
            status='received',
        )

        if patient_id and state not in ('opted_out',):

            # Patient wants to talk on the phone instead of texting — works at any stage
            if any(kw in body_upper for kw in CALL_REQUEST_KEYWORDS):
                cur.execute(
                    "UPDATE patients SET status = %s, conversation_state = %s, contact_preference = %s WHERE id = %s",
                    ('ready_to_schedule_call_requested', 'call_requested', 'call', patient_id),
                )
                reply_text = "Got it — we'll have someone from the practice give you a call to go over scheduling. Thank you!"

            elif body_upper in STOP_KEYWORDS:
                cur.execute(
                    "UPDATE patients SET status = %s, conversation_state = %s WHERE id = %s",
                    ('opted_out', 'opted_out', patient_id),
                )
                # Twilio already sends the carrier-required opt-out confirmation; no extra reply needed.

            elif state in ('not_started', 'awaiting_consent'):
                if body_upper in YES_KEYWORDS:
                    first_q_state = QUESTION_STATES[0]
                    cur.execute(
                        "UPDATE patients SET status = %s, conversation_state = %s WHERE id = %s",
                        ('answering_questions', first_q_state, patient_id),
                    )
                    reply_text = QUESTION_TEXT[first_q_state]
                elif body_upper in NO_KEYWORDS:
                    cur.execute(
                        "UPDATE patients SET conversation_state = %s WHERE id = %s",
                        ('awaiting_reschedule_pref', patient_id),
                    )
                    reply_text = (
                        "No problem. Would you like us to reach out again at a later date, "
                        "or would you prefer not to schedule at this time? Reply LATER or NOT NOW."
                    )
                else:
                    reply_text = (
                        "Sorry, I didn't quite catch that — reply YES if you're able to answer a "
                        "few quick questions to get scheduled, or NO if not right now."
                    )

            elif state == 'awaiting_reschedule_pref':
                if 'LATER' in body_upper:
                    cur.execute(
                        "UPDATE patients SET status = %s, conversation_state = %s WHERE id = %s",
                        ('follow_up_later', 'follow_up_later', patient_id),
                    )
                    reply_text = "Understood — we'll reach back out at a later date. Thank you!"
                else:
                    cur.execute(
                        "UPDATE patients SET status = %s, conversation_state = %s WHERE id = %s",
                        ('declined', 'declined', patient_id),
                    )
                    reply_text = "No problem, thank you for letting us know."

            elif state in QUESTION_STATES:
                # Save the answer to this question's column
                column = QUESTION_COLUMN[state]
                cur.execute(f"UPDATE patients SET {column} = %s WHERE id = %s", (body, patient_id))

                idx = QUESTION_STATES.index(state)
                if idx + 1 < len(QUESTION_STATES):
                    next_state = QUESTION_STATES[idx + 1]
                    cur.execute("UPDATE patients SET conversation_state = %s WHERE id = %s", (next_state, patient_id))
                    reply_text = QUESTION_TEXT[next_state]
                else:
                    cur.execute(
                        "UPDATE patients SET status = %s, conversation_state = %s WHERE id = %s",
                        ('ready_to_schedule', 'complete', patient_id),
                    )
                    reply_text = (
                        "Thank you! We have everything we need — our scheduling team will follow up "
                        "shortly to confirm your appointment."
                    )
            # else: state is 'complete', 'declined', 'follow_up_later', or 'call_requested' —
            # conversation already finished, just log the message, no auto-reply.

        if reply_text:
            resp.message(reply_text)
            log_conversation(
                cur,
                practice_id=practice_id,
                patient_id=patient_id,
                direction='outbound',
                body=reply_text,
                status='auto_reply',
            )

        conn.commit()
        cur.close()
        conn.close()

        log.info(f"Inbound SMS from {from_number}: '{body}' (patient_id={patient_id}, state was {state})")

    except Exception as e:
        log.error(f"Inbound webhook processing failed: {e}", exc_info=True)
        # Still return valid empty TwiML so Twilio doesn't retry endlessly

    return str(resp), 200, {'Content-Type': 'text/xml'}


# ── Delivery status callback ─────────────────────────────────────────────────
@app.route('/sms/status-callback', methods=['POST'])
def sms_status_callback():
    """
    POST /sms/status-callback
    Twilio calls this for every status change on a message it sent on our behalf
    (queued -> sent -> delivered, or -> failed/undelivered). Configure this URL
    as the Messaging Service's "Delivery Status Callback" in the Twilio Console
    (separate field from the inbound "Incoming Messages" webhook).

    Updates the matching sms_conversations row's status, and — for failed or
    undelivered messages — flags the patient record so staff can see the send
    didn't actually reach them (a "sent" message that silently never arrives
    would otherwise look identical to one that did).
    """
    if TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        signature = request.headers.get('X-Twilio-Signature', '')
        if not validator.validate(request.url, request.form, signature):
            log.warning("Invalid Twilio signature on status callback")
            return ('Invalid signature', 403)

    message_sid   = request.form.get('MessageSid', '')
    message_status = request.form.get('MessageStatus', '')  # queued, sent, delivered, undelivered, failed
    error_code    = request.form.get('ErrorCode')
    error_message = request.form.get('ErrorMessage')

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("UPDATE sms_conversations SET status = %s WHERE twilio_sid = %s", (message_status, message_sid))

        if message_status in ('failed', 'undelivered'):
            cur.execute("SELECT patient_id FROM sms_conversations WHERE twilio_sid = %s LIMIT 1", (message_sid,))
            row = cur.fetchone()
            if row and row.get('patient_id'):
                error_detail = f"{error_code}: {error_message}" if error_code else message_status
                cur.execute(
                    "UPDATE patients SET last_delivery_status = %s, last_delivery_error = %s WHERE id = %s",
                    (message_status, error_detail, row['patient_id']),
                )
                log.warning(f"SMS delivery failed for patient {row['patient_id']}: {error_detail}")
        elif message_status == 'delivered':
            cur.execute("SELECT patient_id FROM sms_conversations WHERE twilio_sid = %s LIMIT 1", (message_sid,))
            row = cur.fetchone()
            if row and row.get('patient_id'):
                cur.execute(
                    "UPDATE patients SET last_delivery_status = %s, last_delivery_error = NULL WHERE id = %s",
                    (message_status, row['patient_id']),
                )

        conn.commit()
        cur.close()
        conn.close()

    except Exception as e:
        log.error(f"Status callback processing failed: {e}", exc_info=True)

    return ('', 204)


# ── Message templates (per practice) ──────────────────────────────────────────
@app.route('/practices', methods=['GET'])
@require_auth
def get_practices():
    """
    GET /practices — list practices with provider/patient counts.
    Superadmin sees all; a practice admin sees only their own; providers can't list practices.
    """
    u = request.oncue_user
    if u['role'] == 'provider':
        return jsonify({'error': 'Not authorized for this action'}), 403

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        base_sql = """
            SELECT
                p.id, p.name, p.address, p.phone, p.specialty, p.created_at,
                COUNT(DISTINCT pr.id) FILTER (WHERE pr.role = 'provider') AS provider_count,
                COUNT(DISTINCT pt.id) AS patient_count
            FROM practices p
            LEFT JOIN profiles pr ON pr.practice_id = p.id
            LEFT JOIN patients pt ON pt.practice_id = p.id
        """
        if u['role'] == 'admin':
            base_sql += " WHERE p.id = %s GROUP BY p.id ORDER BY p.name"
            cur.execute(base_sql, (u['practice_id'],))
        else:
            base_sql += " GROUP BY p.id ORDER BY p.name"
            cur.execute(base_sql)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error(f"Get practices failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/practices', methods=['POST'])
@require_auth
@require_role('superadmin')
def create_practice():
    """
    POST /practices — superadmin only.
    Body: JSON { name, address, phone, specialty }
    Creates a new practice.
    """
    data      = request.get_json()
    name      = (data.get('name') or '').strip()
    address   = data.get('address') or None
    phone     = data.get('phone') or None
    specialty = data.get('specialty') or None

    if not name:
        return jsonify({'error': 'name required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO practices (name, address, phone, specialty)
            VALUES (%s, %s, %s, %s)
            RETURNING *
        """, (name, address, phone, specialty))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"Created practice {name} ({row['id']})")
        return jsonify(dict(row))
    except Exception as e:
        log.error(f"Create practice failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/practices/update', methods=['POST'])
@require_auth
@require_role('admin', 'superadmin')
def update_practice():
    """
    POST /practices/update — admin/superadmin only.
    Body: JSON { id, name, address, phone, specialty }
    Admins may only update their own practice.
    """
    u    = request.oncue_user
    data = request.get_json()
    practice_id = data.get('id')

    if u['role'] == 'admin':
        practice_id = u['practice_id']  # can only ever update their own

    if not practice_id:
        return jsonify({'error': 'id required'}), 400

    name      = (data.get('name') or '').strip()
    address   = data.get('address') or None
    phone     = data.get('phone') or None
    specialty = data.get('specialty') or None

    if not name:
        return jsonify({'error': 'name required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE practices SET name = %s, address = %s, phone = %s, specialty = %s
            WHERE id = %s
            RETURNING *
        """, (name, address, phone, specialty, practice_id))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if not row:
            return jsonify({'error': 'Practice not found'}), 404
        return jsonify(dict(row))
    except Exception as e:
        log.error(f"Update practice failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/templates', methods=['GET'])
@require_auth
def get_template_route():
    """
    GET /templates?practice_id=xxx
    Returns the practice's custom template if one exists, otherwise the default.
    Admins may only view their own practice's template; superadmin may view any.
    """
    u = request.oncue_user
    if u['role'] == 'provider':
        return jsonify({'error': 'Not authorized for this action'}), 403
    practice_id = request.args.get('practice_id')
    if u['role'] == 'admin':
        practice_id = u['practice_id']  # ignore any other practice_id requested
    if not practice_id:
        return jsonify({'error': 'practice_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM message_templates WHERE practice_id = %s", (practice_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row:
            return jsonify({**dict(row), 'is_default': False})
        return jsonify({'practice_id': practice_id, 'template_text': DEFAULT_TEMPLATE, 'is_default': True})

    except Exception as e:
        log.error(f"Get template failed: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/templates', methods=['POST'])
@require_auth
def save_template_route():
    """
    POST /templates — admin/superadmin only.
    Body: JSON { practice_id, template_text }
    Creates or updates a practice's custom opening message template.
    Admins can only save their own practice's template.
    Supports placeholders: {patient_name} {doctor_name} {practice_name} {procedure}
    """
    u = request.oncue_user
    if u['role'] not in ('admin', 'superadmin'):
        return jsonify({'error': 'Not authorized for this action'}), 403

    data          = request.get_json()
    practice_id   = data.get('practice_id')
    template_text = data.get('template_text')

    if u['role'] == 'admin':
        practice_id = u['practice_id']  # admins can only ever save their own

    if not practice_id or not template_text:
        return jsonify({'error': 'practice_id and template_text required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO message_templates (practice_id, template_text, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (practice_id) DO UPDATE SET
                template_text = EXCLUDED.template_text,
                updated_at    = NOW()
            RETURNING *
        """, (practice_id, template_text))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify(dict(row))

    except Exception as e:
        log.error(f"Save template failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Get conversation history for a patient ───────────────────────────────────
@app.route('/sms/conversations', methods=['GET'])
@require_auth
def sms_conversations():
    """
    GET /sms/conversations?patient_id=xxx
    Returns SMS conversation history for a patient, oldest first.
    Caller must have this patient in their scope (own practice/own patient).
    """
    patient_id = request.args.get('patient_id')
    if not patient_id:
        return jsonify({'error': 'patient_id required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if not patient_in_scope(cur, patient_id, request.oncue_user):
            cur.close()
            conn.close()
            return jsonify({'error': 'Not authorized for this patient'}), 403

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
@require_auth
@require_role('admin', 'superadmin')
def parse():
    """
    POST /parse — admin/superadmin only.
    Body: multipart/form-data with 'file' (PDF or CSV)
           + 'practice_id' (UUID)
           + 'provider_id' (UUID)
           + 'lookup_phones' (bool, default true)
    Returns: JSON with parsed patients and stats
    Admins may only parse into their own practice.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400

    practice_id  = request.form.get('practice_id')
    provider_id  = request.form.get('provider_id')
    do_lookup    = request.form.get('lookup_phones', 'true').lower() == 'true'

    if request.oncue_user['role'] == 'admin':
        practice_id = request.oncue_user['practice_id']

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
@require_auth
@require_role('admin', 'superadmin')
def save_patients():
    """
    POST /save — admin/superadmin only.
    Body: JSON { patients: [...], practice_id, provider_id }
    Saves parsed patients to Cloud SQL. Admins may only save into their own practice.
    """
    data        = request.get_json()
    patients    = data.get('patients', [])
    practice_id = data.get('practice_id')
    provider_id = data.get('provider_id')

    if request.oncue_user['role'] == 'admin':
        practice_id = request.oncue_user['practice_id']

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
FIREBASE_WEB_API_KEY = os.environ.get('FIREBASE_WEB_API_KEY', 'AIzaSyCBFy58BRY_ijv3niOcxdcOCghMoaFXwO8')

@app.route('/create-user', methods=['POST'])
@require_auth
@require_role('admin', 'superadmin')
def create_user():
    """
    POST /create-user — admin/superadmin only.
    Body: JSON { name, email, password, role, practice_id }
    Creates a Firebase auth user (via the Identity Toolkit REST API using the
    project's Web API key — no service-account key required) + a profile row
    in Cloud SQL.
    Admins may only create providers/admins within their own practice — never
    a superadmin, and never in a different practice.
    """
    u = request.oncue_user
    data        = request.get_json()
    name        = data.get('name', '').strip()
    email       = data.get('email', '').strip()
    password    = data.get('password', '')
    role        = data.get('role', 'provider')
    practice_id = data.get('practice_id')

    if u['role'] == 'admin':
        practice_id = u['practice_id']  # can't create in another practice
        if role == 'superadmin':
            return jsonify({'error': 'Not authorized to create a superadmin'}), 403

    if not name or not email or not password:
        return jsonify({'error': 'name, email, and password are required'}), 400
    if role not in ('provider', 'admin', 'superadmin'):
        return jsonify({'error': 'Invalid role'}), 400

    try:
        # Create the Firebase Auth user via the public Identity Toolkit REST API.
        signup_url = f'https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_WEB_API_KEY}'
        resp = requests.post(signup_url, json={
            'email': email,
            'password': password,
            'displayName': name,
            'returnSecureToken': True,
        })
        resp_data = resp.json()

        if resp.status_code != 200 or 'localId' not in resp_data:
            err_msg = resp_data.get('error', {}).get('message', 'Unknown Firebase error')
            log.error(f"Firebase signUp failed for {email}: {err_msg}")
            return jsonify({'error': f'Firebase error: {err_msg}'}), 400

        user_id = resp_data['localId']
        log.info(f"Created Firebase user {email} with uid {user_id}")

        # Insert profile into Cloud SQL — firebase_uid goes in its own column,
        # id is left to auto-generate as a proper uuid.
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO profiles (firebase_uid, name, email, role, practice_id, active)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, name, email, role, practice_id, True))
        new_row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        log.info(f"Created profile {new_row['id']} for {email} ({role}) in practice {practice_id}")
        return jsonify({'success': True, 'id': new_row['id'], 'user_id': user_id, 'email': email})

    except Exception as e:
        log.error(f"Create user failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Delete practice endpoint ────────────────────────────────────────────────
@app.route('/delete-practice', methods=['POST'])
@require_auth
@require_role('superadmin')
def delete_practice():
    """
    POST /delete-practice — superadmin only.
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
    GET /profile
    Returns the caller's own profile + practice name.
    Verifies the caller's Firebase ID token and uses the UID FROM THE TOKEN —
    never trusts a firebase_uid passed as a query param, since that would let
    anyone fetch anyone else's profile (including role/practice_id) with no
    authentication at all.
    """
    token = get_bearer_token()
    if not token:
        return jsonify({'error': 'Missing Authorization header'}), 401
    try:
        decoded = verify_firebase_token(token)
    except Exception as e:
        log.warning(f"Token verification failed on /profile: {e}")
        return jsonify({'error': 'Invalid or expired token'}), 401

    firebase_uid = decoded.get('uid') or decoded.get('user_id') or decoded.get('sub')

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


@app.route('/profile/update', methods=['POST'])
@require_auth
def update_profile():
    """
    POST /profile/update — any authenticated user, own profile only.
    Body: JSON { name }
    """
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("UPDATE profiles SET name = %s WHERE id = %s RETURNING *", (name, request.oncue_user['id']))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify(dict(row))
    except Exception as e:
        log.error(f"Update profile failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Get patients ─────────────────────────────────────────────────────────────
@app.route('/patients', methods=['GET'])
@require_auth
def get_patients():
    """
    GET /patients?practice_id=xxx        → patients for one practice (admin, own practice only)
    GET /patients?provider_id=xxx        → patients for one provider (provider, self only)
    GET /patients?all=true               → every patient across every practice (superadmin only)
    Query params are overridden/ignored where they'd let a caller see beyond their own scope.
    """
    u = request.oncue_user
    practice_id = request.args.get('practice_id')
    provider_id = request.args.get('provider_id')
    all_flag    = request.args.get('all') == 'true'

    # Force scope based on the caller's actual role — never trust the client's query params
    if u['role'] == 'superadmin':
        pass  # all_flag / practice_id / provider_id as requested are fine
    elif u['role'] == 'admin':
        practice_id, provider_id, all_flag = u['practice_id'], None, False
    else:  # provider
        practice_id, provider_id, all_flag = None, u['id'], False

    if not practice_id and not provider_id and not all_flag:
        return jsonify({'error': 'practice_id, provider_id, or all=true required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if all_flag:
            cur.execute("SELECT * FROM patients ORDER BY recall_date")
        elif practice_id:
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
@require_auth
def get_providers():
    """
    GET /providers?practice_id=xxx   → providers for one practice (admin, own practice only)
    GET /providers?all=true          → every provider across every practice (superadmin only)
    Returns id, name, email, role, active, practice_id, practice_name.
    """
    u = request.oncue_user
    practice_id = request.args.get('practice_id')
    all_flag    = request.args.get('all') == 'true'
    role_filter = request.args.get('role', 'provider')  # 'provider' (default), 'all', or a specific role

    if u['role'] == 'superadmin':
        pass
    elif u['role'] == 'admin':
        practice_id, all_flag = u['practice_id'], False
    else:  # plain providers don't need to browse the roster
        return jsonify({'error': 'Not authorized for this action'}), 403

    if not practice_id and not all_flag:
        return jsonify({'error': 'practice_id or all=true required'}), 400

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        base_sql = """
            SELECT pr.id, pr.name, pr.email, pr.role, pr.active, pr.practice_id, p.name AS practice_name
            FROM profiles pr
            LEFT JOIN practices p ON p.id = pr.practice_id
            WHERE 1=1
        """
        params = []
        if role_filter != 'all':
            base_sql += " AND pr.role = %s"
            params.append(role_filter)
        if practice_id:
            base_sql += " AND pr.practice_id = %s ORDER BY pr.name"
            params.append(practice_id)
        else:
            base_sql += " ORDER BY p.name, pr.name"

        cur.execute(base_sql, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        log.error(f"Get providers failed: {e}")
        return jsonify({'error': str(e)}), 500


# ── Update patient ─────────────────────────────────────────────────────────────
@app.route('/patients/update', methods=['POST'])
@require_auth
def update_patient():
    """
    POST /patients/update
    Body: JSON { id, ...fields }
    Updates a patient row. Caller must have this patient in their scope
    (own practice for admins, own assigned patients for providers).
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
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if not patient_in_scope(cur, patient_id, request.oncue_user):
            cur.close()
            conn.close()
            return jsonify({'error': 'Not authorized for this patient'}), 403

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
@require_auth
@require_role('admin', 'superadmin')
def add_patient():
    """
    POST /patients/add — admin/superadmin only.
    Body: JSON { practice_id, provider_id, name, ... }
    Adds a single patient. Admins may only add into their own practice.
    """
    data = request.get_json()
    practice_id = data.get('practice_id')
    if request.oncue_user['role'] == 'admin':
        practice_id = request.oncue_user['practice_id']
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
            practice_id,
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
