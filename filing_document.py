"""Small, bounded XBRL evidence reader for reusable financial-input contracts.

Presentation roles establish statement membership; calculation relationships are
kept separately and must reconcile against numeric contexts before they are used.
No network links in a filing are executed or followed by this parser.
"""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from bs4 import BeautifulSoup


def concept_from_href(href):
    name = href.rsplit("#", 1)[-1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*_[A-Za-z][A-Za-z0-9_.-]*", name):
        return None
    prefix, local = name.split("_", 1)
    return prefix + ":" + local


def norm(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


def _document_date(value):
    value = re.sub(r"\s*,\s*", ", ", value)
    for fmt in ["%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%b. %d, %Y"]:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Unsupported annual document date")


def read_inline(html, cik, end):
    # lxml keeps the full filing tree much smaller than a BeautifulSoup wrapper
    # around every prose node. External entities and network DTDs are disabled.
    from lxml import etree

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, recover=False, huge_tree=True
    )
    try:
        root = etree.fromstring(html.encode(), parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError("Malformed inline-XBRL source document") from exc

    def local(node):
        return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""

    def text(node):
        return "".join(node.itertext()).strip()

    tags = defaultdict(list)
    for node in root.iter():
        tags[local(node)].append(node)
    meta = {}
    for key in [
        "dei:DocumentType",
        "dei:DocumentPeriodEndDate",
        "dei:EntityCentralIndexKey",
    ]:
        values = {
            " ".join(text(x).split())
            for x in tags["nonNumeric"]
            if x.get("name") == key
        }
        if len(values) != 1:
            raise ValueError("Ambiguous or missing annual document identity")
        meta[key] = values.pop()
    if (
        meta["dei:DocumentType"] != "10-K"
        or _document_date(meta["dei:DocumentPeriodEndDate"]) != end
        or meta["dei:EntityCentralIndexKey"].lstrip("0") != cik.lstrip("0")
    ):
        raise ValueError("Annual document identity does not match SEC metadata")
    contexts = {}
    for context in tags["context"]:
        children = defaultdict(list)
        for node in context.iter():
            children[local(node)].append(node)
        ident = children["identifier"][0] if len(children["identifier"]) == 1 else None
        instants = children["instant"]
        starts = children["startDate"]
        finishes = children["endDate"]
        if (
            ident is None
            or text(ident).lstrip("0") != cik.lstrip("0")
            or children["typedMember"]
        ):
            continue
        if not re.fullmatch(r"https?://www\.sec\.gov/CIK", ident.get("scheme", "")):
            continue
        members = children["explicitMember"]
        dims = {m.get("dimension"): text(m) for m in members}
        if len(dims) != len(members):
            continue
        if not (
            (len(instants) == 1 and not starts and not finishes)
            or (not instants and len(starts) == len(finishes) == 1)
        ):
            continue
        try:
            pe = date.fromisoformat(text((instants or finishes)[0]))
            begins = date.fromisoformat(text(starts[0])) if starts else None
        except ValueError:
            continue
        if pe > end or (
            not instants and (begins is None or not 330 <= (pe - begins).days <= 380)
        ):
            continue
        row = {
            "end": str(pe),
            "start": str(begins) if begins else None,
            "instant": bool(instants),
            "dimensions": dims,
        }
        key = context.get("id")
        if key in contexts and contexts[key] != row:
            raise ValueError("Conflicting duplicate XBRL context")
        contexts[key] = row
    units = {}
    for unit_node in tags["unit"]:
        measures = [n for n in unit_node.iter() if local(n) == "measure"]
        divided = any(local(n) == "divide" for n in unit_node.iter())
        measure = text(measures[0]) if len(measures) == 1 and not divided else None
        unit = {"iso4217:USD": "USD", "xbrli:shares": "shares"}.get(measure)
        if divided:
            numerators = [n for n in unit_node.iter() if local(n) == "unitNumerator"]
            denominators = [
                n for n in unit_node.iter() if local(n) == "unitDenominator"
            ]
            if len(numerators) == len(denominators) == 1:
                numerator = [
                    text(n) for n in numerators[0].iter() if local(n) == "measure"
                ]
                denominator = [
                    text(n) for n in denominators[0].iter() if local(n) == "measure"
                ]
                if (
                    numerator == ["iso4217:USD"]
                    and denominator == ["xbrli:shares"]
                    and len(measures) == 2
                ):
                    unit = "USD/shares"
        key = unit_node.get("id")
        if key in units and units[key] != unit:
            raise ValueError("Conflicting duplicate XBRL unit")
        units[key] = unit
    rows = []
    for fact in tags["nonFraction"]:
        ctx = contexts.get(fact.get("contextRef"))
        unit = units.get(fact.get("unitRef"))
        fmt = norm(fact.get("format", "").split(":")[-1])
        hidden = any(local(n) == "hidden" for n in fact.iterancestors())
        nil = fact.get("{http://www.w3.org/2001/XMLSchema-instance}nil")
        if (
            ctx is None
            or unit is None
            or hidden
            or fact.get("continuedAt")
            or nil in {"true", "1"}
            or fmt not in {"", "numdotdecimal", "zerodash", "fixedzero", "numdash"}
        ):
            continue
        raw = text(fact).replace(",", "").replace("\u00a0", "").strip()
        if raw in {"—", "–", "-"} and fmt in {"zerodash", "fixedzero", "numdash"}:
            raw = "0"
        try:
            scale = int(fact.get("scale", "0"))
            if not -12 <= scale <= 12:
                continue
            value = Decimal(raw) * Decimal(10) ** scale
            if fact.get("sign") == "-":
                value = -value
            if not value.is_finite():
                continue
        except (InvalidOperation, ValueError, OverflowError):
            continue
        tr = next((n for n in fact.iterancestors() if local(n) == "tr"), None)
        labels = []
        if tr is not None:
            for cell in tr:
                if local(cell) not in {"td", "th"}:
                    continue
                if any(local(n) == "nonFraction" for n in cell.iter()):
                    break
                label = " ".join(" ".join(cell.itertext()).split())
                if label not in {"", "$", "€", "£", "(", ")"}:
                    labels.append(label)
        rows.append(
            {
                **ctx,
                "concept": fact.get("name"),
                "value": value,
                "unit": unit,
                "label": " ".join(labels),
                "decimals": fact.get("decimals", ""),
                "scale": str(scale),
                "context_id": fact.get("contextRef"),
            }
        )
    return rows


def relationships(xml, kind):
    soup = BeautifulSoup(xml, "xml")
    out = {}
    for link in soup.find_all(kind + "Link"):
        role = link.get("xlink:role")
        loc = {
            locator.get("xlink:label"): concept_from_href(locator.get("xlink:href", ""))
            for locator in link.find_all("loc")
        }
        candidates = defaultdict(list)
        for arc in link.find_all(kind + "Arc"):
            parent, child = loc.get(arc.get("xlink:from")), loc.get(arc.get("xlink:to"))
            if not parent or not child:
                continue
            try:
                priority = int(arc.get("priority", "0"))
                weight = Decimal(arc.get("weight", "1"))
                order = Decimal(arc.get("order", "0"))
            except (InvalidOperation, ValueError):
                raise ValueError("Invalid XBRL relationship")
            candidates[(parent, child)].append(
                (priority, arc.get("use") == "prohibited", weight, order)
            )
        edges = defaultdict(list)
        for (parent, child), options in candidates.items():
            priority = max(o[0] for o in options)
            chosen = [o for o in options if o[0] == priority]
            if any(o[1] for o in chosen):
                continue
            if len({o[2] for o in chosen}) != 1 or chosen[0][2] not in {
                Decimal(-1),
                Decimal(1),
            }:
                raise ValueError("Conflicting or unsupported calculation weights")
            edges[parent].append((child, chosen[0][2], min(o[3] for o in chosen)))
        out[role] = dict(edges)
    return out


class FilingDocument:
    def __init__(self, html, cik, end, linked):
        self.rows = read_inline(html, cik, end)
        self.index = defaultdict(list)
        for row in self.rows:
            self.index[
                (row["concept"], row["end"], row["unit"], row["instant"])
            ].append(row)
        self.calculations = relationships(linked["calculation"], "calculation")
        self.presentations = relationships(linked["presentation"], "presentation")
        schema = BeautifulSoup(linked["schema"], "xml")
        self.kinds = {}
        self.concepts = {}
        for role in schema.find_all("roleType"):
            definition = role.find("definition")
            text = definition.get_text(" ", strip=True) if definition else ""
            parts = re.split(r"\s+[-–]\s+", text)
            if (
                len(parts) < 3
                or parts[1].lower() != "statement"
                or "parenthetical" in text.lower()
            ):
                continue
            uri = role.get("roleURI")
            edges = self.presentations.get(uri, {})
            concepts = set(edges) | {
                c for children in edges.values() for c, _, _ in children
            }
            kind = None
            if "us-gaap:NetCashProvidedByUsedInOperatingActivities" in concepts:
                kind = "cash_flow_statement"
            elif "us-gaap:Assets" in concepts and (
                "us-gaap:LiabilitiesAndStockholdersEquity" in concepts
                or "us-gaap:StockholdersEquity" in concepts
            ):
                kind = "balance_sheet"
            elif concepts & {
                "us-gaap:NetIncomeLoss",
                "us-gaap:ProfitLoss",
                "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
            } and any(w in norm(text) for w in ["income", "operations", "earnings"]):
                kind = "income_statement"
            if kind:
                self.kinds[uri] = kind
                self.concepts[uri] = concepts

    def roles(self, kind):
        return [r for r, k in self.kinds.items() if k == kind]

    def member(self, concept, kind):
        return any(concept in self.concepts[r] for r in self.roles(kind))

    def fact(
        self, concept, pe, *, unit="USD", instant=False, dimensions=None, kind=None
    ):
        if kind and not self.member(concept, kind):
            return None
        selected = [
            r
            for r in self.index.get((concept, str(pe), unit, instant), [])
            if r["dimensions"] == (dimensions or {})
        ]
        if not selected or len({r["start"] for r in selected}) != 1:
            return None

        # A filing can repeat a statement number in rounded narrative text.
        # Select the unique most-precise value only when every less-precise
        # duplicate is compatible with its declared rounding interval.
        def precision(row):
            value = row["decimals"]
            if value == "INF":
                return 100
            try:
                parsed = int(value)
                return parsed if -18 <= parsed <= 18 else None
            except (ValueError, TypeError):
                return None

        if any(precision(r) is None for r in selected):
            return None
        best = max(precision(r) for r in selected)
        precise = [r for r in selected if precision(r) == best]
        if len({r["value"] for r in precise}) != 1:
            return None
        chosen = max(precise, key=lambda r: bool(r["label"]))
        for row in selected:
            tolerance = (
                Decimal(0)
                if precision(row) == 100
                else Decimal(10) ** -precision(row) / 2
            )
            if abs(row["value"] - chosen["value"]) > tolerance:
                return None
        return {
            **chosen,
            "compatible_values": sorted({str(r["value"]) for r in selected}),
        }

    def children(self, role, parent):
        # This standard nonnumeric disclosure concept can occur in calculation
        # linkbases. It is not a recognized monetary liability or a zero fact.
        return [
            r
            for r in self.calculations.get(role, {}).get(parent, [])
            if r[0] != "us-gaap:CommitmentsAndContingencies"
        ]

    def role_fact(self, concept, pe, role, *, instant=False):
        direct = self.fact(concept, pe, instant=instant)
        if direct is not None:
            return direct
        if self.kinds.get(role) != "balance_sheet" or not instant:
            return None
        # Some primary balance sheets present business-group components instead
        # of a consolidated fact. Only the explicitly presented member set of
        # this one axis can be summed, and callers must reconcile the parent.
        axis = "srt:ProductOrServiceAxis"
        edges = self.presentations.get(role, {})
        members = set()
        seen = set()

        def visit(node):
            if node in seen:
                return
            seen.add(node)
            if node.endswith("Member"):
                members.add(node)
            for child, _, _ in edges.get(node, []):
                visit(child)

        visit(axis)
        if not 2 <= len(members) <= 12:
            return None
        components = [
            self.fact(concept, pe, instant=True, dimensions={axis: member})
            for member in sorted(members)
        ]
        if any(row is None for row in components):
            return None
        value = sum((row["value"] for row in components), Decimal(0))
        return {
            **components[0],
            "value": value,
            "dimensions": {},
            "components": components,
            "label": "Sum of presented business-group amounts",
            "aggregation_axis": axis,
        }

    def reconcile(self, role, parent, pe, *, instant=False):
        root = self.role_fact(parent, pe, role, instant=instant)
        children = self.children(role, parent)
        if root is None or not children:
            return None
        rows = [self.role_fact(c, pe, role, instant=instant) for c, _, _ in children]
        if any(r is None for r in rows):
            return None
        value = sum(
            (r["value"] * w for r, (_, w, _) in zip(rows, children)), Decimal(0)
        )
        if value != root["value"]:
            return None
        return [root, *rows]
