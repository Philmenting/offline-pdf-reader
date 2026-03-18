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

    sample_ordinal = """
Invoice Number: INV-2026-88
Date: 17th March 2026
Total: 149.00 EUR
"""
    rec_ordinal = build_export_record("demo-ordinal.pdf", sample_ordinal)
    assert rec_ordinal.datum == "2026-03-17"

    sample_compact_month_first = """
Invoice INV-2026-89
Date 031726
"""
    rec_compact_month_first = build_export_record("demo-compact.pdf", sample_compact_month_first)
    assert rec_compact_month_first.datum == "2026-03-17"

    print("validate_helpers: OK")


if __name__ == "__main__":
    run()
