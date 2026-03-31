from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from models import ExportRecord, ParsedDocInfo


def _normalize_filename_part(value: str, max_len: int = 48) -> str:
    token = (value or "").strip()
    if not token:
        return ""
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
    }
    for src, dst in replacements.items():
        token = token.replace(src, dst)
    token = unicodedata.normalize("NFKD", token)
    token = token.encode("ascii", "ignore").decode("ascii")
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", token)
    token = re.sub(r"_+", "_", token).strip("._-")
    return token[:max_len]


def sanitize_filename(name: str) -> str:
    name = _normalize_filename_part(name, max_len=140)
    if not name:
        return "Dokument"

    # Avoid Windows reserved device names (CON, PRN, AUX, NUL, COM1..9, LPT1..9).
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }
    stem = name.split(".", 1)[0].upper()
    if stem in reserved:
        return f"{name}_"
    return name


def _list_pdf_files(folder: Path) -> list[Path]:
    return sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])


def _looks_like_subject_line(line: str) -> bool:
    ln = (line or "").strip()
    if len(ln) < 10:
        return False
    if re.fullmatch(r"[\W_\d]+", ln):
        return False

    low = ln.lower()
    if any(k in low for k in ["straße", "str.", "strasse", "street", "telefon", "phone", "fax", "www.", "mail", "e-mail", "deutschland", "germany"]):
        return False

    words = re.findall(r"[A-Za-zÄÖÜäöüß]{2,}", ln)
    if len(words) < 3:
        return False

    short_words = [w for w in words if len(w) <= 2]
    if len(short_words) > max(2, len(words) // 2):
        return False

    return True


def parse_doc_info(text: str) -> ParsedDocInfo:
    lower = text.lower()
    raw_lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in raw_lines if ln]

    # Doc type
    doc_type = "Dokument"

    type_patterns: dict[str, list[str]] = {
        "Gutschrift": [r"\bgutschrift\b", r"\bcredit\s+note\b", r"\bcredit\s+memo\b"],
        "Mahnung": [r"\bmahnung\b", r"\bzahlungserinnerung\b", r"\bpayment\s+reminder\b"],
        "Auftragsbestaetigung": [r"\bauftragsbest[aä]tigung\b", r"\border\s+confirmation\b"],
        "Lieferschein": [r"\blieferschein\b", r"\bdelivery\s+note\b", r"\bdispatch\s+note\b", r"\bdespatch\s+note\b", r"\bpacking\s+slip\b"],
        "Rechnung": [r"\brechnung\b", r"\binvoice\b"],
        "Angebot": [r"\bangebot\b", r"\bquote\b", r"\bquotation\b"],
        "Bestellung": [r"\bbestellung\b", r"\bpurchase\s+order\b", r"\border\b"],
        "Vertrag": [r"\bvertrag\b", r"\bcontract\b"],
    }

    type_scores: dict[str, int] = {k: 0 for k in type_patterns}

    type_label_re = re.compile(r"(?i)^(?:dokumenttyp|typ|type|document\s+type)\s*[:#-]?\s*(.+)$")
    for idx, ln in enumerate(lines[:60]):
        m_type = type_label_re.match(ln)
        if not m_type:
            continue
        tail = m_type.group(1).lower()
        for t_name, patterns in type_patterns.items():
            if any(re.search(p, tail) for p in patterns):
                type_scores[t_name] += 10
        if idx + 1 < len(lines):
            nxt = lines[idx + 1].lower()
            for t_name, patterns in type_patterns.items():
                if any(re.search(p, nxt) for p in patterns):
                    type_scores[t_name] += 7

    top_chunk = "\n".join(lines[:30]).lower()
    for t_name, patterns in type_patterns.items():
        for pat in patterns:
            if re.search(pat, lower):
                type_scores[t_name] += 2
            if re.search(pat, top_chunk):
                type_scores[t_name] += 2

    type_priority = {
        "Gutschrift": 8,
        "Mahnung": 7,
        "Auftragsbestaetigung": 6,
        "Lieferschein": 5,
        "Rechnung": 4,
        "Angebot": 3,
        "Bestellung": 2,
        "Vertrag": 1,
    }

    best_type = max(type_scores.items(), key=lambda kv: (kv[1], type_priority.get(kv[0], 0)))
    if best_type[1] > 0:
        doc_type = best_type[0]

    # Date
    date_patterns = [
        r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b",
        r"\b(\d{1,2}\.\d{1,2}\.\d{2})\b",
        r"\b(\d{4}-\d{1,2}-\d{1,2})\b",
        r"\b(\d{4}\.\d{1,2}\.\d{1,2})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{4})\b",
        r"\b(\d{1,2}-\d{1,2}-\d{2})\b",
        r"\b(\d{4}/\d{1,2}/\d{1,2})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b",
        r"\b(\d{1,2}/\d{1,2}/\d{2})\b",
        r"\b(\d{1,2}\s+\d{1,2}\s+\d{4})\b",
        r"\b(\d{1,2}\s+\d{1,2}\s+\d{2})\b",
        r"\b(\d{4}\s+\d{1,2}\s+\d{1,2})\b",
    ]

    def _parse_date_token(raw: str) -> str:
        token = (raw or "").strip().strip(".,;:)")
        if not token:
            return ""

        compact = re.sub(r"\D", "", token)
        if compact:
            compact_formats: tuple[str, ...] = ()
            if len(compact) == 8:
                compact_formats = ("%Y%m%d", "%d%m%Y", "%m%d%Y")
            elif len(compact) == 6:
                compact_formats = ("%d%m%y", "%y%m%d", "%m%d%y")
            for fmt in compact_formats:
                try:
                    parsed = datetime.strptime(compact, fmt)
                    if 1990 <= parsed.year <= 2100:
                        return parsed.strftime("%Y-%m-%d")
                except ValueError:
                    continue

        for fmt in (
            "%d.%m.%Y", "%d.%m.%y", "%m.%d.%Y", "%m.%d.%y",
            "%Y-%m-%d", "%Y.%m.%d", "%d-%m-%Y", "%d-%m-%y",
            "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d", "%d/%m/%Y",
            "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y",
            "%d %m %Y", "%d %m %y", "%Y %m %d",
        ):
            try:
                parsed = datetime.strptime(token, fmt)
                if 1990 <= parsed.year <= 2100:
                    return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return ""

    date = ""

    date_label_pattern = (
        r"(?:dat(?:um|urn)|date|"
        r"rechnungs\s*[-_]?\s*dat(?:um|urn)|beleg\s*[-_]?\s*dat(?:um|urn)|"
        r"ausstellungs\s*[-_]?\s*dat(?:um|urn)|leistungs\s*[-_]?\s*dat(?:um|urn)|"
        r"invoice\s+date|document\s+date|issue\s+date)"
    )
    date_label_re = re.compile(rf"(?i)^{date_label_pattern}\s*[:#-]?\s*(.*)$")
    inline_date_re = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{8}|\d{6})\b")
    for idx, ln in enumerate(lines[:80]):
        m_label = date_label_re.match(ln)
        if not m_label:
            continue
        tail = (m_label.group(1) or "").strip()
        m_inline = inline_date_re.search(tail)
        if m_inline:
            parsed_inline = _parse_date_token(m_inline.group(1))
            if parsed_inline:
                date = parsed_inline
                break
        if idx + 1 < len(lines):
            m_next = inline_date_re.search(lines[idx + 1])
            if m_next:
                parsed_next = _parse_date_token(m_next.group(1))
                if parsed_next:
                    date = parsed_next
                    break

    if not date:
        for pat in date_patterns:
            m = re.search(pat, text)
            if m:
                parsed_generic = _parse_date_token(m.group(1))
                if parsed_generic:
                    date = parsed_generic
                    break

    if not date:
        m_compact = re.search(
            rf"(?i)\b{date_label_pattern}\s*[:\-]?\s*(\d{{8}}|\d{{6}})\b",
            text,
        )
        if m_compact:
            raw = m_compact.group(1)
            try:
                if len(raw) == 8:
                    if raw.startswith(("19", "20")):
                        parsed = datetime.strptime(raw, "%Y%m%d")
                    else:
                        parsed = None
                        for fmt in ("%d%m%Y", "%m%d%Y"):
                            try:
                                parsed = datetime.strptime(raw, fmt)
                                break
                            except ValueError:
                                continue
                        if not parsed:
                            raise ValueError
                else:
                    parsed = None
                    for fmt in ("%d%m%y", "%m%d%y", "%y%m%d"):
                        try:
                            parsed = datetime.strptime(raw, fmt)
                            break
                        except ValueError:
                            continue
                    if not parsed:
                        raise ValueError
                date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass

    if not date:
        compact_candidates = re.findall(r"\b\d{8}\b", text) + re.findall(r"\b\d{6}\b", text)
        for raw in compact_candidates:
            parsed = None
            if len(raw) == 8:
                try:
                    if raw.startswith(("19", "20")):
                        parsed = datetime.strptime(raw, "%Y%m%d")
                    else:
                        parsed = None
                        for fmt in ("%d%m%Y", "%m%d%Y"):
                            try:
                                parsed = datetime.strptime(raw, fmt)
                                break
                            except ValueError:
                                continue
                        if not parsed:
                            raise ValueError
                except ValueError:
                    continue
            else:
                for fmt in ("%d%m%y", "%m%d%y", "%y%m%d"):
                    try:
                        parsed = datetime.strptime(raw, fmt)
                        break
                    except ValueError:
                        continue
                if not parsed:
                    continue

            if 1990 <= parsed.year <= 2100:
                date = parsed.strftime("%Y-%m-%d")
                break

    if not date:
        month_map = {
            "januar": "01", "january": "01", "jan": "01",
            "februar": "02", "february": "02", "feb": "02",
            "märz": "03", "maerz": "03", "marz": "03", "mär": "03", "mrz": "03", "march": "03", "mar": "03",
            "april": "04", "apr": "04",
            "mai": "05", "may": "05",
            "juni": "06", "june": "06", "jun": "06",
            "juli": "07", "july": "07", "jul": "07",
            "august": "08", "aug": "08",
            "september": "09", "sep": "09", "sept": "09",
            "oktober": "10", "october": "10", "okt": "10", "oct": "10",
            "november": "11", "nov": "11",
            "dezember": "12", "december": "12", "dez": "12", "dec": "12",
        }
        m_textual = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?[.\s-]+([A-Za-zÄÖÜäöü]+)[.,\s-]+(\d{2}|\d{4})\b",
            text, re.IGNORECASE,
        )
        if m_textual:
            day, month_raw, year_raw = m_textual.groups()
            month_key = month_raw.strip(".,").lower()
            month = month_map.get(month_key)
            if month:
                try:
                    if len(year_raw) == 2:
                        parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%y")
                    else:
                        parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%Y")
                    date = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    pass

        if not date:
            m_textual_month_first = re.search(
                r"\b([A-Za-zÄÖÜäöü]+)[.,]?[-/\s]+(\d{1,2})(?:st|nd|rd|th)?[,\.]?[-/\s]+(\d{2}|\d{4})\b",
                text, re.IGNORECASE,
            )
            if m_textual_month_first:
                month_raw, day, year_raw = m_textual_month_first.groups()
                month_key = month_raw.strip(".,").lower()
                month = month_map.get(month_key)
                if month:
                    try:
                        if len(year_raw) == 2:
                            parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%y")
                        else:
                            parsed = datetime.strptime(f"{int(day):02d}.{month}.{year_raw}", "%d.%m.%Y")
                        date = parsed.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

    # Number
    def _normalize_doc_number(raw: str) -> str:
        cleaned = (raw or "").strip().strip(".,;:)")
        cleaned = re.sub(r"^[#:\-\s]+", "", cleaned)
        cleaned = re.sub(
            r"(?i)^(?:nr|nummer|no|number|invoice|rechnung|beleg|doc|id)\s*[:#\-/]*\s*",
            "", cleaned,
        )
        cleaned = cleaned.replace("\\", "/")
        cleaned = re.sub(r"\s*([/_-])\s*", r"\1", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = cleaned.replace(" ", "")
        cleaned = re.sub(r"[.,;:]+$", "", cleaned)
        cleaned = re.sub(r"(?<=\d)[Oo](?=\d)", "0", cleaned)
        cleaned = re.sub(r"(?<=\d)[Il](?=\d)", "1", cleaned)
        cleaned = re.sub(r"(?<=\d)S(?=\d)", "5", cleaned)
        cleaned = re.sub(r"(?<=\d)B(?=\d)", "8", cleaned)
        cleaned = re.sub(r"([/_-]){2,}", r"\1", cleaned)
        return cleaned.strip()

    number = ""
    value_re = re.compile(r"^[A-Z0-9][A-Z0-9/_-]{2,}$", re.IGNORECASE)
    number_candidates: dict[str, int] = {}

    def _add_number_candidate(raw_val: str, score: int) -> None:
        val = _normalize_doc_number(raw_val)
        if not val or not value_re.match(val):
            return
        number_candidates[val] = number_candidates.get(val, 0) + score

    invoice_like = doc_type in {"Rechnung", "Gutschrift", "Mahnung"}
    order_like = doc_type in {"Bestellung", "Auftragsbestaetigung"}
    delivery_like = doc_type == "Lieferschein"

    strict_patterns = [
        (
            r"(?i)(?:rechnungs\s*[-_]?\s*(?:nr|nummer)\.?|rechn\.?\s*[-/]?\s*nr\.?|re\.?\s*[-/]?\s*nr\.?|rg\.?\s*[-/]?\s*nr\.?|invoice\s*(?:no|number|nr)\.?|invoice\s*#|beleg\s*[-_]?\s*nr\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            12 if invoice_like else 10,
        ),
        (
            r"(?i)(?:vorgangs\s*[-_]?\s*(?:nr|nummer)\.?|bestell\s*[-_]?\s*(?:nr|nummer)\.?|order\s*(?:no|number)\.?|purchase\s*order\s*(?:no|number)\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            9 if order_like else 6,
        ),
        (
            r"(?i)(?:lieferschein\s*[-_]?\s*(?:nr|nummer)\.?|delivery\s*note\s*(?:no|number)\.?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
            10 if delivery_like else 7,
        ),
        (r"(?i)\b(?:inv|doc|po|dn)\s*[-_]?\s*([A-Z0-9][A-Z0-9/_-]{2,})\b", 4),
    ]
    for pat, pts in strict_patterns:
        for m_num in re.finditer(pat, text):
            _add_number_candidate(m_num.group(1), pts)

    label_re = re.compile(
        r"(?i)^(?:rechnungs\s*[-_]?\s*(?:nr|nummer)|rechn\.?\s*nr\.?|re\.?\s*nr\.?|rg\.?\s*nr\.?|invoice\s*(?:no|number|nr)|beleg\s*[-_]?\s*nr\.?|vorgangs\s*[-_]?\s*(?:nr|nummer)|bestell\s*[-_]?\s*(?:nr|nummer)|order\s*(?:no|number)|purchase\s*order\s*(?:no|number)|lieferschein\s*[-_]?\s*(?:nr|nummer)|delivery\s*note\s*(?:no|number)|nr\.?)\s*[:#-]?\s*(.*)$"
    )
    for idx, ln in enumerate(lines[:80]):
        m_label = label_re.match(ln.strip())
        if not m_label:
            continue
        ll = ln.lower()
        if any(k in ll for k in ["rechnung", "invoice", "beleg"]):
            base_score = 11 if invoice_like else 9
        elif any(k in ll for k in ["bestell", "order", "purchase"]):
            base_score = 8 if order_like else 6
        elif any(k in ll for k in ["lieferschein", "delivery note"]):
            base_score = 9 if delivery_like else 6
        else:
            base_score = 6
        inline_val = _normalize_doc_number(m_label.group(1))
        if inline_val:
            _add_number_candidate(inline_val, base_score)
        if idx + 1 < len(lines):
            _add_number_candidate(lines[idx + 1], base_score - 1)

    for cand in list(number_candidates.keys()):
        bonus = 0
        low = cand.lower()
        if re.search(r"\d", cand) and re.search(r"[A-Z]", cand, re.IGNORECASE):
            bonus += 2
        if any(sep in cand for sep in ["/", "-", "_"]):
            bonus += 1
        if len(cand) < 4:
            bonus -= 3
        elif len(cand) < 6:
            bonus -= 1
        if re.match(r"(?i)^(re|rg|inv)", cand):
            bonus += 3 if invoice_like else 1
        if re.match(r"(?i)^(po|best|ord)", cand):
            bonus += 3 if order_like else 0
        if re.match(r"(?i)^(dn|ls|lief)", cand):
            bonus += 3 if delivery_like else 0
        if low in {"invoice", "rechnung", "number", "nummer", "order", "po", "doc", "id", "total", "summe"}:
            bonus -= 6
        if re.fullmatch(r"\d{4,}", cand):
            bonus -= 2
        number_candidates[cand] += bonus

    if number_candidates:
        number = max(number_candidates.items(), key=lambda kv: (kv[1], len(kv[0])))[0]

    def is_address_like(line: str) -> bool:
        l = line.lower()
        if re.search(r"\b\d{5}\b", l):
            return True
        if re.search(r"\b\d{1,4}[a-z]?\b", l) and any(
            token in l
            for token in ["straße", "str.", "strasse", "street", "st.", "weg", "allee", "avenue", "road", "rd."]
        ):
            return True
        if any(token in l for token in ["deutschland", "germany", "telefon", "phone", "fax", "mail", "e-mail", "www."]):
            return True
        return False

    subject = ""
    m_subject = re.search(r"(?im)^(?:betreff|subject)\s*(?::|-)?\s*(.+)$", text)
    if m_subject:
        subject = m_subject.group(1).strip()
    else:
        preferred_keywords = ["verkauf", "teilgrundstück", "grundstück", "betreff", "antrag", "kündigung", "vertrag", "rechnung", "invoice", "angebot", "gutschrift"]
        weighted: list[tuple[int, str]] = []
        for idx, ln in enumerate(lines[:60]):
            if not _looks_like_subject_line(ln):
                continue
            ll = ln.lower()
            score = 0
            if any(k in ll for k in preferred_keywords):
                score += 8
            score += min(6, len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", ln)))
            if idx < 25:
                score += 2
            weighted.append((score, ln))
        if weighted:
            weighted.sort(key=lambda x: x[0], reverse=True)
            subject = weighted[0][1]

    company_tokens = ["gmbh", "ag", "ug", "kg", "ohg", "inc", "llc", "ltd", "corp", "s.a.", "sarl", "e.k."]
    recipient_tokens = ["herr", "frau", "empfaenger", "empfänger", "kunde", "an:", "z. hd", "z.hd", "attn", "recipient"]

    def _clean_vendor_prefix(raw: str) -> str:
        cleaned = re.sub(r"(?i)^\s*(firma|company|vendor|lieferant|sender|absender)\s*[:\-]\s*", "", raw).strip()
        return cleaned

    sender_label_re = re.compile(
        r"(?i)^\s*(?:absender|sender|von|from|firma|company|vendor|lieferant)\s*[:\-]\s*(.+)$"
    )
    recipient_label_re = re.compile(
        r"(?i)^\s*(?:rechnung\s+an|invoice\s+to|kunde|empf[aä]nger|recipient|bill\s+to|ship\s+to)\s*[:\-]?.*$"
    )

    candidates_vendor: list[tuple[str, int]] = []

    for ln in lines[:40]:
        m_sender = sender_label_re.match(ln)
        if not m_sender:
            continue
        labeled = _clean_vendor_prefix(m_sender.group(1))
        if labeled and not is_address_like(labeled):
            candidates_vendor.append((labeled, 14))

    for ln in lines[:35]:
        ll = ln.lower()
        if len(ln) < 3:
            continue
        if is_address_like(ln):
            continue
        if recipient_label_re.match(ln):
            continue
        if any(k in ll for k in ["rechnung", "invoice", "seite", "page", "betreff", "subject", "datum", "date"]):
            continue
        if re.fullmatch(r"[\d\W_]+", ln):
            continue
        cleaned = _clean_vendor_prefix(ln)
        if cleaned:
            candidates_vendor.append((cleaned, 0))

    vendor = ""
    if candidates_vendor:
        scored: list[tuple[int, str]] = []
        for idx, (ln, base_score) in enumerate(candidates_vendor):
            ll = ln.lower()
            score = base_score
            if any(tok in ll for tok in company_tokens):
                score += 8
            if any(tok in ll for tok in recipient_tokens):
                score -= 6
            if re.search(r"\b(gbr|kg|gmbh|ag|inc|llc|ltd|corp|s\.?a\.?r\.?l\.?|s\.?a\.?)\b", ll):
                score += 3
            if idx < 10:
                score += 1
            word_count = len(re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", ln))
            score += min(4, word_count)
            if len(ln) > 64:
                score -= 2
            scored.append((score, ln))

        scored.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
        vendor = scored[0][1]

    return ParsedDocInfo(date=date, vendor=vendor, doc_type=doc_type, number=number, subject=subject)


def suggest_filename_from_text(text: str) -> str:
    info = parse_doc_info(text)
    parts = [
        _normalize_filename_part(info.date, max_len=16),
        _normalize_filename_part(info.doc_type, max_len=28),
        _normalize_filename_part(info.vendor, max_len=40),
        _normalize_filename_part(info.number, max_len=28),
    ]
    prioritized = [p for p in parts if p]
    if prioritized:
        return "_".join(prioritized)[:140] + ".pdf"

    fallback = _normalize_filename_part(info.subject, max_len=90)
    if fallback:
        return fallback + ".pdf"

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:40]:
        if _looks_like_subject_line(ln):
            cleaned = _normalize_filename_part(ln, max_len=90)
            if cleaned:
                return cleaned + ".pdf"

    return "Dokument.pdf"


def _normalize_amount_token(raw: str) -> str:
    token = re.sub(r"[\s\u00A0\u202F]", "", raw)
    token = token.replace("\u2019", "").replace("'", "")
    if not token:
        return ""

    has_comma = "," in token
    has_dot = "." in token

    if has_comma and has_dot:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousand_sep = "." if decimal_sep == "," else ","
        token = token.replace(thousand_sep, "")
        integer_part, frac_part = token.rsplit(decimal_sep, 1)
        if frac_part.isdigit() and len(frac_part) == 1:
            frac_part += "0"
        return f"{integer_part},{frac_part}"

    if has_dot and not has_comma:
        if re.search(r"\.\d{1,2}$", token):
            integer_part, frac_part = token.rsplit(".", 1)
            if len(frac_part) == 1:
                frac_part += "0"
            return f"{integer_part},{frac_part}"
        return token.replace(".", "")

    if has_comma and not has_dot:
        if re.search(r",\d{1,2}$", token):
            integer_part, frac_part = token.rsplit(",", 1)
            if len(frac_part) == 1:
                frac_part += "0"
            return f"{integer_part},{frac_part}"
        return token.replace(",", "")

    return token


def _normalize_currency_token(raw: str) -> str:
    token = (raw or "").strip().lower()
    if token in {"€", "eur"}:
        return "EUR"
    if token in {"chf"}:
        return "CHF"
    if token in {"$", "usd"}:
        return "USD"
    if token in {"£", "gbp"}:
        return "GBP"
    return ""


def extract_total_amount_info(text: str) -> tuple[str, str]:
    amount_expr = r"\d{1,3}(?:[\.,''\s\u00A0\u202F]\d{3})*(?:[\.,]\d{1,2})?|\d+(?:[\.,]\d{1,2})?"
    currency_expr = r"€|eur|chf|\$|usd|£|gbp"
    label_expr = (
        r"gesamt(?:betrag)?|rechnungsbetrag|endbetrag|summe|zu\s+zahlen|zu\s+überweisen|"
        r"brutto(?:betrag)?|fälliger\s+betrag|offener\s+betrag|restbetrag|saldo|"
        r"zahlbar(?:er\s+betrag)?|total(?:\s+due)?|grand\s+total|amount\s+due|"
        r"amount\s+payable|balance\s+due"
    )

    keyword_pattern = re.compile(
        rf"(?i)\b(?P<label>{label_expr})\b[^\dA-Z]{{0,16}}(?:(?P<curr_before>{currency_expr})\s*)?(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})?"
    )
    fallback_patterns = [
        re.compile(rf"(?i)(?P<curr_before>{currency_expr})\s*(?P<amount>{amount_expr})\b"),
        re.compile(rf"(?i)(?P<amount>{amount_expr})\s*(?P<curr_after>{currency_expr})\b"),
    ]

    label_weights = {
        "grand total": 100, "amount due": 95, "amount payable": 95,
        "balance due": 95, "gesamtbetrag": 90, "endbetrag": 90,
        "rechnungsbetrag": 88, "zu zahlen": 86, "zu überweisen": 86,
        "falliger betrag": 84, "bruttobetrag": 82, "brutto": 80,
        "total": 76, "summe": 70, "saldo": 68, "offener betrag": 66, "restbetrag": 65,
    }

    best: tuple[int, int, str, str] | None = None
    for m in keyword_pattern.finditer(text):
        amount = _normalize_amount_token(m.group("amount"))
        if not amount:
            continue
        currency = _normalize_currency_token(m.groupdict().get("curr_before") or m.groupdict().get("curr_after") or "")
        raw_label = (m.group("label") or "").lower()
        normalized_label = unicodedata.normalize("NFKD", raw_label).encode("ascii", "ignore").decode("ascii")
        weight = label_weights.get(normalized_label, 60)
        candidate = (weight, m.start(), amount, currency)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    if best is not None:
        return best[2], best[3]

    for pat in fallback_patterns:
        m = pat.search(text)
        if not m:
            continue
        amount = _normalize_amount_token(m.group("amount"))
        currency = _normalize_currency_token(m.groupdict().get("curr_before") or m.groupdict().get("curr_after") or "")
        return amount, currency

    return "", ""


def extract_total_amount(text: str) -> str:
    amount, _ = extract_total_amount_info(text)
    return amount


def candidate_variants(value: str) -> list[str]:
    confusion_map = {
        "0": ["O", "Q"], "O": ["0"], "1": ["I", "l"], "I": ["1", "l"],
        "l": ["1", "I"], "5": ["S"], "S": ["5"], "8": ["B"], "B": ["8"],
        "2": ["Z"], "Z": ["2"],
    }
    variants = {value}
    for idx, ch in enumerate(value):
        for repl in confusion_map.get(ch, []):
            variants.add(value[:idx] + repl + value[idx + 1:])
    variants.discard(value)
    return sorted(variants)[:6]


def build_export_record(file_name: str, text: str) -> ExportRecord:
    info = parse_doc_info(text)
    amount, currency = extract_total_amount_info(text)
    excerpt = re.sub(r"\s+", " ", text).strip()[:220]
    return ExportRecord(
        datei=file_name,
        datum=info.date,
        typ=info.doc_type,
        absender=info.vendor,
        nummer=info.number,
        betreff=info.subject,
        betrag=amount,
        waehrung=currency,
        text_laenge=len(text),
        text_auszug=excerpt,
    )


def export_records_as_txt(records: list[ExportRecord]) -> str:
    parts: list[str] = []
    for rec in records:
        parts.append(
            "\n".join([
                f"Datei: {rec.datei}",
                f"Datum: {rec.datum or '-'}",
                f"Typ: {rec.typ or '-'}",
                f"Absender: {rec.absender or '-'}",
                f"Nummer: {rec.nummer or '-'}",
                f"Betreff: {rec.betreff or '-'}",
                f"Betrag: {(rec.betrag + ' ' + rec.waehrung).strip() or '-'}",
                f"Textlänge: {rec.text_laenge}",
                f"Auszug: {rec.text_auszug or '-'}",
            ])
        )
    return "\n\n".join(parts)
