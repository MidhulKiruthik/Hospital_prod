import sqlite3

conn = sqlite3.connect('/data/hospital.db')
cur = conn.cursor()

queries = [
    "select count(1) from patients where email != '' and email not like 'enc::%'",
    "select count(1) from patients where phone != '' and phone not like 'enc::%'",
    "select count(1) from patient_profile_compat where medical_summary != '' and medical_summary not like 'enc::%'",
]

for q in queries:
    print(cur.execute(q).fetchone()[0])
