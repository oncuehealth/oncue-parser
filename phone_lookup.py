"""
OnCue Health — Twilio Phone Lookup
Determines if a number is mobile (SMS-able), landline, or VoIP
"""

import os
import logging
from twilio.rest import Client

log = logging.getLogger(__name__)

# Phone type → SMS capability mapping
SMS_CAPABLE = {
    'mobile':   True,
    'voip':     True,   # Usually SMS-capable (Google Voice, etc.)
    'landline': False,
    'unknown':  None,   # Can't determine
}

def lookup_phone(phone_number, account_sid, auth_token):
    """
    Look up a phone number via Twilio Lookup API
    Returns dict with type, sms_capable, carrier info
    """
    if not phone_number or len(phone_number.replace('-','').replace(' ','')) < 10:
        return {'type': 'unknown', 'sms_capable': None, 'error': 'Invalid number'}

    try:
        client = Client(account_sid, auth_token)

        # Format to E.164
        digits = ''.join(filter(str.isdigit, phone_number))
        if len(digits) == 10:
            e164 = f'+1{digits}'
        elif len(digits) == 11 and digits[0] == '1':
            e164 = f'+{digits}'
        else:
            return {'type': 'unknown', 'sms_capable': None, 'error': 'Cannot format number'}

        # Twilio Lookup v2 with line_type_intelligence
        number = client.lookups.v2.phone_numbers(e164).fetch(
            fields='line_type_intelligence'
        )

        line_type = None
        carrier_name = None

        if number.line_type_intelligence:
            line_type = number.line_type_intelligence.get('type', 'unknown')
            carrier_name = number.line_type_intelligence.get('carrier_name', '')

        sms_capable = SMS_CAPABLE.get(line_type)

        return {
            'number':      phone_number,
            'e164':        e164,
            'type':        line_type or 'unknown',
            'sms_capable': sms_capable,
            'carrier':     carrier_name,
            'valid':       number.valid,
            'error':       None,
        }

    except Exception as e:
        log.warning(f"Twilio lookup failed for {phone_number}: {e}")
        return {
            'number':      phone_number,
            'type':        'unknown',
            'sms_capable': None,
            'carrier':     None,
            'valid':       None,
            'error':       str(e),
        }

def lookup_patient_phones(patient, account_sid, auth_token):
    """
    Look up all phones for a patient and determine best SMS number
    Returns updated patient dict with phone type info
    """
    results = []

    phones_to_check = []
    if patient.get('phone'):
        phones_to_check.append({
            'number': patient['phone'],
            'label':  patient.get('phone_label', 'home')
        })
    if patient.get('phone2'):
        phones_to_check.append({
            'number': patient['phone2'],
            'label':  patient.get('phone2_label', 'work')
        })

    for phone_info in phones_to_check:
        result = lookup_phone(phone_info['number'], account_sid, auth_token)
        result['label'] = phone_info['label']
        results.append(result)
        log.info(f"  {phone_info['number']} ({phone_info['label']}): {result['type']} — SMS: {result['sms_capable']}")

    # Determine best SMS number
    sms_number = None
    sms_number_type = None

    for r in results:
        if r['sms_capable'] is True:
            sms_number = r['number']
            sms_number_type = r['type']
            break

    # If no confirmed mobile, use first number as fallback
    if not sms_number and results:
        sms_number = results[0]['number']
        sms_number_type = results[0]['type']

    return {
        **patient,
        'phone_lookups':   results,
        'sms_number':      sms_number,
        'sms_number_type': sms_number_type,
        'sms_capable':     sms_number is not None,
        # Update primary phone display info
        'phone_type':      results[0]['type'] if results else 'unknown',
        'phone2_type':     results[1]['type'] if len(results) > 1 else None,
    }

