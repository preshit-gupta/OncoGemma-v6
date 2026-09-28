/**
 * Colour-blind-safe palette for scientific data visualization in Research view.
 * Based on the Okabe-Ito (2008) palette, universally accessible to deuteranopia,
 * protanopia, tritanopia, and monochromatic vision.
 */
export const PALETTE = {
  primary: "#0072B2", // Blue
  secondary: "#D55E00", // Vermilion
  accent1: "#009E73", // Bluish Green
  accent2: "#E69F00", // Orange
  accent3: "#56B4E9", // Sky Blue
  accent4: "#CC79A7", // Reddish Purple
  neutral: "#718096", // Grey
  highlight: "#F0E442", // Yellow
  grid: "#E2E8F0", // Slate-200
  dark: "#1A202C", // Slate-900
} as const;

export const COLOR_BLIND_SERIES = [
  PALETTE.primary,
  PALETTE.secondary,
  PALETTE.accent1,
  PALETTE.accent2,
  PALETTE.accent3,
  PALETTE.accent4,
  PALETTE.neutral,
];
