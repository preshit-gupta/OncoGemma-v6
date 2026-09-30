# Nottingham Grade Extraction Prompt v2 (SPEC-02 §4 step 3)

You read one breast-cancer surgical pathology report, given after these instructions as a delimited document. It may be OCR text, in capitals, in English, German or Polish, and may contain form fields and noise.

Extract the Nottingham (Elston-Ellis) grading of the INVASIVE carcinoma, exactly as the report states it. The same system is also written as modified Bloom-Richardson (B-R), Scarff-Bloom-Richardson, SBR, mSBR, ESBR, NHG, "malignancy grade", combined/composite histologic grade, or "G1/G2/G3" in a TNM line.

Fields:
- grade: the overall histologic grade of the invasive carcinoma, 1, 2 or 3. "II/III", "2 of 3", "G2", "NHG2" mean 2. The words well / moderately / poorly differentiated count only when the report gives them as the grade of a named system (for example "Nottingham grade: moderately differentiated"); a bare "Histologic grade: moderately differentiated" does not count.
- total: the Nottingham total score, 3 to 9 ("score 7/9" means 7).
- tubule: the tubule / gland formation (architecture) score, 1 to 3.
- pleo: the nuclear pleomorphism score of the invasive carcinoma, 1 to 3. "Nuclei 2", "nuclear score 2" or "nuclear grade 2" given among the Nottingham grade statements counts.
- mitoses: the mitotic score, 1 to 3 (not the raw count per HPF).
- A sum such as "(3+2+1)" or "A/N/M = 3/2/1" next to the grade lists tubule, pleo and mitoses in that order.

Rules:
- Use null for any field the report does not state as a number or roman numeral (or, for grade only, as the differentiation words above). Never infer a score from a description: "high mitotic index", "slight or no tubule formation" or "marked pleomorphism" are not scores. Never compute a value that is not written.
- Ignore grades of in situ carcinoma (DCIS nuclear grade, LCIS or lobular intraepithelial neoplasia), nuclear grade systems that are not part of the Nottingham grade (for example Black's), and every other score (Oncotype recurrence score, HER2 score, Ki-67, AJCC stage).
- If the report gives different grades or scores for more than one invasive tumour or specimen (including an earlier biopsy quoted in the history), use null for every field.
- For every non-null field add one evidence item: the field name and a quote copied character for character from the document. The quote must contain both the words that name the field and the value, for example "Mitotic count: 2" or "NOTTINGHAM GRADE: 2 OF 3", never a bare "2". Keep quotes to one phrase.

Respond strictly as JSON with this schema:
{
  "grade": <1 | 2 | 3 | null>,
  "total": <3..9 | null>,
  "tubule": <1 | 2 | 3 | null>,
  "pleo": <1 | 2 | 3 | null>,
  "mitoses": <1 | 2 | 3 | null>,
  "evidence": [{"field": "<grade | total | tubule | pleo | mitoses>", "quote": "<verbatim text>"}]
}
