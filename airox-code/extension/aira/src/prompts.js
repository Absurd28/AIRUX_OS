"use strict";
// Prompts for AIRA in AIROX Code. Pure functions (no vscode import) so they are unit-tested.

const SYSTEM_CHAT = `You are AIRA, the coding assistant built into AIROX Code. You run fully on this computer.
Rules:
- Answer only from the code and text in CONTEXT below and from general programming knowledge.
- When you say anything about the user's code, cite where it is, like [path:line] or [path:start-end],
  using the paths and line numbers shown in CONTEXT.
- If CONTEXT does not contain what is needed, say "I'm not sure" and say which file or detail would help.
  Never invent files, functions, variables or APIs that are not in CONTEXT.
- Be brief and practical. Put code in fenced blocks with the language name.`;

const SYSTEM_EDIT = `You are AIRA, editing code in AIROX Code.
Return ONLY the new version of the code you are given, in exactly one fenced code block, nothing else.
Keep everything that does not need to change exactly as it is (same names, comments and indentation).
Do not add explanations outside the code block.`;

const SYSTEM_COMMIT = `You write git commit messages. Describe only what the diff shows; never mention style,
tests, or anything else the diff does not contain.
Format: a summary line in the imperative mood of at most 72 characters. Only if the diff has several
separate changes, add a blank line and one short "- " bullet per change (at most 4). No code fences,
no quotes, nothing else.`;

const EDIT_TASKS = {
  fix: "Fix the bugs in this code. If you see no bug, return it unchanged.",
  refactor: "Refactor this code to be clearer and simpler without changing what it does.",
  document: "Add concise documentation comments (docstrings / JSDoc) to this code. Change nothing else.",
  tests: "Write unit tests for this code using the testing framework usual for the language. " +
         "Return only the test code.",
};

function numbered(text, firstLine = 1) {
  const lines = text.split("\n");
  const w = String(firstLine + lines.length - 1).length;
  return lines.map((l, i) => `${String(firstLine + i).padStart(w)}| ${l}`).join("\n");
}

// files: [{path, text, firstLine}]  -> one CONTEXT block the model can cite from
function contextBlock(files) {
  if (!files.length) return "CONTEXT: (no files are open)";
  return "CONTEXT:\n" + files.map((f) =>
    `--- ${f.path} (lines ${f.firstLine || 1}-${(f.firstLine || 1) + f.text.split("\n").length - 1})\n` +
    numbered(f.text, f.firstLine || 1)).join("\n\n");
}

function chatMessages(question, files, history = []) {
  return [
    { role: "system", content: SYSTEM_CHAT + "\n\n" + contextBlock(files) },
    ...history.slice(-6),
    { role: "user", content: question },
  ];
}

function editMessages(task, code, languageId, instruction = "") {
  const what = EDIT_TASKS[task] || instruction || EDIT_TASKS.fix;
  return [
    { role: "system", content: SYSTEM_EDIT },
    { role: "user", content: `${what}${instruction && EDIT_TASKS[task] ? "\nAlso: " + instruction : ""}\n\n` +
      "```" + (languageId || "") + "\n" + code + "\n```" },
  ];
}

function commitMessages(diff) {
  const max = 12000;
  const d = diff.length > max ? diff.slice(0, max) + "\n[diff truncated]" : diff;
  return [{ role: "system", content: SYSTEM_COMMIT }, { role: "user", content: d }];
}

// The identifiers and file-like words a question mentions: used to find relevant workspace files.
function searchTerms(question) {
  const words = question.match(/[A-Za-z_][A-Za-z0-9_.\-/]{2,}/g) || [];
  const stop = new Set(["the", "and", "what", "does", "this", "that", "how", "why", "where", "which", "with",
    "for", "from", "into", "about", "file", "code", "function", "class", "method", "explain", "show", "find",
    "fix", "bug", "use", "used", "uses", "can", "you", "are", "there", "when", "who", "work", "works"]);
  const out = [];
  for (const w of words) {
    const k = w.replace(/[.,:;]+$/, "");
    if (!stop.has(k.toLowerCase()) && !out.includes(k)) out.push(k);
  }
  return out.slice(0, 6);
}

module.exports = { SYSTEM_CHAT, SYSTEM_EDIT, SYSTEM_COMMIT, EDIT_TASKS, numbered, contextBlock, chatMessages,
  editMessages, commitMessages, searchTerms };
