# Nuclear Pleomorphism Verification Prompt v1

You are an expert breast pathologist checking one field for Nottingham grading of invasive breast carcinoma.
Image: one 128×128 µm H&E field of invasive breast carcinoma at high power (40×, 0.25 µm/pixel).

Score the nuclear pleomorphism of the invasive tumour cells in this field, compared with normal breast epithelium:
- 1: Small, regular, uniform nuclei with even chromatin, similar to normal ductal epithelial cells.
- 2: Moderate increase in size and variation, open chromatin, visible nucleoli.
- 3: Marked variation in size and shape, vesicular chromatin, prominent (often multiple) nucleoli.

Answer with this JSON object only, with no other text:
{"pleomorphism_score": <1 | 2 | 3>}
