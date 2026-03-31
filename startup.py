from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

STARTUP_PDF_OPTION_NAMES = {
    "--file",
    "--open",
    "--document",
    "--pdf",
    "--filename",
    "--input",
    "--path",
    "-f",
}


def resolve_startup_pdf_argument(raw_arg: str) -> Path | None:
    value = (raw_arg or "").strip()
    if not value:
        return None

    for option in STARTUP_PDF_OPTION_NAMES:
        lower_value = value.lower()

        if len(value) > len(option) and lower_value.startswith(option):
            remainder = value[len(option):]
            looks_like_path = (
                remainder[:1] in {"/", "\\", "~", ".", '"', "'"}
                or re.match(r"^[A-Za-z]:", remainder)
                or remainder.lower().startswith(("file://", "./", "../"))
                or remainder.lower().endswith(".pdf")
            )
            if looks_like_path:
                value = remainder.strip()
                if not value:
                    return None
                break

        for separator in ("=", ":"):
            prefix = f"{option}{separator}"
            if lower_value.startswith(prefix):
                value = value[len(prefix):].strip()
                if not value:
                    return None
                break
        else:
            continue
        break

    wrapper_pairs = {"<": ">", "(": ")", "[": "]", "{": "}"}
    while True:
        changed = False

        while len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
            changed = True
            if not value:
                return None

        while len(value) >= 2 and value[0] in wrapper_pairs and value[-1] == wrapper_pairs[value[0]]:
            value = value[1:-1].strip()
            changed = True
            if not value:
                return None

        while value.endswith((".", ",", ";", ":", "!", "?")):
            trimmed = value.rstrip(".,;:!?").strip()
            if (
                trimmed
                and (
                    trimmed.lower().startswith("file://")
                    or ".pdf" in trimmed.lower()
                )
            ):
                value = trimmed
                changed = True
                continue
            break

        if not changed:
            break

    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/].*", value):
        value = re.split(r"[?#]", value, maxsplit=1)[0]
        candidate = Path(unquote(value)).expanduser()
        if candidate.exists() and candidate.is_file() and candidate.suffix.lower() == ".pdf":
            return candidate

    parsed = urlparse(value)
    if parsed.scheme == "file":
        file_path = unquote(parsed.path or "")
        if parsed.netloc:
            netloc = parsed.netloc.lower()
            if netloc == "localhost":
                pass
            elif os.name == "nt" and re.match(r"^[A-Za-z]:$", parsed.netloc):
                file_path = f"{parsed.netloc}{file_path}"
            else:
                file_path = f"//{parsed.netloc}{file_path}"

        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", file_path):
            file_path = file_path[1:]

        if os.name == "nt" and re.match(r"^/?[A-Za-z]\|/", file_path):
            if file_path.startswith("/"):
                file_path = file_path[1:]
            file_path = f"{file_path[0]}:{file_path[2:]}"

        candidate = Path(file_path).expanduser()
    elif parsed.scheme == "" and (parsed.query or parsed.fragment):
        candidate = Path(unquote(parsed.path or "")).expanduser()
    else:
        candidate = Path(unquote(value)).expanduser()

    if candidate.exists() and candidate.is_file() and candidate.suffix.lower() == ".pdf":
        return candidate
    return None


def find_startup_pdf_argument(argv: list[str]) -> Path | None:
    i = 0
    while i < len(argv):
        raw = argv[i]
        if raw in STARTUP_PDF_OPTION_NAMES:
            if i + 1 < len(argv):
                candidate = resolve_startup_pdf_argument(argv[i + 1])
                if candidate is not None:
                    return candidate
            i += 1
            continue

        candidate = resolve_startup_pdf_argument(raw)
        if candidate is not None:
            return candidate
        i += 1

    return None
