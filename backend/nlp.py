"""
Clinical NLP processor with rule-based extraction and optional transformer summarization.
"""

import os
import re
import time
from datetime import datetime
from functools import lru_cache

from flask import current_app, has_app_context


SYMPTOMS = {
    'pain', 'fever', 'cough', 'fatigue', 'nausea', 'vomiting', 'dizziness',
    'headache', 'shortness of breath', 'chest pain', 'abdominal pain',
    'back pain', 'joint pain', 'swelling', 'rash', 'itching', 'diarrhea',
    'constipation', 'insomnia', 'anxiety', 'palpitations', 'weakness',
    'numbness', 'tingling', 'weight loss', 'weight gain', 'loss of appetite',
    'sore throat', 'runny nose', 'congestion', 'blurred vision', 'tinnitus',
    'muscle ache', 'chills', 'night sweats', 'bleeding', 'bruising',
}

DIAGNOSES = {
    'hypertension', 'diabetes', 'asthma', 'copd', 'pneumonia', 'bronchitis',
    'sinusitis', 'gastritis', 'gerd', 'ibs', 'uti', 'arthritis', 'migraine',
    'anemia', 'hypothyroidism', 'hyperthyroidism', 'depression', 'anxiety disorder',
    'eczema', 'psoriasis', 'dermatitis', 'fracture', 'sprain', 'laceration',
    'myocardial infarction', 'stroke', 'appendicitis', 'cholecystitis',
    'kidney stones', 'upper respiratory infection', 'lower respiratory infection',
    'viral infection', 'bacterial infection', 'allergic reaction',
}

MEDICATIONS = {
    'paracetamol', 'ibuprofen', 'aspirin', 'amoxicillin', 'azithromycin',
    'metformin', 'insulin', 'amlodipine', 'atorvastatin', 'omeprazole',
    'pantoprazole', 'salbutamol', 'montelukast', 'cetirizine', 'loratadine',
    'doxycycline', 'ciprofloxacin', 'metronidazole', 'prednisolone',
    'hydrocortisone', 'ranitidine', 'levofloxacin', 'cefixime',
    'hydroxychloroquine', 'lisinopril', 'losartan',
}

PROCEDURES = {
    'ecg', 'x-ray', 'mri', 'ct scan', 'ultrasound', 'blood test',
    'urine test', 'complete blood count', 'cbc', 'culture', 'biopsy',
    'endoscopy', 'colonoscopy', 'echo', 'echocardiography', 'spirometry',
    'glucose test', 'hba1c', 'thyroid function test', 'lipid profile',
    'kidney function test', 'liver function test', 'chest x-ray',
}


def _transformer_enabled() -> bool:
    if has_app_context():
        return current_app.config.get('ENABLE_TRANSFORMER_SUMMARIZATION', False)
    return os.environ.get('ENABLE_TRANSFORMER_SUMMARIZATION', 'False').lower() in ('true', '1', 'yes', 'on')


def _transformer_model_name() -> str:
    if has_app_context():
        return current_app.config.get('TRANSFORMER_MODEL_NAME', 'sshleifer/distilbart-cnn-12-6')
    return os.environ.get('TRANSFORMER_MODEL_NAME', 'sshleifer/distilbart-cnn-12-6')


def _extract_entities(text: str, entity_set: set) -> list:
    text_lower = text.lower()
    found = []
    for entity in sorted(entity_set, key=len, reverse=True):
        if entity in text_lower and entity not in found:
            found.append(entity)
    return found


def _extract_vitals(text: str) -> dict:
    vitals = {}
    patterns = {
        'bp': r'(?:bp|blood pressure)[:\s]+(\d{2,3}/\d{2,3})',
        'hr': r'(?:hr|heart rate|pulse)[:\s]+(\d{2,3})\s*(?:bpm)?',
        'temp': r'(?:temp|temperature)[:\s]+(\d{2,3}(?:\.\d)?)\s*[CF]?',
        'spo2': r'(?:spo2|oxygen saturation|o2 sat)[:\s]+(\d{2,3})\s*%?',
        'rr': r'(?:rr|respiratory rate)[:\s]+(\d{1,2})\s*(?:/min)?',
        'weight': r'weight[:\s]+(\d{2,3}(?:\.\d)?)\s*(?:kg|lbs)?',
        'height': r'height[:\s]+(\d{3}(?:\.\d)?)\s*(?:cm)?',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            vitals[key] = match.group(1)
    return vitals


def _extract_duration(text: str) -> str:
    patterns = [
        r'(?:for|since|past|last)\s+(\d+\s+(?:day|week|month|year)s?)',
        r'(\d+)\s*-?\s*(?:day|week|month|year)s?\s+(?:history|ago|duration)',
        r'onset\s+(\d+\s+(?:day|week|month|year)s?)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return ''


def _infer_chief_complaint(text: str, symptoms: list) -> str:
    patterns = [
        r'(?:c/o|complains? of|presenting with|chief complaint)[:\s]+([^.;,\n]+)',
        r'(?:came|presenting|referred)\s+(?:for|with)[:\s]+([^.;,\n]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().capitalize()

    sentences = [sentence.strip() for sentence in re.split(r'[.!?]', text) if sentence.strip()]
    if sentences:
        return sentences[0][:150]
    if symptoms:
        return f"Patient presenting with {', '.join(symptoms[:3])}"
    return 'General consultation'


def _infer_assessment(text: str, diagnoses: list) -> str:
    patterns = [
        r'(?:diagnosis|assessment|impression|dx)[:\s]+([^.;,\n]+)',
        r'(?:likely|consistent with|suggestive of)[:\s]*([^.;,\n]+)',
        r'(?:diagnosed|confirmed)\s+(?:with|as)[:\s]+([^.;,\n]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip().capitalize()

    if diagnoses:
        return ', '.join(item.capitalize() for item in diagnoses[:3])
    return 'Assessment pending clinical evaluation'


def _infer_plan(text: str, medications: list, procedures: list) -> str:
    plan_parts = []
    patterns = [
        r'(?:plan|treatment|management|advised|prescribed)[:\s]+([^.;\n]{5,150})',
        r'(?:start|continue|add|refer)\s+([^.;\n]{5,100})',
        r'(?:follow.?up|review)\s+(?:in|after)\s+([^.;\n]{3,60})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            plan_parts.append(match.group(1).strip().capitalize())

    if medications:
        plan_parts.append(f"Prescribed: {', '.join(medications[:5])}")
    if procedures:
        plan_parts.append(f"Investigations: {', '.join(procedures[:4])}")

    followup = re.search(r'follow.?up\s+(?:in|after)\s+([\w\s]+)', text, re.IGNORECASE)
    if followup:
        plan_parts.append(f"Follow-up: {followup.group(1).strip()}")

    return '. '.join(plan_parts) if plan_parts else 'Symptomatic management. Follow-up as needed.'


@lru_cache(maxsize=1)
def _load_transformer_pipeline(model_name: str):
    from transformers import pipeline

    return pipeline('summarization', model=model_name)


def generate_with_transformer(notes: str, model_name: str = None):
    model_name = model_name or _transformer_model_name()
    try:
        summarizer = _load_transformer_pipeline(model_name)
        if len(notes.split()) < 25:
            return None, model_name
        summary = summarizer(notes, max_length=140, min_length=40, do_sample=False)
        return summary[0]['summary_text'], model_name
    except Exception:
        return None, model_name


def generate_clinical_summary(notes: str, appointment_data: dict = None) -> dict:
    started_at = time.time()

    if not notes or len(notes.strip()) < 5:
        return {
            'chief_complaint': 'No notes provided',
            'findings': '',
            'assessment': 'Insufficient notes for summary generation',
            'plan': '',
            'summary_text': 'Consultation notes not available.',
            'entities': {},
            'vitals': {},
            'processing_time_s': 0.0,
            'method': 'rule-based-nlp',
            'model_name': '',
            'status': 'error',
        }

    symptoms = _extract_entities(notes, SYMPTOMS)
    diagnoses = _extract_entities(notes, DIAGNOSES)
    medications = _extract_entities(notes, MEDICATIONS)
    procedures = _extract_entities(notes, PROCEDURES)
    vitals = _extract_vitals(notes)
    duration = _extract_duration(notes)

    chief_complaint = _infer_chief_complaint(notes, symptoms)
    assessment = _infer_assessment(notes, diagnoses)
    plan = _infer_plan(notes, medications, procedures)

    findings_parts = []
    if symptoms:
        findings_parts.append(f"Symptoms: {', '.join(item.capitalize() for item in symptoms[:6])}")
    if duration:
        findings_parts.append(f"Duration: {duration}")
    if vitals:
        vital_str = ', '.join(f"{key.upper()}: {value}" for key, value in vitals.items())
        findings_parts.append(f"Vitals: {vital_str}")
    if procedures:
        findings_parts.append(f"Investigations ordered: {', '.join(item.upper() for item in procedures[:4])}")
    findings = '. '.join(findings_parts) or 'Clinical examination performed.'

    transformer_summary = None
    transformer_model = ''
    method = 'rule-based-nlp'
    if _transformer_enabled():
        transformer_summary, transformer_model = generate_with_transformer(notes)
        if transformer_summary:
            method = 'transformer+rule-based'

    ctx = appointment_data or {}
    header = ''
    if ctx.get('patient_name'):
        header = (
            f"CLINICAL SUMMARY\n"
            f"Patient: {ctx['patient_name']}  |  "
            f"Doctor: {ctx.get('doctor_name', 'N/A')}  |  "
            f"Date: {ctx.get('date', datetime.utcnow().strftime('%Y-%m-%d'))}  |  "
            f"Specialty: {ctx.get('specialty', 'General')}\n"
            f"{'-' * 60}\n"
        )

    summary_sections = []
    if transformer_summary:
        summary_sections.append(f"EXECUTIVE SUMMARY: {transformer_summary}")
    summary_sections.extend([
        f"CHIEF COMPLAINT: {chief_complaint}",
        f"FINDINGS: {findings}",
        f"ASSESSMENT: {assessment}",
        f"PLAN: {plan}",
    ])
    summary_body = '\n\n'.join(summary_sections)

    return {
        'chief_complaint': chief_complaint,
        'findings': findings,
        'assessment': assessment,
        'plan': plan,
        'summary_text': f"{header}{summary_body}",
        'entities': {
            'symptoms': symptoms,
            'diagnoses': diagnoses,
            'medications': medications,
            'procedures': procedures,
        },
        'vitals': vitals,
        'duration': duration,
        'processing_time_s': round(time.time() - started_at, 3),
        'method': method,
        'model_name': transformer_model,
        'status': 'ready',
    }
