"""
MNR Claw — PDF Filler
Two-pass approach:
  1. PyMuPDF → fills text fields and checkboxes
  2. pikepdf  → sets radio buttons precisely via /AS and /V

Radio selections in data dict use "_radio_select":
  [
    {"field": "Gender", "on_state": "F"},
    {"field": "Yes Being Cared for By a Medical Physician", "on_state": "Yes"},
    {"field": "Required Is this patient pregnant", "on_state": "No"},
    {"field": "Tobacco Use", "on_state": "No_6"}
  ]

  - Grouped radio (parent+kids): key = parent field /T, on_state = kid state string (no slash)
  - Independent radio: key = field /T, on_state = that widget's on_state string (no slash)
  - Unmentioned grouped fields → all kids set to /Off
  - Unmentioned independent fields → set to /Off

Usage: python3 fill_mnr.py <data.json> <output.pdf>
"""

import fitz
import pikepdf
import json
import sys
import os
import tempfile

TEMPLATE = os.path.join(os.path.dirname(__file__), "MNR_template.pdf")

# Fields that tend to have long text — force small font so they stay readable
SMALL_FONT_FIELDS = {
    "Treatment Goals",
    "How will you measure progress toward these goals",
    "Response to most recent Treatment Plan",
    "How has it changed?",
    "How has it changed?#1",
    "How has it changed?#2",
    "Activity#0", "Activity#1", "Activity#2",
    "Measurements", "Measurements#1", "Measurements#2",
    "Chief Complaint(s)", "Chief Complaint(s) 2", "Chief Complaint(s) 3",
    "Cause of Condition/Injury", "Cause of Condition/Injury 2", "Cause of Condition/Injury 3",
    "How long does relief last?", "How long does relief last? 2", "How long does relief last? 3",
    "Observation", "Observation 2", "Observation 3",
    "Other Comments eg Responses to Care Barriers to Progress Patient Health History 1",
    "Other Comments eg Responses to Care Barriers to Progress Patient Health History 2",
    "Conditions",
    "Changes in Pain Medication Use eg name frequency amount dosage",
}
SMALL_FONT_SIZE = 6.5


def fill_text_and_checkboxes(data: dict, tmp_path: str):
    """Pass 1: PyMuPDF fills text fields and checkboxes."""
    # Auto-sync Patient ID and Subscriber ID
    if "Patient ID" in data and "Subscriber ID" not in data:
        data["Subscriber ID"] = data["Patient ID"]
    elif "Subscriber ID" in data and "Patient ID" not in data:
        data["Patient ID"] = data["Subscriber ID"]

    doc = fitz.open(TEMPLATE)
    page = doc[0]

    for widget in page.widgets():
        name = widget.field_name
        ftype = widget.field_type_string

        if ftype == "Text":
            val = data.get(name, "")
            if val:
                widget.field_value = str(val)
                if name in SMALL_FONT_FIELDS:
                    widget.text_fontsize = SMALL_FONT_SIZE
                widget.update()

        elif ftype == "CheckBox":
            val = data.get(name, False)
            widget.field_value = True if val else False
            widget.update()

        # RadioButtons handled in pass 2

    doc.save(tmp_path, deflate=True)
    doc.close()


def inject_empty_off(pdf, ap_n):
    """Replace /Off appearance with empty stream so viewers render nothing when off."""
    ap_n["/Off"] = pikepdf.Stream(pdf, b"")


def fix_radio_buttons(tmp_path: str, data: dict, output_path: str):
    """Pass 2: pikepdf sets radio /AS and /V directly."""
    radio_selections = {}
    for sel in data.get("_radio_select", []):
        radio_selections[sel["field"]] = sel["on_state"]

    pdf = pikepdf.open(tmp_path)
    acroform = pdf.Root.get("/AcroForm")
    if not acroform:
        pdf.save(output_path)
        return

    for field in acroform.get("/Fields", []):
        ft = str(field.get("/FT", ""))
        ff = int(field.get("/Ff", 0))
        is_radio = bool(ff & (1 << 15))
        if ft != "/Btn" or not is_radio:
            continue

        name = str(field.get("/T", ""))
        kids = list(field.get("/Kids", []))
        desired = radio_selections.get(name)

        if kids:
            # Grouped radio (Gender, Pregnant, Tobacco, etc.)
            if desired:
                field["/V"] = pikepdf.Name("/" + desired)
            else:
                field["/V"] = pikepdf.Name("/Off")

            for kid in kids:
                ap = kid.get("/AP")
                if not ap or "/N" not in ap:
                    continue
                n_dict = ap["/N"]
                kid_states = [str(k).lstrip("/") for k in n_dict.keys()]
                non_off = [s for s in kid_states if s != "Off"]
                if non_off and desired and non_off[0] == desired:
                    # This kid is ON
                    kid["/AS"] = pikepdf.Name("/" + non_off[0])
                else:
                    # This kid is OFF — blank out its /Off appearance
                    inject_empty_off(pdf, n_dict)
                    kid["/AS"] = pikepdf.Name("/Off")

        else:
            # Independent radio widget
            ap = field.get("/AP")
            on_st = None
            if ap and "/N" in ap:
                for k in ap["/N"].keys():
                    s = str(k).lstrip("/")
                    if s != "Off":
                        on_st = s
                        break

            if desired and on_st and desired == on_st:
                # Turn ON
                field["/AS"] = pikepdf.Name("/" + on_st)
                field["/V"] = pikepdf.Name("/" + on_st)
            else:
                # Turn OFF — blank out /Off appearance so nothing renders
                if ap and "/N" in ap:
                    inject_empty_off(pdf, ap["/N"])
                field["/AS"] = pikepdf.Name("/Off")
                field["/V"] = pikepdf.Name("/Off")

    pdf.save(output_path)


def bake_visible_layer(input_path: str, output_path: str):
    """
    Overlay field values as static page text so any viewer (WhatsApp, iOS)
    can see them — while keeping all form fields fully editable.
    """
    doc = fitz.open(input_path)
    page = doc[0]

    for widget in page.widgets():
        ftype = widget.field_type_string
        val = widget.field_value
        rect = widget.rect

        if ftype == "Text" and val:
            # Draw the text as static content inside the widget rect
            fontsize = widget.text_fontsize if widget.text_fontsize and widget.text_fontsize > 0 else 7
            # Clip to field rect with a small inset
            inset_rect = fitz.Rect(rect.x0 + 1, rect.y0 + 1, rect.x1 - 1, rect.y1 - 1)
            page.insert_textbox(
                inset_rect,
                val,
                fontsize=fontsize,
                color=(0, 0, 0),
                align=0,
            )

        elif ftype == "CheckBox" and val in (True, "Yes", "/Yes", "On", "/On"):
            # Draw a checkmark at the checkbox center
            cx = (rect.x0 + rect.x1) / 2
            cy = (rect.y0 + rect.y1) / 2
            page.insert_text(
                (rect.x0, rect.y1 - 1),
                "✓",
                fontsize=min(rect.height, rect.width) * 0.9,
                color=(0, 0, 0),
            )

    doc.save(output_path, deflate=True)
    doc.close()


def clear_field_appearances(input_path: str, output_path: str):
    """
    Remove /AP from text & checkbox fields so their appearance doesn't
    render on top of the already-baked static text layer.
    Fields stay fully editable — clicking activates them for typing.
    """
    pdf = pikepdf.open(input_path)
    acroform = pdf.Root.get("/AcroForm")
    if not acroform:
        pdf.save(output_path)
        return

    for field in acroform.get("/Fields", []):
        ft = str(field.get("/FT", ""))
        ff = int(field.get("/Ff", 0))
        is_radio = bool(ff & (1 << 15))

        # Only clear text and checkbox fields — leave radio buttons alone
        if ft == "/Tx" or (ft == "/Btn" and not is_radio):
            if "/AP" in field:
                del field["/AP"]

    # Also clear on page-level annotations directly
    for page in pdf.pages:
        for annot in page.get("/Annots", []):
            ft = str(annot.get("/FT", ""))
            ff = int(annot.get("/Ff", 0))
            is_radio = bool(ff & (1 << 15))
            if ft == "/Tx" or (ft == "/Btn" and not is_radio):
                if "/AP" in annot:
                    del annot["/AP"]

    # Critical: tell all PDF viewers to use our set appearances,
    # not regenerate them from scratch (which would show old template values)
    acroform = pdf.Root.get("/AcroForm")
    if acroform:
        acroform["/NeedAppearances"] = pikepdf.Boolean(False)

    pdf.save(output_path)


def fill_mnr(data: dict, output_path: str):
    tmp_files = []
    def make_tmp():
        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_files.append(tf.name)
        tf.close()
        return tf.name

    try:
        t1 = make_tmp()  # text + checkboxes
        t2 = make_tmp()  # radio buttons fixed
        t3 = make_tmp()  # baked visible layer
        fill_text_and_checkboxes(data, t1)
        fix_radio_buttons(t1, data, t2)
        bake_visible_layer(t2, t3)
        clear_field_appearances(t3, output_path)
        print(f"Saved: {output_path}")
    finally:
        for p in tmp_files:
            if os.path.exists(p):
                os.unlink(p)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 fill_mnr.py <data.json> <output.pdf>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        data = json.load(f)
    fill_mnr(data, sys.argv[2])
