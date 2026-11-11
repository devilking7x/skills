#!/usr/bin/env python3
"""Validate every skill in skills/*/SKILL.md, plus skills.json.

Two tiers, because the two sets of skills follow different anatomies:

- Universal checks, per skill (errors block the run). These hold for
  every skill in the repo, upstream and new:
  1. YAML frontmatter is present (``---`` block at the top of the file).
  2. Frontmatter ``name`` matches the skill's directory name, and the
     directory name is lowercase-hyphen-separated.
  3. Frontmatter ``description`` is non-empty.
  4. No placeholder text: bracketed unfinished markers (``[placeholder]``,
     ``[todo]``, ``[fixme]``, ``[tbd]``, ``[xxx]``). The check is
     deliberately narrow: words like "TODO" in example code comments
     and "placeholder" in prose about ``.env.example`` values are
     legitimate content and are NOT flagged.
  5. No em-dash anywhere in the file. AGENTS.md bans em-dashes in
     repo prose outright; this holds for every skill, upstream and
     new. En-dashes are checked for the new skills only: several
     upstream skills use en-dash ranges, and the additive-only rule
     for this fork leaves upstream files untouched, so upstream
     en-dashes are out of scope here. New skills write ranges out
     ("30 to 60 minutes"). Applies to frontmatter too.
  6. User-invoked consistency (AGENTS.md): a skill with
     ``disable-model-invocation: true`` in frontmatter must also have
     ``agents/openai.yaml`` with ``policy.allow_implicit_invocation:
     false``. Any ``agents/openai.yaml`` that exists must carry an
     ``interface`` with ``display_name`` and ``short_description``.

- Strict checks, for the five Raza7x security skills only
  (``NEW_SECURITY_SKILLS``). Upstream skills follow this repo's own
  anatomy (short descriptions, no fixed section list), so the
  description and section contract below applies to the new skills
  only; applying it upstream would fail skills that are correct as
  written (e.g. ``ask-matt`` has an 83-char description, ``pr`` a
  29-char one, and most upstream skills carry no ``## Overview``).
  7. Frontmatter ``description`` is 100-1024 chars and states trigger
     conditions: it must contain "Use when" (or the equivalent "Use
     before/after/during"), and a negated-only description ("Do not use
     when ...") does not count. The description value must also be
     valid YAML (no unquoted ": " inside it, a host parsing the
     frontmatter as YAML rejects the whole file otherwise).
  8. Required sections are present: ``## Overview``,
     ``## When to Use``, ``## Common Rationalizations``, ``## Red
     Flags``, ``## Verification``. Headings inside fenced code blocks
     do not count.
  9. A core workflow ("Process") section is present: a ``## Process``
     or ``## Process: ...`` heading, or an equivalent heading
     (``Core Process``, ``The Workflow``, ``Steps``, ``How It Works``,
     ``Pipeline``, ``Cycle``, ``Checklist``).

- Index check (when ``skills.json`` exists at the repo root):
  10. Every ``skills/*/SKILL.md`` is listed exactly once, with
      matching ``name``, ``description`` and ``path``; no stale
      entries point at skills that no longer exist.

Usage:
    python3 scripts/validate-skills.py [repo-root]

Exit code 0 when every skill passes, 1 otherwise. Python standard
library only.
"""

import json
import re
import sys
from pathlib import Path

KEBAB_CASE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# The five security skills added by this fork. They live in
# skills/in-progress/ (beta bucket: no plugin.json entry, no docs page,
# no ask-matt routing, per AGENTS.md) and are user-invoked.
NEW_SECURITY_SKILLS = {
    "bug-bounty-hunting",
    "secret-management",
    "incident-response",
    "api-security-audit",
    "supply-chain-security",
}

# A description must state WHEN to use the skill. Accept "Use when ..."
# plus the equivalent "Use before/after/during ..." phrasings; reject
# negated-only descriptions.
DESCRIPTION_TRIGGER = re.compile(
    r"\buse (this )?when\b|\buse (before|after|during)\b", re.I
)
DESCRIPTION_TRIGGER_NEGATE = re.compile(
    r"\b(do not|don't|never) use (this )?(when|before|after|during)\b", re.I
)

# Bracketed markers that mean unfinished content. Deliberately narrow:
# see check 4 in the module docstring.
PLACEHOLDERS = ("[placeholder]", "[todo]", "[fixme]", "[tbd]", "[xxx]")

# Sections every new security SKILL.md must contain.
REQUIRED_SECTIONS = (
    "Overview",
    "When to Use",
    "Common Rationalizations",
    "Red Flags",
    "Verification",
)

# Headings that count as the skill's core workflow ("Process") section.
PROCESS_PATTERNS = (
    "process",
    "workflow",
    "pipeline",
    "cycle",
    "checklist",
    "steps",
    "how it works",
)

# Upstream exception for check 6: software-factory is user-invoked
# (disable-model-invocation: true) but ships no agents/openai.yaml.
# That predates this fork; the additive-only rule for this fork leaves
# upstream files untouched, so the validator records it instead of
# failing on it. A fix belongs upstream.
OPENAI_YAML_EXEMPT = {
    "software-factory": "user-invoked upstream skill with no agents/openai.yaml",
}


def strip_fenced_code_blocks(content):
    """Remove fenced code blocks (``` / ~~~) so headings and markers
    inside examples or templates are not matched by lint rules."""
    out = []
    open_fence = None  # (marker, length) while inside a fence
    for line in content.splitlines():
        if open_fence:
            close = re.match(r"^ {0,3}(`{3,}|~{3,})[ \t]*$", line)
            if close and close.group(1)[0] == open_fence[0] and len(close.group(1)) >= open_fence[1]:
                open_fence = None
            out.append("")
            continue
        start = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if start and not (start.group(1)[0] == "`" and "`" in start.group(2)):
            open_fence = (start.group(1)[0], len(start.group(1)))
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def parse_frontmatter(text):
    """Return (frontmatter dict, body) or (None, text) if no frontmatter."""
    match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not match:
        return None, text
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = unquote(value.strip())
    return fields, text[match.end():]


def unquote(value):
    """Undo YAML quoting on a single-line scalar: double-quoted values
    unescape \" and \\ (JSON-compatible escapes), single-quoted values
    unescape '' to '."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def headings(body):
    """Return the list of level-2 heading titles in the body."""
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", body, re.M)]


def has_process_section(found):
    """True if any level-2 heading is the skill's core workflow section."""
    for heading in found:
        lowered = heading.lower()
        if lowered == "process" or lowered.startswith("process:"):
            return True
        if any(pattern in lowered for pattern in PROCESS_PATTERNS):
            return True
    return False


def check_skill(path):
    """Check one SKILL.md. Return a list of error strings (empty = pass)."""
    errors = []
    name = path.parent.name
    text = path.read_text(encoding="utf-8")
    is_new = name in NEW_SECURITY_SKILLS

    frontmatter, body = parse_frontmatter(text)
    if frontmatter is None:
        errors.append("missing YAML frontmatter (--- block)")
        return errors

    if frontmatter.get("name") != name:
        errors.append(
            f"frontmatter name {frontmatter.get('name')!r} does not match "
            f"directory name {name!r}"
        )
    if not KEBAB_CASE.match(name):
        errors.append(
            f"directory name {name!r} is not lowercase-hyphen-separated"
        )

    description = frontmatter.get("description", "")
    if not description:
        errors.append("description is empty")
    if is_new:
        if len(description) < 100:
            errors.append(
                f"description is {len(description)} chars, must be >= 100"
            )
        if len(description) > 1024:
            errors.append(
                f"description is {len(description)} chars, exceeds 1024"
            )
        if ": " in description or description.endswith(":"):
            errors.append(
                "description has an unquoted ': ' — YAML reads that as a "
                "nested mapping and a host parsing the frontmatter rejects "
                "the file; wrap the value in quotes"
            )
        stripped = DESCRIPTION_TRIGGER_NEGATE.sub("", description)
        if not DESCRIPTION_TRIGGER.search(description) or not DESCRIPTION_TRIGGER.search(stripped):
            errors.append(
                'description has no "when to use" trigger — add a '
                '"Use when ..." clause'
            )

    prose = strip_fenced_code_blocks(body)
    for marker in PLACEHOLDERS:
        if marker in prose.lower():
            errors.append(f"placeholder text found: {marker!r}")
    if "—" in text:
        errors.append("em-dash found — AGENTS.md bans em-dashes in repo prose")
    if is_new and "–" in text:
        errors.append("en-dash found — write ranges out (\"30 to 60 minutes\")")

    if is_new:
        found = headings(prose)
        for section in REQUIRED_SECTIONS:
            if section not in found:
                errors.append(f"missing required section: ## {section}")
        if not has_process_section(found):
            errors.append(
                "missing core workflow section: ## Process (or equivalent "
                "heading such as Steps, Checklist, Pipeline)"
            )

    # Check 6: user-invoked consistency with agents/openai.yaml.
    user_invoked = frontmatter.get("disable-model-invocation") == "true"
    yaml_path = path.parent / "agents" / "openai.yaml"
    if user_invoked and not yaml_path.exists():
        if name in OPENAI_YAML_EXEMPT:
            print(f"  note: {name}: {OPENAI_YAML_EXEMPT[name]} (upstream, exempt)")
        else:
            errors.append(
                "user-invoked (disable-model-invocation: true) but "
                "agents/openai.yaml is missing"
            )
    if yaml_path.exists():
        yaml_text = yaml_path.read_text(encoding="utf-8")
        if "display_name" not in yaml_text:
            errors.append("agents/openai.yaml has no interface.display_name")
        if "short_description" not in yaml_text:
            errors.append("agents/openai.yaml has no interface.short_description")
        if user_invoked and "allow_implicit_invocation: false" not in yaml_text:
            errors.append(
                "user-invoked but agents/openai.yaml does not set "
                "policy.allow_implicit_invocation: false"
            )

    return errors


def check_index(root):
    """Check skills.json against the skills on disk. Return error list."""
    errors = []
    index_path = root / "skills.json"
    if not index_path.exists():
        return ["skills.json is missing"]
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"skills.json is not valid JSON: {exc}"]
    if not isinstance(index, list):
        return ["skills.json must be a JSON array of skill entries"]

    on_disk = {}
    for skill_path in sorted((root / "skills").rglob("SKILL.md")):
        frontmatter, _ = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        if frontmatter is None:
            continue
        rel = str(skill_path.relative_to(root)).replace("\\", "/")
        on_disk[frontmatter.get("name")] = {
            "description": frontmatter.get("description", ""),
            "path": rel,
        }

    seen = set()
    for entry in index:
        entry_name = entry.get("name")
        if entry_name in seen:
            errors.append(f"skills.json lists {entry_name!r} more than once")
            continue
        seen.add(entry_name)
        if entry_name not in on_disk:
            errors.append(f"skills.json lists {entry_name!r} but no such skill exists")
            continue
        for field in ("description", "path"):
            if entry.get(field) != on_disk[entry_name][field]:
                errors.append(
                    f"skills.json {entry_name!r} {field} does not match "
                    f"SKILL.md ({entry.get(field)!r} vs "
                    f"{on_disk[entry_name][field]!r})"
                )
        if not entry.get("category"):
            errors.append(f"skills.json {entry_name!r} has no category")
    for name in sorted(on_disk):
        if name not in seen:
            errors.append(f"skills.json is missing skill {name!r}")
    return errors


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    skills_dir = root / "skills"
    skill_files = sorted(skills_dir.rglob("SKILL.md"))
    if not skill_files:
        print(f"no skills found under {skills_dir}")
        return 1

    skill_failures = 0
    for skill_path in skill_files:
        name = skill_path.parent.name
        errors = check_skill(skill_path)
        if errors:
            skill_failures += 1
            print(f"FAIL {name} ({skill_path.relative_to(root)}):")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"PASS {name}")

    index_errors = check_index(root)
    if index_errors:
        print("FAIL skills.json:")
        for error in index_errors:
            print(f"  - {error}")
    else:
        print("PASS skills.json")

    total = len(skill_files)
    print(f"\n{total - skill_failures}/{total} skills passed"
          f"{' and skills.json passed' if not index_errors else ''}")
    return 1 if (skill_failures or index_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
