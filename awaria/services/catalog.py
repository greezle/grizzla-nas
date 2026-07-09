"""Error catalog: the text format the printers parse, plus the wizard's
save/reorder operations (ids are persisted in printer EEPROMs)."""
import json


def render_catalog(db):
    """Text format parsed by the printers (common/awaria_catalog.hpp)."""
    seq = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    lines = ["version 1", "seq %s" % (seq["value"] if seq else "1")]
    for d in db.execute("SELECT * FROM error_defs ORDER BY position, id"):
        flags = (1 if d["print_ctx"] else 0) | (2 if d["hidden"] else 0)
        lines.append("e %d %d %d %s" %
                     (d["id"], d["severity"], flags, d["label"]))
        try:
            questions = json.loads(d["questions"])
        except json.JSONDecodeError:
            questions = []
        for question in questions[:2]:
            text = str(question.get("text", "")).strip()
            answers = question.get("answers", [])[:3]
            if not text or len(answers) < 2:
                continue
            lines.append("q %s" % text)
            for a in answers:
                sev = a.get("severity")
                lines.append("a %s %s" % (sev if sev in (0, 1, 2) else "-",
                                          str(a.get("text", "")).strip()))
    lines.append("end")
    return "\n".join(lines) + "\n"


SEVERITY_NAMES = {
    0: "krytyczna (blokuje)",
    1: "operator decyduje",
    2: "tylko notatka"
}


# firmware buffer limits, in BYTES of UTF-8 (Polish letters take 2)
LABEL_MAX_B, QUESTION_MAX_B, ANSWER_MAX_B = 39, 47, 19


def utf8_clamp(text, max_bytes):
    raw = text.strip().encode("utf-8")[:max_bytes]
    while raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw[:-1]
    return ""


def bump_catalog_seq(db):
    row = db.execute(
        "SELECT value FROM meta WHERE key='catalog_seq'").fetchone()
    db.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('catalog_seq', ?)",
        (str(int(row["value"]) + 1 if row else 1), ))


def save_def(db, form):

    def field(name, default=""):
        return (form.get(name) or [default])[0]

    label = utf8_clamp(field("label"), LABEL_MAX_B)
    if not label:
        return None
    severity = int(field("severity", "1"))
    severity = severity if severity in (0, 1, 2) else 1

    questions = []
    for qi in range(2):
        text = utf8_clamp(field(f"q{qi}_text"), QUESTION_MAX_B)
        answers = []
        for ai in range(3):
            a_text = utf8_clamp(field(f"q{qi}_a{ai}_text"), ANSWER_MAX_B)
            if not a_text:
                continue
            a_sev = field(f"q{qi}_a{ai}_sev", "-")
            answer = {"text": a_text}
            if a_sev in ("0", "1", "2"):
                answer["severity"] = int(a_sev)
            answers.append(answer)
        if text and len(answers) >= 2:
            questions.append({"text": text, "answers": answers})

    def_id = field("id")
    values = (label, severity, 1 if "print_ctx" in form else 0,
              1 if "hidden" in form else 0,
              json.dumps(questions, ensure_ascii=False))
    if def_id:
        # position is managed by drag & drop on the list, keep it unchanged
        db.execute(
            "UPDATE error_defs SET label=?, severity=?, print_ctx=?, hidden=?,"
            " questions=? WHERE id=?", values + (int(def_id), ))
    else:
        row = db.execute(
            "SELECT value FROM meta WHERE key='next_error_id'").fetchone()
        new_id = int(row["value"]) if row else 15
        if new_id > 127:
            return None  # firmware slot encoding limit
        position = (db.execute(
            "SELECT COALESCE(MAX(position), 0) + 10 p FROM error_defs").
                    fetchone())["p"]
        db.execute(
            "INSERT INTO error_defs(label, severity, print_ctx, hidden,"
            " questions, position, id) VALUES (?,?,?,?,?,?,?)",
            values + (position, new_id))
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('next_error_id', ?)",
            (str(new_id + 1), ))
    bump_catalog_seq(db)
    db.commit()
    return True


def reorder_defs(db, ids):
    known = {r["id"] for r in db.execute("SELECT id FROM error_defs")}
    ids = [i for i in ids if isinstance(i, int) and i in known]
    if len(ids) != len(known):
        return False  # stale list in the browser - reload
    for position, def_id in enumerate(ids):
        db.execute("UPDATE error_defs SET position=? WHERE id=?",
                   ((position + 1) * 10, def_id))
    bump_catalog_seq(db)
    db.commit()
    return True
