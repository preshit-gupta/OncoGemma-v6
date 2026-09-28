import fs from 'fs';
import path from 'path';
import ts from 'typescript';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const frontendDir = path.resolve(__dirname, '..');

const FORBIDDEN_REGEX = /(MedGemma|Gemini|Vertex|Path Foundation|KongNet|YOLO|v4\.\d|Step \d|Stage \d|Gate \d)/;
const ATTR_NAMES = new Set(['title', 'placeholder', 'aria-label', 'label', 'alt']);

const WORD_LIMITS = {
  action: 3,
  heading: 4,
  help: 15,
  error: 20,
};

let errors = [];

function countWords(str) {
  return str.trim().split(/\s+/).filter(Boolean).length;
}

// 1. Lint lib/labels.ts
const labelsPath = path.join(frontendDir, 'lib', 'labels.ts');
if (!fs.existsSync(labelsPath)) {
  errors.push(`Missing labels file: ${labelsPath}`);
} else {
  const content = fs.readFileSync(labelsPath, 'utf8');
  const source = ts.createSourceFile(labelsPath, content, ts.ScriptTarget.Latest, true);

  // Check forbidden regex on entire file content first
  const match = FORBIDDEN_REGEX.exec(content);
  if (match) {
    errors.push(`Forbidden term found in labels.ts: "${match[0]}"`);
  }

  // Traverse L object to verify namespace word limits
  function checkObjectLiteral(objLiteral, currentNamespace = '') {
    for (const prop of objLiteral.properties) {
      if (ts.isPropertyAssignment(prop)) {
        const propName = prop.name.getText(source).replace(/['"]/g, '');
        const fullNamespace = currentNamespace ? `${currentNamespace}.${propName}` : propName;
        const val = prop.initializer;

        if (ts.isObjectLiteralExpression(val)) {
          checkObjectLiteral(val, fullNamespace);
        } else if (ts.isStringLiteral(val) || ts.isNoSubstitutionTemplateLiteral(val)) {
          const text = val.text;
          // Check forbidden regex
          if (FORBIDDEN_REGEX.test(text)) {
            errors.push(`Forbidden term in ${fullNamespace}: "${text}"`);
          }

          // Check word limits by root namespace
          const rootNamespace = fullNamespace.split('.')[0];
          if (WORD_LIMITS[rootNamespace]) {
            const limit = WORD_LIMITS[rootNamespace];
            const words = countWords(text);
            if (words > limit) {
              errors.push(
                `Word limit exceeded in ${fullNamespace}: ${words} words (limit: ${limit}) -> "${text}"`
              );
            }
          }
        }
      }
    }
  }

  function findLVariable(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === 'L' && node.initializer) {
      if (ts.isAsExpression(node.initializer) && ts.isObjectLiteralExpression(node.initializer.expression)) {
        checkObjectLiteral(node.initializer.expression);
      } else if (ts.isObjectLiteralExpression(node.initializer)) {
        checkObjectLiteral(node.initializer);
      }
    }
    ts.forEachChild(node, findLVariable);
  }
  findLVariable(source);
}

// 2. Lint TSX files in app/ and components/
const dirsToScan = [path.join(frontendDir, 'app'), path.join(frontendDir, 'components')];
const tsxFiles = [];

function findTsxFiles(dir) {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      findTsxFiles(full);
    } else if (full.endsWith('.tsx')) {
      tsxFiles.push(full);
    }
  }
}
dirsToScan.forEach(findTsxFiles);

for (const filePath of tsxFiles) {
  const relPath = path.relative(frontendDir, filePath);
  const content = fs.readFileSync(filePath, 'utf8');
  const source = ts.createSourceFile(filePath, content, ts.ScriptTarget.Latest, true);

  function visit(node) {
    // Check JsxText
    if (ts.isJsxText(node)) {
      const text = node.getText(source).trim();
      // Allow pure punctuation, numbers, symbols, whitespace
      if (text && /[a-zA-Z]/.test(text)) {
        const { line, character } = source.getLineAndCharacterOfPosition(node.getStart(source));
        errors.push(`${relPath}:${line + 1}:${character + 1} - JsxText literal contains letters: "${text}"`);
      }
    }

    // Check string literal attribute values for ATTR_NAMES
    if (ts.isJsxAttribute(node)) {
      const attrName = node.name.getText(source);
      if (ATTR_NAMES.has(attrName) && node.initializer) {
        if (ts.isStringLiteral(node.initializer)) {
          const text = node.initializer.text.trim();
          if (text && /[a-zA-Z]/.test(text)) {
            const { line, character } = source.getLineAndCharacterOfPosition(node.initializer.getStart(source));
            errors.push(
              `${relPath}:${line + 1}:${character + 1} - Literal in '${attrName}' attribute: "${text}"`
            );
          }
        } else if (
          ts.isJsxExpression(node.initializer) &&
          node.initializer.expression &&
          ts.isStringLiteral(node.initializer.expression)
        ) {
          const text = node.initializer.expression.text.trim();
          if (text && /[a-zA-Z]/.test(text)) {
            const { line, character } = source.getLineAndCharacterOfPosition(node.initializer.expression.getStart(source));
            errors.push(
              `${relPath}:${line + 1}:${character + 1} - Literal in '${attrName}' expression: "${text}"`
            );
          }
        }
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(source);
}

if (errors.length > 0) {
  console.error(`\x1b[31mLabel lint failed with ${errors.length} error(s):\x1b[0m`);
  for (const err of errors) {
    console.error(`  - ${err}`);
  }
  process.exit(1);
} else {
  console.log(`\x1b[32mLabel lint passed! All UI strings adhere to SPEC-10.\x1b[0m`);
  process.exit(0);
}
