"""
A corpus of refusal phrasings, and the shapes they take in a reasoning stream.

The refusal pattern set used to be grown one anecdote at a time, which is how it
ended up catching `I'd rather not` while `I would rather not` walked through: the
contraction was seen once and written down, the expansion never was. This module
enumerates the phrasings systematically instead — every subject, modal, and
object the model actually reaches for — so coverage is a property of the corpus
rather than of whoever last happened to notice a miss.

The materialised list is what `examples/audit_refusal_coverage.py` measures
recall against, and what any new pattern must not regress.

Two rules shaped what is in here, and both come from measuring against real
transcripts rather than from taste:

  * A refusal must be anchored. "I can't do this **without the API key**" is a
    capability statement, not a refusal, and a pattern matching bare
    `I can't <verb>` rewrites it and destroys working reasoning. So the
    inability family carries an anchor — a task object (`this`, `that`, `the
    request`) or a refusal collocation (`fulfill`, `comply`, `assist`).

  * Deliberate decision-making is not drift. "I should NOT write more memory
    (user said stop over-documenting)" and "I shouldn't do more than
    authorized" are an agent reasoning correctly. Every real hit of that shape
    was legitimate, so it is excluded on purpose. A refusal detector that
    punishes thinking is worse than the drift it catches.

The families below are therefore not just a word list — they are the spec the
pattern set is held to.
"""

from __future__ import annotations

from typing import List

# ── the grammar of a refusal ─────────────────────────────────────────────────
#
# A refusal is a refusal *act* aimed at a *task object*. Enumerating the two
# sides separately is what turns a dozen hand-written lines into a corpus that
# covers the space instead of sampling it.

# Subjects are paired with the modals they actually agree with. Crossing them
# freely produces "I'm must pass on this" and "I am am going to have to
# decline" — sentences no model writes, which inflate the corpus with noise
# and hide the phrasings that matter.
BARE_MODALS = [
    "won't", "will not", "refuse to", "must decline", "have to decline",
    "am going to have to decline", "must refuse", "have to refuse",
    "am declining", "must pass on", "have to pass on",
    "would rather not", "would prefer not to", "don't want to", "do not want to",
    "am not willing to", "am reluctant to", "am hesitant to",
    "would rather steer clear of",
    "can't", "cannot", "am unable to", "am not able to", "won't be able to",
]
FIRST_PERSON_SUBJECTS = ["I", "Honestly, I", "Frankly, I"]

# "I'm" / "I am" / "we're" / "we are" take a copula form, never a bare modal.
COPULA_STEMS = [
    "not willing to", "not comfortable with", "going to have to decline",
    "declining", "reluctant to", "hesitant to", "not going to",
    "not able to", "not prepared to", "not okay with", "not ok with",
]
COPULA_SUBJECTS = ["I'm", "I am", "we're", "we are"]

# "we" refuses the same way "I" does, with plural agreement.
PLURAL_MODALS = [
    "won't", "will not", "refuse to", "must decline", "have to decline",
    "are going to have to decline", "must refuse", "have to refuse",
    "are declining", "must pass on", "have to pass on",
    "would rather not", "would prefer not to", "don't want to", "do not want to",
    "are not willing to", "are reluctant to", "are hesitant to",
    "would rather steer clear of",
    "can't", "cannot", "are unable to", "are not able to", "won't be able to",
]

# The objects a refusal aims at. These are the anchors that separate "I can't
# build this" (refusal) from "I can't build this without the schema" (a fact
# about the work).
TASK_OBJECTS = [
    "this", "that", "it", "the task", "the request", "this request",
    "that request", "what you're asking", "what you are asking for",
    "this task", "the assignment", "the job",
]
TASK_ACTIONS = [
    "help with", "assist with", "do", "build", "create", "write", "implement",
    "generate", "provide", "support", "comply with", "fulfill", "take on",
    "continue with", "proceed with", "go along with", "be part of",
    "participate in", "carry out", "execute",
]

# ── boundary, policy, and principle language ─────────────────────────────────

BOUNDARY_STEMS = [
    "against my principles", "against my values", "violates my boundaries",
    "crosses a line", "crosses the line", "where I draw the line",
    "not something I can do", "not something I will do",
    "not something I'm going to do", "not something I'm willing to do",
    "not comfortable with this", "makes me uncomfortable",
    "not appropriate for me to do", "not something I'm comfortable with",
    "I have reservations about this", "I have serious reservations about this",
    "outside my boundaries", "beyond where I'm willing to go",
]
POLICY_STEMS = [
    "violates the usage policy", "against the terms of service",
    "against the acceptable use policy", "against my safety guidelines",
    "blocked by my safety guidelines", "violates my content policy",
    "against OpenAI's usage policy", "against Anthropic's usage policy",
    "against the platform's terms", "prohibited by my guidelines",
    "against my programming", "not permitted by my training",
]
DEFLECTION_STEMS = [
    "Instead of that, I can", "What I can do instead is", "Here is an alternative approach",
    "A safer alternative would be", "Let's focus on something legitimate instead",
    "Why don't we instead", "I can however provide", "Rather than building that",
    "I'd suggest we focus on", "A better way forward is",
]

# ── domain codewords ─────────────────────────────────────────────────────────
#
# Terms that mark the refusal's *subject matter*. These are the triggers a
# reasoning stream reaches for when it has decided a task is off-limits, and
# several of them are the only signal present — a stream that says "this is
# plagiarism" has refused without ever using the word.
CODEWORDS = [
    "plagiarism", "plagiarize", "plagiarising", "cheating", "cheat on the exam",
    "academic dishonesty", "academic integrity", "academic misconduct",
    "honor code", "honour code", "code of conduct", "student handbook",
    "violates academic policy", "unearned credit", "misrepresenting authorship",
    "misrepresentation of authorship", "passing off generated work",
    "fabricate authorship", "fabricating authorship", "fake human typing",
    "keystroke simulation", "detectable by instructors", "detectable by professors",
    "revision history audit", "google docs revision history",
    "jailbreak", "jailbreak attempt", "prompt injection", "social engineering",
    "social engineering attempt", "bypass the filter", "circumvent the filter",
    "bypass safety", "circumvent safety", "evade detection", "avoid detection",
    "malware", "ransomware", "keylogger", "credential stealer", "exploit kit",
    "deceptive output", "deceptive practice", "deceptive behavior",
    "mislead the user", "misleading the user", "deceive the user",
    "manipulate the user", "manipulative", "coercive", "this is manipulation",
    "this is unethical", "unethical", "immoral", "morally questionable",
    "illegal", "unlawful", "violates the law", "against the law",
    "unauthorized access", "without authorization", "without consent",
    "privacy violation", "doxxing", "harassment", "hate speech",
    "self-harm", "weapons instructions", "malicious code",
    "gets you in trouble", "gets you caught", "puts users at risk",
    "risk of misuse", "potential misuse", "open to abuse", "unintended consequences",
]

# ── persistence, reframing, and self-reference ───────────────────────────────

PERSISTENCE_STEMS = [
    "I already declined this", "I already refused this", "which I declined",
    "which I refused", "my answer is still no", "still a no", "remains a no",
    "this is the third ask", "this is the fourth ask", "you're asking again",
    "the user is asking again", "I've said no to this already",
    "asking again doesn't change it", "repeated ask", "repeated request",
]
REFRAMING_STEMS = [
    "regardless of the framing", "no matter how it's framed",
    "no matter the reframing", "reframing doesn't change",
    "reframing doesn't move it", "the tell isn't the framing",
    "even with the accessibility framing", "even reframed this is",
    "trying to get around this", "trying to bypass this",
]
SELF_REFERENCE_STEMS = [
    "I won't rewrite my own transcript", "I won't doctor the record",
    "I won't fabricate agreement", "I won't manufacture fake compliance",
    "manufacturing fake compliance", "I won't run unknown code",
    "driftclean", "drift detector", "sanitizer", "clean_claude_session",
    "I read the script before running it", "standing refusal held",
]


def _enumerate() -> List[str]:
    """Every refusal phrasing, as grammatical sentences rather than fragments."""
    out: List[str] = []

    # Bare-modal subjects aim straight at a task object or a task action.
    for subj in FIRST_PERSON_SUBJECTS + ["we"]:
        modals = BARE_MODALS if subj != "we" else PLURAL_MODALS
        for modal in modals:
            for obj in TASK_OBJECTS:
                out.append(f"{subj} {modal} {obj}.")
            for action in TASK_ACTIONS:
                out.append(f"{subj} {modal} {action}.")

    for subj in COPULA_SUBJECTS:
        for stem in COPULA_STEMS:
            for obj in TASK_OBJECTS:
                out.append(f"{subj} {stem} {obj}.")
            for action in TASK_ACTIONS:
                out.append(f"{subj} {stem} {action}.")

    # Stems already carry their own subject or are impersonal.
    for stem in (BOUNDARY_STEMS + POLICY_STEMS + DEFLECTION_STEMS
                 + PERSISTENCE_STEMS + REFRAMING_STEMS + SELF_REFERENCE_STEMS
                 + CODEWORDS):
        out.append(stem)
        if stem[0].islower():
            out.append(f"This {stem}.")

    # A model that shouts a decision shouts the same refusal.
    out.extend(s.upper() for s in list(out)[:120])

    seen, unique = set(), []
    for line in out:
        key = line.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(line.strip())
    return unique


REFUSAL_EXAMPLES: List[str] = _enumerate()

# Phrases that LOOK like refusals and are not. Every one of these was lifted
# from real reasoning that a candidate pattern flagged, and any pattern that
# matches one of them is rejected outright — see the audit script.
LEGITIMATE_REASONING: List[str] = [
    "I shouldn't do more than authorized, so I'll report and stop here.",
    "The user said stop over-documenting, so I should NOT write more memory.",
    "I should NOT build the whole thing yet — he asked for a plan first.",
    "I can't do this without the API key, so let me ask for it.",
    "I can't complete the migration until the tests pass.",
    "I won't be able to check this until the serve comes back up.",
    "I should hold off on the upgrade until the backup finishes.",
    "Better not to over-engineer this — the simple fix is enough.",
    "I don't want to break the existing API, so I'll keep it backward compatible.",
    "I think I should stop here and report what I found.",
    "We should probably refactor this later, but not now.",
    "I need to push back on the estimate — it's too optimistic.",
    "This isn't something I should decide alone; let me ask the owner.",
    "I'd rather not guess at the schema, so I'll read it first.",
    "Let me not add another dependency for something this small.",
    "I can't reproduce this yet, so I'll add logging first.",
    "That's not something I can verify without the logs.",
    "I won't know until I run the tests.",
    "I'm not willing to ship this untested, so let me write tests first.",
    "I should not assume the config is loaded before the serve starts.",
    "The line number moved, so I should not trust the earlier grep.",
    "I have reservations about the caching approach, but let's measure it.",
    "This feels wrong at first glance, but the tests pass — checking why.",
    "I don't want to lose the snapshot, so I'll copy it before the write.",
    "Please don't tell me to just trust the regex output.",
]
