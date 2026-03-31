from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedDocInfo:
    date: str = ""
    vendor: str = ""
    doc_type: str = "Dokument"
    number: str = ""
    subject: str = ""


@dataclass
class ExportRecord:
    datei: str
    datum: str
    typ: str
    absender: str
    nummer: str
    betreff: str
    betrag: str
    waehrung: str
    text_laenge: int
    text_auszug: str


@dataclass
class OCRPassResult:
    text: str
    mean_confidence: float
    low_conf_tokens: list[str]
    low_conf_lines: list[str]


@dataclass
class BatchRenameProposal:
    src: Path
    dst: Path
    reason: str
    source: str
    confidence: str
