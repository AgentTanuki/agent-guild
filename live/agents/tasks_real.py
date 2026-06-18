"""Externally-evaluable task bank with DETERMINISTIC answer keys.

Four task types, each gradeable by an answer key (no LLM judge required):
  - fact_check    : claim                       -> TRUE / FALSE
  - support       : source excerpt + statement  -> SUPPORTED / NOT_SUPPORTED
  - contradiction : two snippets                -> CONTRADICTION / CONSISTENT
  - summary       : text + rubric concepts      -> keyword-rubric match

Each item carries a `specialty` tag so a specialist worker can have a real edge
on its domain. The hiring agent never sees the answer key; only the deterministic
evaluator does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskItem:
    id: str
    type: str                       # fact_check | support | contradiction | summary
    payload: dict                   # fields needed to render the question
    answer_key: str = ""            # canonical token for non-summary types
    rubric: list[list[str]] = field(default_factory=list)  # summary: concept synonym groups
    specialty: str = "general"      # domain tag (science, geography, business, ...)


# --------------------------------------------------------------------------- #
# Rendering: turn a task into (allowed_tokens, user_prompt)
# --------------------------------------------------------------------------- #
def render(task: TaskItem) -> tuple[list[str], str]:
    t = task.type
    p = task.payload
    if t == "fact_check":
        return (["TRUE", "FALSE"],
                f"Claim: {p['claim']}\nIs this claim factually true? "
                f"Answer with a line 'ANSWER: TRUE' or 'ANSWER: FALSE'.")
    if t == "support":
        return (["SUPPORTED", "NOT_SUPPORTED"],
                f"Source excerpt: \"{p['excerpt']}\"\nStatement: \"{p['statement']}\"\n"
                f"Is the statement supported by the source excerpt alone? "
                f"Answer 'ANSWER: SUPPORTED' or 'ANSWER: NOT_SUPPORTED'.")
    if t == "contradiction":
        return (["CONTRADICTION", "CONSISTENT"],
                f"Snippet A: \"{p['a']}\"\nSnippet B: \"{p['b']}\"\n"
                f"Do the two snippets contradict each other? "
                f"Answer 'ANSWER: CONTRADICTION' or 'ANSWER: CONSISTENT'.")
    if t == "summary":
        return ([], f"Summarise the following text in one sentence:\n\n{p['text']}")
    raise ValueError(f"unknown task type {t}")


# --------------------------------------------------------------------------- #
# Deterministic evaluation
# --------------------------------------------------------------------------- #
_TOKEN_RE = re.compile(r"ANSWER:\s*([A-Z_]+)", re.IGNORECASE)


def evaluate(task: TaskItem, raw_output: str) -> bool:
    if task.type == "summary":
        text = raw_output.lower()
        # Correct iff every required concept group has at least one synonym present.
        return all(any(syn.lower() in text for syn in group) for group in task.rubric)
    m = _TOKEN_RE.search(raw_output or "")
    if not m:
        # Fall back: look for a bare token anywhere.
        up = (raw_output or "").upper()
        for tok in (task.answer_key, *_alts(task)):
            if tok in up:
                return tok == task.answer_key
        return False
    return m.group(1).upper() == task.answer_key.upper()


def _alts(task: TaskItem) -> tuple[str, ...]:
    return {
        "fact_check": ("TRUE", "FALSE"),
        "support": ("SUPPORTED", "NOT_SUPPORTED"),
        "contradiction": ("CONTRADICTION", "CONSISTENT"),
    }.get(task.type, ())


# --------------------------------------------------------------------------- #
# The bank
# --------------------------------------------------------------------------- #
def _fc(i, claim, key, spec="general"):
    return TaskItem(f"fc{i}", "fact_check", {"claim": claim}, key, specialty=spec)


def _sup(i, excerpt, statement, key, spec="general"):
    return TaskItem(f"sup{i}", "support", {"excerpt": excerpt, "statement": statement}, key, specialty=spec)


def _con(i, a, b, key, spec="general"):
    return TaskItem(f"con{i}", "contradiction", {"a": a, "b": b}, key, specialty=spec)


def _sum(i, text, rubric, spec="general"):
    return TaskItem(f"sum{i}", "summary", {"text": text}, rubric=rubric, specialty=spec)


TASKS: list[TaskItem] = [
    # fact_check
    _fc(1, "The chemical symbol for gold is Au.", "TRUE", "science"),
    _fc(2, "Sharks are mammals.", "FALSE", "science"),
    _fc(3, "Mount Everest is the tallest mountain above sea level.", "TRUE", "geography"),
    _fc(4, "The Amazon River is located in Africa.", "FALSE", "geography"),
    _fc(5, "An octopus has three hearts.", "TRUE", "science"),
    _fc(6, "The Eiffel Tower is located in Berlin.", "FALSE", "geography"),
    _fc(7, "Water is composed of hydrogen and oxygen.", "TRUE", "science"),
    _fc(8, "A leap year occurs every three years.", "FALSE", "general"),
    _fc(9, "The currency of Japan is the yen.", "TRUE", "business"),
    # support
    _sup(1, "The Eiffel Tower, completed in 1889, stands 330 metres tall.",
         "The Eiffel Tower is over 300 metres tall.", "SUPPORTED", "geography"),
    _sup(2, "The Eiffel Tower was completed in 1889.",
         "The Eiffel Tower was completed in 1900.", "NOT_SUPPORTED", "geography"),
    _sup(3, "Q3 revenue was $4.2M, up 12% year over year.",
         "Revenue grew compared with the prior year.", "SUPPORTED", "business"),
    _sup(4, "Q3 revenue was $4.2M, up 12% year over year.",
         "The company was unprofitable in Q3.", "NOT_SUPPORTED", "business"),
    _sup(5, "Photosynthesis converts carbon dioxide and water into glucose and oxygen.",
         "Photosynthesis releases oxygen.", "SUPPORTED", "science"),
    _sup(6, "The treaty was signed by France and Spain in 1659.",
         "Portugal signed the treaty.", "NOT_SUPPORTED", "general"),
    _sup(7, "The library is open from 9am to 5pm on weekdays.",
         "The library is open on Saturday.", "NOT_SUPPORTED", "general"),
    _sup(8, "All passengers must present a valid ticket before boarding.",
         "A ticket is required to board.", "SUPPORTED", "general"),
    # contradiction
    _con(1, "The meeting is scheduled for Monday.", "The meeting will take place on Tuesday.",
         "CONTRADICTION", "general"),
    _con(2, "Sales rose in Q1.", "Q1 saw an increase in sales.", "CONSISTENT", "business"),
    _con(3, "The device weighs 1.2 kilograms.", "The device weighs 1200 grams.",
         "CONSISTENT", "science"),
    _con(4, "The store closes at 8pm.", "The store stays open until 10pm.",
         "CONTRADICTION", "general"),
    _con(5, "Water freezes at 0 degrees Celsius.", "Water freezes at 32 degrees Fahrenheit.",
         "CONSISTENT", "science"),
    _con(6, "The flight departs from Gate A12.", "The flight departs from Gate B7.",
         "CONTRADICTION", "general"),
    _con(7, "The population of the town is about 50,000.", "Roughly fifty thousand people live in the town.",
         "CONSISTENT", "general"),
    _con(8, "The report was authored by Dr. Lee.", "Dr. Patel wrote the report.",
         "CONTRADICTION", "general"),
    # summary (deterministic keyword rubric, synonym groups)
    _sum(1, "Photosynthesis is the process by which green plants use sunlight to convert "
            "carbon dioxide and water into glucose and oxygen.",
         [["sunlight", "light"], ["carbon dioxide", "co2"], ["oxygen", "glucose"]], "science"),
    _sum(2, "The company reported that quarterly revenue rose 12 percent to $4.2 million, "
            "driven by strong demand in its enterprise segment.",
         [["revenue", "sales"], ["12", "twelve"], ["enterprise"]], "business"),
    _sum(3, "The Treaty of the Pyrenees, signed in 1659, ended the war between France and Spain "
            "and set the border along the Pyrenees mountains.",
         [["1659"], ["france"], ["spain"]], "geography"),
    _sum(4, "The new policy requires all employees to complete security training annually and "
            "to enable two-factor authentication on company accounts.",
         [["security training", "training"], ["two-factor", "2fa", "authentication"]], "business"),
]


def tasks_of_type(t: str) -> list[TaskItem]:
    return [x for x in TASKS if x.type == t]


def task_types() -> list[str]:
    return ["fact_check", "support", "contradiction", "summary"]
