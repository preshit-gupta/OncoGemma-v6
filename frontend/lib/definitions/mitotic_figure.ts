/**
 * Operational Definition of a Mitotic Figure (SPEC-06 §3, van Diest-style criteria)
 */
export const MITOTIC_FIGURE_DEFINITION = `Operational Definition (van Diest-style criteria):

Count a cell as a mitotic figure when ALL of the following hold:
1. The nuclear membrane is absent (the cell is beyond prophase).
2. Condensed chromosomes are visible as dark, hairy or spiky projections, in one of these arrangements:
   - clotted (early metaphase / prometaphase)
   - a plate or ring (metaphase)
   - two separating groups (anaphase)
   - two separate clots at opposite poles (telophase)
3. The cell is a neoplastic (invasive carcinoma) cell.

Atypical mitoses (multipolar, lagging chromosomes, asymmetric) are counted.

Do NOT count:
- prophase or any figure with a visible nuclear membrane
- hyperchromatic interphase nuclei (intact smooth contour)
- pyknotic nuclei (small, homogeneous, round, smooth, no projections)
- apoptotic bodies (dense round fragments, eosinophilic cytoplasm, clear halo)
- lymphocytes and plasma cells
- crushed or smeared nuclei
- mitoses in non-neoplastic cells (endothelium, stroma, inflammatory cells, normal epithelium, in-situ component)

Dividing-cell counting rule:
One dividing cell counts as one mitosis. Both chromosome groups of anaphase or telophase belong to the same figure and are counted once.`;
