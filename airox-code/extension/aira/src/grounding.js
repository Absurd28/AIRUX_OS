"use strict";
// AIRA's grounding rules in AIROX Code, applied to every chat answer in code (not in the prompt):
//  - every [path:line] citation must point at a file and line range that was in the CONTEXT;
//  - an answer that cites nothing is labelled "unverified" (unless it says "I'm not sure");
//  - edits are never applied without the user's accept, and are read back after applying.

const CITE_RE = /\[([^\]\s:]+(?:\/[^\]\s:]+)*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?\]/g;

function citations(text) {
  const out = [];
  let m;
  CITE_RE.lastIndex = 0;
  while ((m = CITE_RE.exec(text))) {
    out.push({ raw: m[0], path: m[1], start: +m[2], end: m[3] ? +m[3] : +m[2] });
  }
  return out;
}

// files: [{path, text, firstLine}]
function checkAnswer(text, files) {
  const byPath = new Map(files.map((f) => {
    const first = f.firstLine || 1;
    return [f.path, { first, last: first + f.text.split("\n").length - 1 }];
  }));
  const found = citations(text);
  const bad = [];
  const good = [];
  for (const c of found) {
    const f = byPath.get(c.path) || [...byPath.entries()].find(([p]) => p.endsWith("/" + c.path))?.[1];
    if (!f || c.start < f.first || c.end > f.last || c.end < c.start) bad.push(c);
    else good.push(c);
  }
  const notSure = /\bI['’]m not sure\b|\bI couldn['’]t verify\b/i.test(text);
  let status = "verified";
  if (bad.length) status = "unverified";
  else if (!good.length && !notSure) status = files.length ? "unverified" : "general";
  if (notSure && !good.length) status = "not sure";
  return { status, good, bad };
}

module.exports = { citations, checkAnswer };
