"""
Vendii service plan PDF parser.
Extracts plan metadata, package summary (page 2), and per-machine orders (pages 3+).

Real PDF structure (confirmed from Vendii export):
  Page 1: plan name on line after "Plan name" header; machine table with route column
  Page 2: package summary table (SKU | name | unit | total qty | bags)
  Pages 3+: one machine per page — UUID line, position/time/route, machine ID + location,
             ingredient slots, then notes section at the bottom
"""

import re
import pdfplumber
from dataclasses import dataclass, field


@dataclass
class Ingredient:
    sku: str
    name: str
    slot: str
    qty: str


@dataclass
class Machine:
    order: int
    machine_id: str
    location_name: str
    route: str
    arrival_time: str
    notes: str
    ingredients: list[Ingredient] = field(default_factory=list)


@dataclass
class PackageItem:
    sku: str
    name: str
    unit: str
    total_qty: str
    bags: int


@dataclass
class Plan:
    plan_name: str
    plan_id: str
    staff_name: str
    date: str
    route: str
    machines: list[Machine]
    package_summary: list[PackageItem]


def parse_plan_pdf(pdf_path: str) -> Plan:
    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages
        page1_text = pages[0].extract_text() or ""
        page2_text = pages[1].extract_text() or "" if len(pages) > 1 else ""
        machine_pages = [p.extract_text() or "" for p in pages[2:]]

        metadata = _parse_page1(page1_text)
        package_summary = _parse_page2(page2_text)
        machines = _parse_machine_pages(machine_pages)

    return Plan(
        plan_name=metadata.get("plan_name", ""),
        plan_id=metadata.get("plan_id", ""),
        staff_name=metadata.get("staff_name", ""),
        date=metadata.get("date", ""),
        route=metadata.get("route", ""),
        machines=machines,
        package_summary=package_summary,
    )


def _parse_page1(text: str) -> dict:
    """
    Page 1 layout (confirmed):
      Line N:   "Plan name Plan route map"
      Line N+1: "P'Know 11/02"                ← plan name / staff identifier
      ...
      Line M:   "P163482 5 Burwood-Glen Waverly, Main Warehouse 10/02/2026"
                                               ← plan ID + date on same line
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    metadata: dict = {}

    # Plan name: line immediately after the "Plan name" header
    for i, line in enumerate(lines):
        if re.match(r"Plan\s+name\b", line, re.IGNORECASE) and i + 1 < len(lines):
            metadata["plan_name"] = lines[i + 1]
            break

    # Staff name: extracted from plan name "P'Know 11/02" → "P'Know"
    # Full name mapping comes from roster sheet in Step 4
    if "plan_name" in metadata:
        m = re.match(r"(P'\w+)", metadata["plan_name"])
        metadata["staff_name"] = m.group(1) if m else re.sub(r"\s+\d+/\d+.*$", "", metadata["plan_name"]).strip()

    # Plan ID: P followed by exactly 6 digits
    for line in lines:
        m = re.search(r"\b(P\d{6})\b", line)
        if m:
            metadata["plan_id"] = m.group(1)
            break

    # Date: first dd/mm/yyyy in the document
    for line in lines:
        m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", line)
        if m:
            parts = m.group(1).split("/")
            metadata["date"] = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            break

    # Route: derived from machine pages in brief_generator; leave empty here
    metadata["route"] = ""

    return metadata


def _parse_page2(text: str) -> list[PackageItem]:
    """
    Page 2 layout (confirmed):
      SKU  Ingredient  Unit  Total Qty  Packed
      04-03-01-0001  Strong Roasted Coffee  250g/bag  1000g  4
      tbd-gt  Green tea  100g/bag  200g  2

    SKUs can be numeric (04-03-01-0001) or tbd-prefixed (tbd-gt, tbd-whey).
    The last column (Packed) is the bag count.
    """
    items: list[PackageItem] = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    for line in lines:
        # Match both standard and tbd SKU formats
        m = re.match(
            r"^([\w][\w-]+)\s+(.+?)\s+([\d]+\S+/\S+)\s+(\S+)\s+(\d+)$",
            line,
        )
        if m and re.match(r"(\d{2}-\d{2}|tbd-)", m.group(1)):
            try:
                items.append(PackageItem(
                    sku=m.group(1),
                    name=m.group(2).strip(),
                    unit=m.group(3),
                    total_qty=m.group(4),
                    bags=int(m.group(5)),
                ))
            except ValueError:
                pass

    return items


def _parse_machine_pages(pages_text: list[str]) -> list[Machine]:
    """
    Each page 3+ corresponds to one machine. Confirmed layout:

      Order number {UUID}                          ← skip
      P'Know 11/02 (11 Items) 10/02/2026
      Route Position Time to Route Plan ID         ← header, skip
      1/5 11:34 Eastern P163482                    ← order, time, route
      Machine ID Location Name                     ← header, skip
      210332 Cabrini Malvern ICU                   ← machine_id + location_name
      Slot Ingredient Unit Total Qty Packed Filled ← header, skip
      C1 05-03-01-0003 Paper Cup ... 60pcs 4       ← ingredients
      Notes Packed by Filled by                    ← notes section header
      Open 24/7 .......                            ← real note (or just dots if none)
    """
    machines: list[Machine] = []
    _UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    _POS_RE = re.compile(r"^(\d+)/(\d+)\s+(\d{1,2}:\d{2})\s+(.+?)\s+(P\d{6})")
    _MACHINE_RE = re.compile(r"^(\d{6,})\s+(.+)$")
    _NOTES_HDR_RE = re.compile(r"Notes\s+Packed by", re.IGNORECASE)
    _SKIP_LINE_RE = re.compile(
        r"^(Route\s+Position|Machine ID\s+Location|Slot\s+Ingredient|Packed date|Fill date)",
        re.IGNORECASE,
    )

    for text in pages_text:
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        order = 0
        machine_id = ""
        location_name = ""
        route = ""
        arrival_time = ""
        notes = ""
        ingredients: list[Ingredient] = []

        i = 0
        while i < len(lines):
            line = lines[i]

            # Skip UUID line and known header/label lines
            if _UUID_RE.search(line) or _SKIP_LINE_RE.match(line):
                i += 1
                continue

            # Position / time / route line: "1/5 11:34 Eastern P163482"
            pos_m = _POS_RE.match(line)
            if pos_m:
                order = int(pos_m.group(1))
                arrival_time = pos_m.group(3)
                route = pos_m.group(4).strip()
                i += 1
                # Route can wrap to next line e.g. "Burwood-Glen\nWaverly"
                if i < len(lines):
                    nxt = lines[i]
                    if nxt and not re.match(r"Machine ID|\d+/\d+|\d{6,}|Order number", nxt, re.IGNORECASE):
                        route = route + " " + nxt
                        i += 1
                continue

            # Machine ID + location: "210332 Cabrini Malvern ICU"
            machine_m = _MACHINE_RE.match(line)
            if machine_m and not machine_id:
                machine_id = machine_m.group(1)
                location_name = machine_m.group(2).strip()
                i += 1
                continue

            # Notes section: extract real note text, ignore dot placeholders
            if _NOTES_HDR_RE.match(line):
                i += 1
                if i < len(lines):
                    candidate = lines[i]
                    # Strip the dot fill columns used as signature lines
                    clean = re.sub(r"\.{3,}.*$", "", candidate).strip()
                    if clean and not re.match(r"Packed date|Fill date", clean, re.IGNORECASE):
                        notes = clean
                continue

            # Ingredient line: slot + SKU + name + unit + qty + packed [+ filled]
            ingr_m = re.match(
                r"^([A-Z]+\d*)\s+([\w][\w-]+)\s+(.+?)\s+(\d[\w/]+)\s+(\S+)\s+\d+(?:\s+\d+)?$",
                line,
            )
            if ingr_m and re.match(r"(\d{2}-\d{2}|tbd-)", ingr_m.group(2)):
                ingredients.append(Ingredient(
                    sku=ingr_m.group(2),
                    name=ingr_m.group(3).strip(),
                    slot=ingr_m.group(1),
                    qty=ingr_m.group(5),
                ))

            i += 1

        if machine_id:
            machines.append(Machine(
                order=order,
                machine_id=machine_id,
                location_name=location_name,
                route=route,
                arrival_time=arrival_time,
                notes=notes,
                ingredients=ingredients,
            ))

    return machines


# ── CLI test runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python parsers/plan_pdf.py <path-to-plan.pdf>")
        sys.exit(1)

    plan = parse_plan_pdf(sys.argv[1])

    output = {
        "plan_name": plan.plan_name,
        "plan_id": plan.plan_id,
        "staff_name": plan.staff_name,
        "date": plan.date,
        "route": plan.route,
        "machines": [
            {
                "order": m.order,
                "machine_id": m.machine_id,
                "location_name": m.location_name,
                "route": m.route,
                "arrival_time": m.arrival_time,
                "notes": m.notes,
                "ingredients": [
                    {"sku": i.sku, "name": i.name, "slot": i.slot, "qty": i.qty}
                    for i in m.ingredients
                ],
            }
            for m in plan.machines
        ],
        "package_summary": [
            {
                "sku": p.sku,
                "name": p.name,
                "unit": p.unit,
                "total_qty": p.total_qty,
                "bags": p.bags,
            }
            for p in plan.package_summary
        ],
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))
