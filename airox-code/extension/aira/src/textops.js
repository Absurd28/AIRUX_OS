"use strict";
// Text handling for edits, inline suggestions and commit messages. Pure functions, unit-tested.

// The code inside the model's single fenced block (or the whole answer if there is no fence).
function extractCode(answer) {
  const m = answer.match(/```[^\n`]*\n([\s\S]*?)\n?```/);
  if (m) return m[1];
  if (/```/.test(answer)) return null;            // an unclosed fence: the answer was cut off
  return answer.trim() ? answer.replace(/\s+$/, "") : null;
}

// Keep the original's trailing newline and leading indentation conventions.
function matchEnds(original, proposed) {
  let out = proposed;
  if (original.endsWith("\n") && !out.endsWith("\n")) out += "\n";
  if (!original.endsWith("\n")) out = out.replace(/\n+$/, "");
  return out;
}

// Clean a fill-in-the-middle completion before showing it as ghost text.
//  prefixLine: text of the current line before the cursor; suffix: text after the cursor
function cleanInline(completion, { prefixLine = "", suffix = "", maxLines = 8 } = {}) {
  if (!completion) return "";
  let c = completion.replace(/\r/g, "");
  // in the middle of a line, only complete that line
  const restOfLine = suffix.split("\n", 1)[0];
  if (restOfLine.trim()) c = c.split("\n", 1)[0];
  // do not repeat what already follows the cursor
  const next = suffix.replace(/^\s+/, "").slice(0, 40);
  if (next) {
    const at = c.indexOf(next.split("\n", 1)[0]);
    if (at > 0 && next.split("\n", 1)[0].length > 3) c = c.slice(0, at);
  }
  const lines = c.split("\n").slice(0, maxLines);
  c = lines.join("\n").replace(/\s+$/, "");
  if (!c.trim()) return "";
  // a completion that only re-types the current line adds nothing
  if (prefixLine.trim() && c.trim() === prefixLine.trim()) return "";
  return c;
}

function cleanCommitMessage(text) {
  let t = text.replace(/^```[^\n]*\n?|\n?```$/g, "").trim();
  t = t.replace(/^["'](.*)["']$/s, "$1").trim();
  const lines = t.split("\n");
  if (lines[0].length > 72) lines[0] = lines[0].slice(0, 72).replace(/\s+\S*$/, "");
  if (lines.length > 1 && lines[1].trim() !== "") lines.splice(1, 0, "");   // git wants a blank line after the summary
  return lines.join("\n").replace(/\n{3,}/g, "\n\n");
}

module.exports = { extractCode, matchEnds, cleanInline, cleanCommitMessage };
