"""Pick the right Java after a crash that says the Java version is wrong.

- "Unsupported major.minor version 65.0" / "class file version 65.0": the server
  needs a NEWER Java (class version 65 = Java 21, 61 = 17, 69 = 25).
- "Unsupported class file major version 65": an old modloader can't handle the
  Java it's running on, so it needs an OLDER Java.
"""
import re

TIERS = [8, 11, 17, 21, 25]  # Java versions the itzg/minecraft-server image is published for
NEEDS_NEWER = [re.compile(r"Unsupported major\.minor version (\d+)"),
               re.compile(r"class file version (\d+)(?:\.\d+)?\)?,? this version of the Java Runtime only recognizes"),
               re.compile(r"compiled by a more recent version of the Java Runtime \(class file version (\d+)")]
NEEDS_OLDER = re.compile(r"Unsupported class file major version (\d+)")


def _major(tag: str) -> int | None:
    m = re.search(r"java(\d+)", tag or "")
    return int(m.group(1)) if m else None


def suggest(lines: list[str], current_tag: str) -> str | None:
    """Return a better image tag (e.g. 'java21'), or None if the log isn't a Java mismatch."""
    text = "\n".join(lines[-600:])
    for rx in NEEDS_NEWER:
        m = rx.search(text)
        if m:
            need = int(m.group(1)) - 44
            for t in TIERS:
                if t >= need:
                    tag = f"java{t}"
                    return tag if tag != current_tag else None
            return "java25"
    m = NEEDS_OLDER.search(text)
    if m:
        cur = _major(current_tag) or 25
        lower = [t for t in TIERS if t < cur and t != 11]
        return f"java{lower[-1]}" if lower else None
    return None
