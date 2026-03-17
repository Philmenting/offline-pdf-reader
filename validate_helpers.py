from app import build_export_record, suggest_filename_from_text


def run() -> None:
    sample = """
Rechnung Nr: INV-2026-77
Datum: 17.03.2026
Muster GmbH
Gesamtbetrag 199,95 EUR
Betreff: Wartung März
"""
    rec = build_export_record("demo.pdf", sample)
    assert rec.typ in {"Rechnung", "Dokument"}
    assert rec.datum == "2026-03-17"
    assert rec.nummer.startswith("INV")
    fn = suggest_filename_from_text(sample)
    assert fn.endswith(".pdf")
    assert "Rechnung" in fn
    print("validate_helpers: OK")


if __name__ == "__main__":
    run()
