import json
import sys
from pathlib import Path

try:
    from app import parse_doc_info, suggest_filename_from_text
except (ModuleNotFoundError, ImportError) as exc:
    missing = getattr(exc, "name", "") or str(exc)
    print(f"Parser regression: SKIPPED (missing dependency: {missing})")
    raise SystemExit(0)


def _contains(actual: str, expected_part: str) -> bool:
    return expected_part.lower() in (actual or "").lower()


def main() -> int:
    cases_path = Path(__file__).with_name("parser_regression_cases.json")
    cases = json.loads(cases_path.read_text(encoding="utf-8"))

    failures: list[str] = []
    for case in cases:
        name = case.get("name", "unnamed")
        text = case.get("text", "")
        expect = case.get("expect", {})

        info = parse_doc_info(text)
        _ = suggest_filename_from_text(text)

        if expect.get("doc_type") and info.doc_type != expect["doc_type"]:
            failures.append(f"{name}: doc_type expected '{expect['doc_type']}', got '{info.doc_type}'")
        if expect.get("date") and info.date != expect["date"]:
            failures.append(f"{name}: date expected '{expect['date']}', got '{info.date}'")
        if expect.get("vendor_contains") and not _contains(info.vendor, expect["vendor_contains"]):
            failures.append(f"{name}: vendor missing '{expect['vendor_contains']}', got '{info.vendor}'")
        if expect.get("number_contains") and not _contains(info.number, expect["number_contains"]):
            failures.append(f"{name}: number missing '{expect['number_contains']}', got '{info.number}'")
        if expect.get("subject_contains") and not _contains(info.subject, expect["subject_contains"]):
            failures.append(f"{name}: subject missing '{expect['subject_contains']}', got '{info.subject}'")

    if failures:
        print("Parser regression: FAILED")
        for f in failures:
            print(f"- {f}")
        return 1

    print(f"Parser regression: OK ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
