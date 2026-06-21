"""
OnCue Health — PDF Recall List Parser
Handles: PDF → OCR → patient extraction → Twilio phone lookup
"""

import re
import os
import json
import logging
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
import numpy as np

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Phone number patterns ──────────────────────────────────────────────────
PHONE_RE = re.compile(
    r'(?:H:|W:|Cell:|C:)?\s*'
    r'(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})'
    r'(?:\s+Ext[:\s\.]*(\S+))?',
    re.IGNORECASE
)

DATE_RE    = re.compile(r'\b(\d{1,2}/\d{1,2}/\d{4})\b')
PATIENT_RE = re.compile(r'(\d{5,7})\s+([A-Z][a-zA-Z\-\']+(?:\s+[A-Z]\.?)?\s+[A-Z][a-zA-Z\-\']+(?:\s+[A-Z][a-zA-Z\-\']+)?(?:\s+Jr\.?|Sr\.?|III?|IV)?)')
RECALL_RE  = re.compile(r'(COLON-\d|EGD-\d|FLEX-\d|MAMMO-\d|PAP-\d|LUNG-\d|EYE-\d)', re.IGNORECASE)
FLAG_RE = {
    'propofol':     re.compile(r'propof[ao]l', re.IGNORECASE),
    'blood_thinner':re.compile(r'coumadin|eliquis|xarelto|warfarin|plavix|blood\s+thin', re.IGNORECASE),
    'glp1':         re.compile(r'ozempic|wegovy|mounjaro|glp.?1|semaglutide|tirzepatide', re.IGNORECASE),
    'alt_provider': re.compile(r'due\s+w\s+dr|see\s+dr|with\s+dr\s+(?!r\.muggia)', re.IGNORECASE),
}

PROCEDURE_MAP = {
    'COLON': 'Colonoscopy',
    'EGD':   'Upper Endoscopy',
    'FLEX':  'Flex Sigmoidoscopy',
    'MAMMO': 'Mammogram',
    'PAP':   'Cervical Screening',
    'LUNG':  'Lung CT',
    'EYE':   'Diabetic Eye Exam',
}

def preprocess_image(img):
    """Enhance image for better OCR accuracy"""
    # Convert to grayscale
    img = img.convert('L')
    # Increase contrast
    img = ImageEnhance.Contrast(img).enhance(2.2)
    # Sharpen
    img = img.filter(ImageFilter.SHARPEN)
    img = img.filter(ImageFilter.SHARPEN)
    # Scale up for better OCR (300 DPI equivalent)
    w, h = img.size
    if w < 1800:
        scale = 2400 / w
        img = img.resize((int(w*scale), int(h*scale)), Image.LANCZOS)
    return img

def extract_phones_from_line(line):
    """Extract all phone numbers from a line, with H:/W: labels"""
    phones = []
    # Look for labeled phones first
    h_match = re.search(r'H:\s*(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})(?:\s+Ext[:\s]*(\S+))?', line, re.IGNORECASE)
    w_match = re.search(r'W:\s*(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})(?:\s+Ext[:\s]*(\S+))?', line, re.IGNORECASE)
    c_match = re.search(r'(?:Cell|C):\s*(\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})', line, re.IGNORECASE)

    if h_match:
        num = clean_phone(h_match.group(1))
        ext = h_match.group(2) or ''
        phones.append({'number': num, 'label': 'home', 'ext': ext})
    if w_match:
        num = clean_phone(w_match.group(1))
        ext = w_match.group(2) or ''
        phones.append({'number': num, 'label': 'work', 'ext': ext})
    if c_match:
        num = clean_phone(c_match.group(1))
        phones.append({'number': num, 'label': 'cell', 'ext': ''})

    # If no labeled phones found, grab any phone number
    if not phones:
        for m in PHONE_RE.finditer(line):
            num = clean_phone(m.group(1))
            if num and len(num) >= 10:
                phones.append({'number': num, 'label': 'unknown', 'ext': m.group(2) or ''})

    return phones

def clean_phone(raw):
    """Normalize phone to digits only, return formatted"""
    if not raw:
        return ''
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return ''

def detect_flags(text):
    """Detect clinical flags from text"""
    flags = []
    for flag, pattern in FLAG_RE.items():
        if pattern.search(text):
            flags.append(flag)
    return flags

def parse_recall_pdf(pdf_path):
    """
    Main parser: PDF → list of patient dicts
    Returns: { patients: [...], stats: {...}, errors: [...] }
    """
    from pdf2image import convert_from_path

    log.info(f"Parsing PDF: {pdf_path}")
    patients = []
    errors = []
    raw_lines = []

    # Convert PDF pages to images
    try:
        pages = convert_from_path(pdf_path, dpi=300)
        log.info(f"PDF has {len(pages)} pages")
    except Exception as e:
        return {'patients': [], 'stats': {}, 'errors': [f"PDF conversion failed: {str(e)}"]}

    # OCR each page
    for page_num, page_img in enumerate(pages):
        try:
            img = preprocess_image(page_img)
            # Use Tesseract with optimized config for tabular data
            text = pytesseract.image_to_string(
                img,
                config='--psm 6 --oem 3 -c tessedit_char_whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/:.-() ,\'"'
            )
            raw_lines.extend(text.split('\n'))
            log.info(f"Page {page_num+1}: extracted {len(text.split(chr(10)))} lines")
        except Exception as e:
            errors.append(f"Page {page_num+1} OCR failed: {str(e)}")

    # Parse lines into patient records
    current = None
    practice_name = None
    report_date = None

    for i, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue

        # Detect practice name (usually in header)
        if 'associates' in line.lower() or 'health' in line.lower() and len(line) > 10 and i < 20:
            if not practice_name:
                practice_name = line.strip()

        # Detect report date
        if 'recall report' in line.lower() or ('/' in line and len(line) < 30 and i < 10):
            d = DATE_RE.search(line)
            if d and not report_date:
                report_date = d.group(1)

        # Check if line starts with a recall date (new patient row)
        date_match = DATE_RE.match(line)
        if date_match:
            # Save previous patient
            if current and current.get('name'):
                patients.append(current)

            recall_date = date_match.group(1)

            # Find patient number and name
            pat_match = PATIENT_RE.search(line)
            if pat_match:
                patient_no = pat_match.group(1)
                raw_name   = pat_match.group(2).strip()
                # Normalize name: remove extra spaces, fix capitalization
                parts = raw_name.split()
                name = ' '.join(p.capitalize() for p in parts if p)
            else:
                patient_no = ''
                name = ''

            # Extract phones from this line
            phones = extract_phones_from_line(line)

            # Get recall type
            recall_match = RECALL_RE.search(line)
            recall_code  = recall_match.group(1).upper() if recall_match else 'COLON-1'
            procedure    = PROCEDURE_MAP.get(recall_code.split('-')[0], 'Colonoscopy')

            # Get flags from this line
            flags = detect_flags(line)

            # Extract comments (text after recall type and resource)
            comments = ''
            comment_match = re.search(r'RMUGGIA\s+(.+)$', line, re.IGNORECASE)
            if comment_match:
                comments = comment_match.group(1).strip()

            current = {
                'recall_date': recall_date,
                'patient_no':  patient_no,
                'name':        name,
                'phones':      phones,
                'phone':       phones[0]['number'] if phones else '',
                'phone2':      phones[1]['number'] if len(phones) > 1 else '',
                'phone_label': phones[0]['label'] if phones else '',
                'phone2_label':phones[1]['label'] if len(phones) > 1 else '',
                'procedure':   procedure,
                'recall_code': recall_code,
                'flags':       flags,
                'comments':    comments,
                'confidence':  'high' if patient_no and phones else 'low',
                'raw_line':    line,
            }
            continue

        # Continuation line — add to current patient
        if current:
            # Look for additional phone numbers
            extra_phones = extract_phones_from_line(line)
            for ep in extra_phones:
                if ep['number'] and ep['number'] not in [p['number'] for p in current['phones']]:
                    current['phones'].append(ep)
                    if not current['phone']:
                        current['phone'] = ep['number']
                        current['phone_label'] = ep['label']
                    elif not current['phone2']:
                        current['phone2'] = ep['number']
                        current['phone2_label'] = ep['label']

            # Add to comments
            extra_flags = detect_flags(line)
            for f in extra_flags:
                if f not in current['flags']:
                    current['flags'].append(f)

            if line and not DATE_RE.match(line) and len(line) > 3:
                if current['comments']:
                    current['comments'] += ' ' + line
                else:
                    current['comments'] = line

    # Save last patient
    if current and current.get('name'):
        patients.append(current)

    # Filter out clearly bad rows
    valid = [p for p in patients if p['name'] and len(p['name']) > 3]
    low_conf = [p for p in valid if p['confidence'] == 'low']
    high_conf = [p for p in valid if p['confidence'] == 'high']

    log.info(f"Parsed {len(valid)} patients ({len(high_conf)} high confidence, {len(low_conf)} low confidence)")

    return {
        'patients':      valid,
        'practice_name': practice_name,
        'report_date':   report_date,
        'stats': {
            'total':           len(valid),
            'high_confidence': len(high_conf),
            'low_confidence':  len(low_conf),
            'with_two_phones': sum(1 for p in valid if p['phone2']),
            'with_flags':      sum(1 for p in valid if p['flags']),
            'errors':          len(errors),
        },
        'errors': errors,
    }

