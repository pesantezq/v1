"""
Defensive 13F information-table XML parser.

The information table schema has varied across years and namespaces, so parsing
is namespace-agnostic (matched by local element name) and tolerant of missing
optional fields. It NEVER guesses: absent fields become ``None``; malformed XML
yields an empty :class:`ParsedFiling` with a ``malformed_xml`` warning (a valid,
honest degraded result — not a crash).

IMPORTANT — value units: pre-2023 13F filings reported ``value`` in THOUSANDS of
dollars; 2023+ filings report actual dollars. This parser records the RAW value
as filed plus a ``value_units_ambiguous`` warning; unit normalization is the
store/scoring layer's job (it has the filing period). We never scale here, to
avoid fabricating a figure.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET  # Element type + ParseError only

# Parse untrusted external EDGAR XML with defusedxml — hardened against XXE /
# billion-laughs / external-entity attacks (repo convention, requirements.txt).
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from .schemas import (
    PUT_CALL_CALL,
    PUT_CALL_NONE,
    PUT_CALL_PUT,
    ParsedFiling,
    ParsedHolding,
)


def _local(tag: str) -> str:
    """Strip an XML namespace: ``{ns}nameOfIssuer`` -> ``nameOfIssuer``."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_local(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text_local(elem: ET.Element, name: str) -> str | None:
    node = _find_local(elem, name)
    if node is None or node.text is None:
        return None
    txt = node.text.strip()
    return txt or None


def _float_local(elem: ET.Element, name: str) -> float | None:
    txt = _text_local(elem, name)
    if txt is None:
        return None
    try:
        return float(txt.replace(",", ""))
    except ValueError:
        return None


def _norm_put_call(raw: str | None) -> str:
    if not raw:
        return PUT_CALL_NONE
    low = raw.strip().lower()
    if low == "put":
        return PUT_CALL_PUT
    if low == "call":
        return PUT_CALL_CALL
    return PUT_CALL_NONE


def _parse_info_table(elem: ET.Element) -> ParsedHolding | None:
    issuer = _text_local(elem, "nameOfIssuer")
    cusip = _text_local(elem, "cusip")
    # A row without an issuer or CUSIP is unusable — skip it (caller warns).
    if not issuer or not cusip:
        return None

    # shrsOrPrnAmt sub-block: sshPrnamt + sshPrnamtType
    shares = _float_local(elem, "sshPrnamt")
    share_type = _text_local(elem, "sshPrnamtType")

    # votingAuthority sub-block: Sole/Shared/None
    voting = _find_local(elem, "votingAuthority")
    v_sole = _float_local(voting, "Sole") if voting is not None else None
    v_shared = _float_local(voting, "Shared") if voting is not None else None
    v_none = _float_local(voting, "None") if voting is not None else None

    other_managers: list[str] = []
    for child in elem.iter():
        if _local(child.tag) == "otherManager" and child.text and child.text.strip():
            other_managers.append(child.text.strip())

    return ParsedHolding(
        issuer_name=issuer,
        class_title=_text_local(elem, "titleOfClass") or "",
        cusip=cusip.strip().upper(),
        value=_float_local(elem, "value"),
        shares_or_principal=shares,
        share_principal_type=(share_type.upper() if share_type else None),
        put_call=_norm_put_call(_text_local(elem, "putCall")),
        figi=_text_local(elem, "figi"),
        investment_discretion=_text_local(elem, "investmentDiscretion"),
        voting_sole=v_sole,
        voting_shared=v_shared,
        voting_none=v_none,
        other_managers=tuple(other_managers),
    )


def parse_information_table(
    xml_text: str, *, accession: str, form_type: str, is_notice: bool = False,
) -> ParsedFiling:
    """Parse an information-table XML string into a :class:`ParsedFiling`.

    Never raises: malformed XML → empty holdings + ``malformed_xml`` warning.
    A notice (13F-NT) is returned with zero holdings and an explicit flag — it
    legitimately has no information table (NOT a failure).
    """
    warnings: list[str] = []
    if is_notice:
        return ParsedFiling(accession=accession, form_type=form_type,
                            holdings=(), parse_warnings=("notice_no_information_table",),
                            is_notice=True)
    if not xml_text or not xml_text.strip():
        return ParsedFiling(accession=accession, form_type=form_type, holdings=(),
                            parse_warnings=("empty_document",))
    try:
        root = DET.fromstring(xml_text)
    except (ET.ParseError, DefusedXmlException, ValueError):
        # Malformed OR a rejected XXE/entity-expansion attempt → honest empty.
        return ParsedFiling(accession=accession, form_type=form_type, holdings=(),
                            parse_warnings=("malformed_xml",))

    holdings: list[ParsedHolding] = []
    skipped = 0
    saw_value = False
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        parsed = _parse_info_table(node)
        if parsed is None:
            skipped += 1
            continue
        if parsed.value is not None:
            saw_value = True
        holdings.append(parsed)

    if skipped:
        warnings.append(f"skipped_unusable_rows:{skipped}")
    if not holdings:
        warnings.append("zero_holdings")
    if saw_value:
        # Raw value recorded as-filed; unit (thousands vs dollars) resolved
        # downstream using the filing period. Flag so no layer assumes dollars.
        warnings.append("value_units_ambiguous")

    return ParsedFiling(accession=accession, form_type=form_type,
                        holdings=tuple(holdings), parse_warnings=tuple(warnings))
