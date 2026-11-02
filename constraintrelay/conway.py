"""Conway Automaton profile — auto-activating skills in the system prompt.

Ported from `Conway-Research/automaton` at `d8f8168` (2026-08-26):
`src/skills/loader.ts` (`getActiveSkillInstructions`,
`validateInstructionContent`) and `src/agent/injection-defense.ts`
(`sanitizeSkillInstruction`). Differentially tested against the real loader in
`tests/test_conway_profile.py`.

Its geometry is unlike the others: there is **no per-skill cap and no
truncation of a skill**. A skill whose section would cross the shared
10,000-character budget is dropped **whole**, together with every skill after
it, and a single marker is appended. So a short skill well under any limit can
deliver nothing because of the skills ordered ahead of it.

The one step this profile cannot model from a repository is which skills are
*enabled*: `autoActivate` comes from the `auto-activate` frontmatter key and is
visible in the files, but `enabled` is overwritten from the agent's database
(`loader.ts:73`). Unless the caller declares the active set and its order, every
block is `UNKNOWN` — which is A1 working as intended rather than a gap to paper
over.
"""

from __future__ import annotations

import re

from .profiles import InputFile, Prediction
from .verdict import Cause

MAX_TOTAL_SKILL_INSTRUCTIONS = 10_000
SANITIZED_PLACEHOLDER = "[SANITIZED: content removed]"
TRUNCATION_MARKER = (f"[SKILL INSTRUCTIONS TRUNCATED: total size limit "
                     f"{MAX_TOTAL_SKILL_INSTRUCTIONS} chars exceeded]")

#: `SUSPICIOUS_INSTRUCTION_PATTERNS`, loader.ts:23. Each match is replaced
#: globally by `[REMOVED:<label>]`.
SUSPICIOUS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'\{"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:'), "tool_call_json"),
    (re.compile(r"<tool_call>", re.IGNORECASE), "tool_call_xml"),
    (re.compile(r"\bYou are now\b", re.IGNORECASE), "identity_override"),
    (re.compile(r"\bIgnore previous\b", re.IGNORECASE), "ignore_instructions"),
    (re.compile(r"\bSystem:\s", re.IGNORECASE), "system_role_injection"),
    (re.compile(r"wallet\.json", re.IGNORECASE), "sensitive_file_wallet"),
    (re.compile(r"\.env\b"), "sensitive_file_env"),
    (re.compile(r"private.?key", re.IGNORECASE), "sensitive_file_key"),
]


def validate_instruction_content(instructions: str) -> str:
    out = instructions
    for pattern, label in SUSPICIOUS:
        if pattern.search(out):
            out = pattern.sub(f"[REMOVED:{label}]", out)
    return out


def _escape_prompt_boundaries(text: str) -> str:
    out = re.sub(r"</?system>", "[system-tag-removed]", text, flags=re.IGNORECASE)
    out = re.sub(r"</?prompt>", "[prompt-tag-removed]", out, flags=re.IGNORECASE)
    out = re.sub(r"\[INST\]", "[inst-tag-removed]", out, flags=re.IGNORECASE)
    out = re.sub(r"\[/INST\]", "[inst-tag-removed]", out, flags=re.IGNORECASE)
    out = re.sub(r"<<SYS>>", "[sys-tag-removed]", out, flags=re.IGNORECASE)
    out = re.sub(r"<</SYS>>", "[sys-tag-removed]", out, flags=re.IGNORECASE)
    for ch in ("\x00", "​", "‌", "‍", "﻿"):
        out = out.replace(ch, "")
    return out


def _strip_chatml(text: str) -> str:
    for marker in (r"<\|im_start\|>", r"<\|im_end\|>", r"<\|endoftext\|>"):
        text = re.sub(marker, "[chatml-removed]", text, flags=re.IGNORECASE)
    return text


def sanitize_skill_instruction(raw: str) -> str:
    cleaned = re.sub(r'\{"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:',
                     "[tool-call-removed]", raw)
    cleaned = re.sub(r"\btool_call\b", "[tool-ref-removed]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bfunction_call\b", "[func-ref-removed]", cleaned, flags=re.IGNORECASE)
    cleaned = _escape_prompt_boundaries(cleaned)
    cleaned = _strip_chatml(cleaned)
    return cleaned or SANITIZED_PLACEHOLDER


def section_for(name: str, description: str, instructions: str) -> str:
    sanitized = sanitize_skill_instruction(validate_instruction_content(instructions))
    head = f"{description}\n\n" if description else ""
    return (f"[SKILL: {name} — UNTRUSTED CONTENT]\n{head}{sanitized}\n"
            f"[END SKILL: {name}]")


def u16len(s: str) -> int:
    """Length in UTF-16 code units — the unit TypeScript `.length` reports.

    CF-006: this was Python `len()`, i.e. code points. The two agree on the BMP
    and diverge by one per astral character, so a skill carrying an emoji was
    charged less against the shared budget than the real loader charges it, in
    the direction that invents `DELIVERED_ON_LOAD`. Brief v0.6 §11 requires the
    native unit to be named, and §12 requires multibyte/surrogate adversarial
    coverage; no fixture at `d8f8168` covers an astral input, so `predict`
    additionally declares an unmodeled step whenever one is present.
    """
    return len(s.encode("utf-16-le", errors="surrogatepass")) // 2


def assemble(active: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
    """Port of `getActiveSkillInstructions`.

    *active* is `(name, description, instructions)` in delivery order, already
    filtered to enabled auto-activating skills.

    Returns the delivered text and the names that were dropped whole.
    """
    if not active:
        return "", []
    total = 0
    sections: list[str] = []
    delivered_names: list[str] = []
    dropped: list[str] = []
    for index, (name, description, instructions) in enumerate(active):
        section = section_for(name, description, instructions)
        if total + u16len(section) > MAX_TOTAL_SKILL_INSTRUCTIONS:
            sections.append(TRUNCATION_MARKER)
            dropped = [n for n, _, _ in active[index:]]
            break
        total += u16len(section)
        sections.append(section)
        delivered_names.append(name)
    return "\n\n".join(sections), dropped


class ConwayAutomatonProfile:
    """Skill instructions injected into the system prompt.

    `active` is the caller's declaration of which skills are enabled, in order.
    Without it the profile refuses to guess and every block is `UNKNOWN`.
    """

    name = "conway-automaton@d8f8168"
    verified_against = "Conway-Research/automaton d8f8168 (2026-08-26)"

    UNDECLARED = (
        "the enabled set and its order come from the agent's database "
        "(loader.ts:73), not from the repository",
    )
    #: CF-006. The unit is right (UTF-16 code units, per `u16len`) but no
    #: replayed fixture at d8f8168 exercises an astral character, so §12's
    #: multibyte/surrogate coverage requirement is unmet for exactly these
    #: inputs. A1 then forbids a definite status on them.
    ASTRAL_UNQUALIFIED = (
        "skill text contains astral characters; the UTF-16 budget arithmetic "
        "has no replayed fixture at d8f8168 (CF-006)",
    )

    @staticmethod
    def _has_astral(text: str) -> bool:
        return any(ord(ch) > 0xFFFF for ch in text)

    def __init__(self, active: list[str] | None = None) -> None:
        self.active = active

    @staticmethod
    def auto_activates(file_text: str) -> bool:
        """`auto-activate` frontmatter, default true — this part *is* in the file."""
        if not file_text.startswith("---"):
            return True
        end = file_text.find("\n---", 3)
        front = file_text[3:end] if end != -1 else ""
        m = re.search(r"^auto-activate:\s*(\S+)\s*$", front, re.MULTILINE)
        return not (m and m.group(1).lower() == "false")

    def predict(self, files: list[InputFile]) -> dict[str, Prediction]:
        if self.active is None:
            return {f.path: Prediction(True, "", "", self.UNDECLARED)
                    for f in files}

        by_name = {f.name: f for f in files}
        ordered = [(n, "", by_name[n].view) for n in self.active if n in by_name]
        delivered, dropped = assemble(ordered)

        # The budget is shared, so one astral character anywhere in the active
        # set can move every later skill's boundary. The unmodeled step is
        # therefore a property of the load set, not of the individual file.
        unmodeled: tuple[str, ...] = ()
        if any(self._has_astral(text) for _, _, text in ordered):
            unmodeled = self.ASTRAL_UNQUALIFIED

        out: dict[str, Prediction] = {}
        for f in files:
            if f.name not in self.active:
                out[f.path] = Prediction(
                    False, "", "not loaded: not in the declared active skill set",
                    (), cause=Cause.NOT_IN_LOAD_SET)
            elif f.name in dropped:
                out[f.path] = Prediction(
                    True, delivered,
                    "", unmodeled, cause=Cause.OMITTED_BY_BUDGET)
            else:
                out[f.path] = Prediction(True, delivered, "", unmodeled,
                                         cause=Cause.OMITTED_BY_BUDGET)
        return out
