# Nottingham Grade Extraction Prompt v1 (SPEC-02 §4 step 3)

You read one breast-cancer surgical pathology report, given after these instructions as a delimited document. It may be OCR text, in capitals, in English or another language, and may contain form fields and noise.

Extract the Nottingham (Elston-Ellis) grading of the INVASIVE carcinoma, as the report states it. The same system is also written as modified Bloom-Richardson, Scarff-Bloom-Richardson, SBR, mSBR, ESBR or combined/composite histologic grade.

Fields:
- grade: the overall histologic grade of the invasive carcinoma, 1, 2 or 3. "II/III", "2 of 3", "G2" mean 2. Words such as "moderately differentiated" count only when they are given as that grade (for example "Nottingham grade: moderately differentiated").
- total: the Nottingham total score, 3 to 9 ("score 7/9" means 7).
- tubule: the tubule / gland formation (architectural) score, 1 to 3.
- pleo: the nuclear pleomorphism score of the invasive carcinoma, 1 to 3. "Nuclei 2" or "nuclear grade 2" inside the Nottingham components counts.
- mitoses: the mitotic count score, 1 to 3 (not the raw count per HPF).

Rules:
- Use null for any field the report does not state. Never infer, estimate or compute a value that is not written.
- Ignore grades of in situ carcinoma (DCIS or LCIS nuclear grade), nuclear grade systems that are not part of the Nottingham grade (for example Black's), and every other score (Oncotype recurrence score, HER2 score, Ki-67, AJCC stage).
- If the report gives different grades or scores for more than one invasive tumour or specimen, use null for every field.
- For every non-null field add one evidence item: the field name and a quote copied character for character from the document that states the value. Keep quotes short (one phrase).

Respond strictly as JSON with this schema:
{
  "grade": <1 | 2 | 3 | null>,
  "total": <3..9 | null>,
  "tubule": <1 | 2 | 3 | null>,
  "pleo": <1 | 2 | 3 | null>,
  "mitoses": <1 | 2 | 3 | null>,
  "evidence": [{"field": "<grade | total | tubule | pleo | mitoses>", "quote": "<verbatim text>"}]
}
