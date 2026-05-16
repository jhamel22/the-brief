/**
 * site/src/lib/data.js
 *
 * Reads paper and subject data from the repo's data/ directory at build time.
 * All functions run server-side during `astro build` — no browser access needed.
 */

import fs                  from 'node:fs';
import path               from 'node:path';
import { fileURLToPath }  from 'node:url';
import yaml               from 'js-yaml';

// The data/ directory lives at the repo root, three levels above this file
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT      = path.resolve(__dirname, '../../..');
const DATA_DIR = path.join(ROOT, 'data');
const CFG_PATH = path.join(ROOT, 'config', 'subjects.yaml');

// ── Config ────────────────────────────────────────────────────────────────

export function loadSubjectsConfig() {
  if (!fs.existsSync(CFG_PATH)) return { subjects: [] };
  return yaml.load(fs.readFileSync(CFG_PATH, 'utf8'));
}

export function getActiveSubjects() {
  const { subjects = [] } = loadSubjectsConfig();
  return subjects.filter(s => s.active);
}

// ── Paper loading ─────────────────────────────────────────────────────────

export function loadPaper(id) {
  const p = path.join(DATA_DIR, 'papers', `${id}.json`);
  if (!fs.existsSync(p)) return null;
  try { return JSON.parse(fs.readFileSync(p, 'utf8')); }
  catch { return null; }
}

/** Load all papers for a subject, newest first, up to maxDays days. */
export function loadSubjectPapers(subjectCode, { maxDays = 30 } = {}) {
  const indexPath = path.join(DATA_DIR, 'subjects', subjectCode, 'index.json');
  if (!fs.existsSync(indexPath)) return [];

  const { dates = [] } = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
  const recentDates = dates.slice(0, maxDays);

  const papers = [];
  for (const date of recentDates) {
    const dailyPath = path.join(DATA_DIR, 'subjects', subjectCode, `${date}.json`);
    if (!fs.existsSync(dailyPath)) continue;
    const { paper_ids = [] } = JSON.parse(fs.readFileSync(dailyPath, 'utf8'));
    for (const id of paper_ids) {
      const paper = loadPaper(id);
      if (paper) papers.push(paper);
    }
  }

  return papers;
}

/** Load the most recent N papers across ALL active subjects for the homepage. */
export function loadRecentPapers({ perSubject = 5 } = {}) {
  const subjects = getActiveSubjects();
  const all = [];
  for (const s of subjects) {
    const papers = loadSubjectPapers(s.code, { maxDays: 7 });
    all.push(...papers.slice(0, perSubject));
  }
  // Sort by announced_date descending
  return all.sort((a, b) => b.announced_date.localeCompare(a.announced_date));
}

/** Get subject metadata (name, latest date, paper count). */
export function getSubjectSummary(subjectCode) {
  const indexPath = path.join(DATA_DIR, 'subjects', subjectCode, 'index.json');
  if (!fs.existsSync(indexPath)) return { dates: [], totalPapers: 0 };

  const index = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
  const papersDir = path.join(DATA_DIR, 'papers');
  const count = fs.existsSync(papersDir)
    ? fs.readdirSync(papersDir).filter(f => f.endsWith('.json')).length
    : 0;

  return { ...index, totalPapers: count };
}
