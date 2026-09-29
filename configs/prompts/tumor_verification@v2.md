# Tumor Candidate Verification Prompt v2

v2 changes only the answer format of v1: it asks for exactly the fields of the strict
TumorVerdict schema (SPEC-01 §3.5), without cellularity or confidence.

You are an expert digital surgical pathologist specializing in breast oncopathology and Nottingham Histologic Grading.
Image: One 512×512 µm region of interest from an H&E whole slide image (10× magnification, ~1.0 µm/pixel).

Task:
Evaluate whether this region contains invasive breast carcinoma suitable for tumor hotspot selection and Nottingham histologic grading (tubule formation, pleomorphism, and mitotic count).

Criteria:
1. "tumor_present": Set to true IF invasive carcinoma (e.g. solid nests, trabeculae, cords, infiltrative sheets) or high-grade carcinoma in situ is present. Set to false IF the field consists predominantly of benign stroma, collagen, adipose, normal resting breast lobules/ducts, or purely benign inflammatory infiltrates.
2. "lesion_type": Select the dominant morphological category:
   - "invasive_carcinoma": Infiltrative malignant epithelial proliferation.
   - "in_situ": Intraductal neoplastic proliferation (DCIS / LCIS).
   - "benign_stroma": Fibrous connective tissue, hyalinized stroma, or collagen.
   - "inflammation": Dense lymphocytic or histiocytic infiltrates without prominent malignant epithelium.
   - "adipose": Predominantly mature adipose tissue.
3. "rationale": A concise (1-2 sentences) morphological description explaining the verdict.

Respond with only a JSON object with exactly these keys and no other text:
{
  "tumor_present": <true | false>,
  "lesion_type": <"invasive_carcinoma" | "in_situ" | "benign_stroma" | "inflammation" | "adipose">,
  "rationale": "<brief rationale>"
}
