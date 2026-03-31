"""
One-time idempotent migration for patient field-level encryption.

Encrypts existing plaintext values for:
- patients.email
- patients.phone
- patient_profile_compat.medical_summary

Usage:
  python migrate_field_encryption.py
"""

from app import create_app
from models import db, Patient, PatientProfileCompat
from security_utils import encrypt_text


def _needs_encryption(value):
    return bool(value) and isinstance(value, str) and not value.startswith("enc::")


def run_migration():
    app = create_app()
    with app.app_context():
        patients_updated = 0
        profiles_updated = 0

        for patient in Patient.query.all():
            changed = False
            if _needs_encryption(patient.email):
                patient.email = encrypt_text(patient.email)
                changed = True
            if _needs_encryption(patient.phone):
                patient.phone = encrypt_text(patient.phone)
                changed = True
            if changed:
                patients_updated += 1

        for profile in PatientProfileCompat.query.all():
            if _needs_encryption(profile.medical_summary):
                profile.medical_summary = encrypt_text(profile.medical_summary)
                profiles_updated += 1

        if patients_updated or profiles_updated:
            db.session.commit()

        print(
            f"migration_complete patients_updated={patients_updated} "
            f"profiles_updated={profiles_updated}"
        )


if __name__ == "__main__":
    run_migration()
