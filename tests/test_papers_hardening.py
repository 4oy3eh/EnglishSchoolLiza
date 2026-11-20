"""Papers — reviewer hardening (phase 16).

* A partial B2-C2 attempt has no overall average / overall Cambridge score (per-skill
  values only, «общий балл не считается — сданы не все части») — detail, print
  report, CSV; a whole-test attempt keeps its overall.
* Manual marks: an item of an unserved part is refused with a clear Russian error;
  choose-one writing max only within the served sections.
* Start vs the teacher's papers change: interleavings (change before the claim is
  served; after it -> 409).
* save / audio-progress of an unserved part refused over HTTP; a partial attempt
  abandoned past its deadline is expired + finalized by the recovery sweep over the
  served sections only.
* The roster row warns when the stored choice no longer matches the test.
* teacher.js renderers escape paper labels, picture names and alt texts (XSS).
"""

# ruff: noqa: F811  (pytest fixtures imported from tests.test_papers are re-bound as params)
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import build_admin_service
from app.admin.service import (
    OVERALL_PARTIAL_NOTE,
    PAPERS_GONE_WARNING,
    AdminStateError,
    AdminValidationError,
)
from app.analysis import MockAnalysisLLM
from app.content import FilesystemStorage
from app.delivery import DeliveryService
from app.persistence import models as pm
from app.persistence.repository import AttemptRepository, ContentRepository
from tests.test_papers import (  # noqa: F401  (pytest fixtures)
    _add,
    _no_background_finalize,
    _seed,
    _start,
    a2_test,
    auth,
    b2_test,
    client,
    storage,
)
from tests.test_results import _fake_scope

ROOT = Path(__file__).resolve().parents[1]
TEACHER_JS = ROOT / "apps" / "web" / "teacher.js"


def _finished(session: Session, storage: FilesystemStorage, test, papers):  # noqa: ANN001
    _seed(session, test, {"e1": papers, "e2": None})
    repo = AttemptRepository(session)
    delivery = DeliveryService(ContentRepository(session), repo)
    ids = []
    for eid in ("e1", "e2"):
        a = delivery.start(eid)
        delivery.submit(a.id)
        ids.append(a.id)
    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    for aid in ids:
        svc.regrade(aid)
    session.commit()
    return svc, ids


# --------------------------------------------------------------------------- #
# 1. No overall for a partial B2-C2 attempt.
# --------------------------------------------------------------------------- #
def test_partial_b2_has_no_overall_but_whole_test_does(
    session: Session, storage: FilesystemStorage
) -> None:
    svc, (partial_id, whole_id) = _finished(session, storage, b2_test(), ["listening"])
    part = svc.attempt_result(partial_id)
    assert part.skills_average_percent is None and part.overall_note == OVERALL_PARTIAL_NOTE
    assert part.cambridge_scale.overall is None
    assert [t.skill for t in part.skill_totals] == ["listening"]
    whole = svc.attempt_result(whole_id)
    assert whole.overall_note is None and whole.skills_average_percent is not None
    assert whole.cambridge_scale.overall is not None or whole.cambridge_scale.overall_cefr is None
    rep = svc.student_report(partial_id)
    assert rep.skills_average_percent is None and rep.overall_note == OVERALL_PARTIAL_NOTE
    assert svc.student_report(whole_id).overall_note is None

    lines = [ln.split(",") for ln in svc.results_csv("pb").split("\r\n") if ln]
    header, rows = lines[0], lines[1:]
    avg, parts = header.index("skills_avg_percent"), header.index("parts")
    by_parts = {r[parts]: r for r in rows}
    assert by_parts["Частично: Listening"][avg] == ""
    assert by_parts["все части"][avg] != ""


def test_dashboard_renders_the_note_instead_of_an_overall() -> None:
    src = TEACHER_JS.read_text(encoding="utf-8")
    assert "d.overall_note" in src and "Общий балл:" in src
    report = (ROOT / "apps" / "web" / "teacher-report.html").read_text(encoding="utf-8")
    assert "r.overall_note" in report


# --------------------------------------------------------------------------- #
# 2. Manual marks only for served items.
# --------------------------------------------------------------------------- #
def test_manual_grade_refused_for_an_unserved_item(
    session: Session, storage: FilesystemStorage
) -> None:
    svc, (partial_id, whole_id) = _finished(session, storage, b2_test(), ["listening"])
    for item in ("pb-w2a", "pb-w1a", "pb-q1"):
        with pytest.raises(AdminValidationError, match="не входило в выданные"):
            svc.set_manual_grade(partial_id, item, 1)
    attempt = AttemptRepository(session).get_attempt(partial_id)
    assert svc._item_max_points(attempt, "pb-w2a") is None
    # The served listening item and the whole-test attempt still take marks.
    assert svc.set_manual_grade(partial_id, "pb-q9", 0).grading.max_score == 1
    assert svc.set_manual_grade(whole_id, "pb-w1a", 12).grading.score >= 12


def test_choose_one_max_only_when_writing_is_served(
    session: Session, storage: FilesystemStorage
) -> None:
    _seed(session, b2_test(), {"w": ["writing"]})
    repo = AttemptRepository(session)
    delivery = DeliveryService(ContentRepository(session), repo)
    a = delivery.start("w")
    delivery.save_answer(a.id, "pb-w2a", "First answer " * 20)
    delivery.save_answer(a.id, "pb-w2b", "Second answer " * 20)
    delivery.submit(a.id)
    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    svc.regrade(a.id)
    attempt = repo.get_attempt(a.id)
    # Both written: the uncounted one is still markable out of its group's 20.
    assert svc._item_max_points(attempt, "pb-w2a") == 20
    assert svc._item_max_points(attempt, "pb-w2b") == 20
    assert svc._item_max_points(attempt, "pb-q9") is None  # listening not served


# --------------------------------------------------------------------------- #
# 3. Start vs the teacher's papers change.
# --------------------------------------------------------------------------- #
def test_change_landing_before_the_claim_is_served(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(session, a2_test(), {"e1": None})
    repo = AttemptRepository(session)
    original = AttemptRepository.claim_roster_entry
    order: list[str] = []

    def interleaved(self, entry_id, attempt_id):  # noqa: ANN001
        # The teacher's PUT lands after start() read the entry, before the flip.
        order.append("teacher")
        assert self.set_roster_papers(entry_id, ["listening"]) is True
        order.append("claim")
        return original(self, entry_id, attempt_id)

    monkeypatch.setattr(AttemptRepository, "claim_roster_entry", interleaved)
    now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    a = DeliveryService(ContentRepository(session), repo).start("e1", now=now)
    assert order == ["teacher", "claim"]
    assert a.papers == ["listening"] and a.deadline == now + timedelta(minutes=35)
    assert repo.get_roster_entry("e1").papers == ["listening"]


def test_change_after_the_claim_is_refused(
    session: Session, storage: FilesystemStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(session, a2_test(), {"e1": None})
    repo = AttemptRepository(session)
    original = AttemptRepository.claim_roster_entry
    results: list[bool] = []

    def interleaved(self, entry_id, attempt_id):  # noqa: ANN001
        claimed = original(self, entry_id, attempt_id)
        results.append(self.set_roster_papers(entry_id, ["listening"]))
        return claimed

    monkeypatch.setattr(AttemptRepository, "claim_roster_entry", interleaved)
    a = DeliveryService(ContentRepository(session), repo).start("e1")
    assert results == [False] and a.papers is None
    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    with pytest.raises(AdminStateError):
        svc.set_student_papers("e1", ["listening"])
    assert repo.get_roster_entry("e1").papers is None


def test_lost_claim_resumes_the_winner(session: Session) -> None:
    _seed(session, a2_test(), {"e1": None})
    repo = AttemptRepository(session)
    svc = DeliveryService(ContentRepository(session), repo)
    stale = repo.get_roster_entry("e1")  # read before another start won the entry
    winner = svc.start("e1")
    assert stale.attempt_id is None
    assert repo.claim_roster_entry("e1", "other") is False  # the second claim loses
    assert svc.start("e1").id == winner.id


# --------------------------------------------------------------------------- #
# 4. Unserved part over HTTP; expiry + recovery sweep.
# --------------------------------------------------------------------------- #
def test_unserved_part_refused_over_http(client: TestClient, auth: dict) -> None:  # noqa: F811
    entry = _add(client, auth, "Rita", ["reading_writing"])
    aid, tok = _start(client, entry)
    save = client.put(f"/exam/attempts/{aid}/answers/pa-q6", json={"response": "Tom"}, headers=tok)
    assert save.status_code == 404
    audio = client.post(
        f"/exam/attempts/{aid}/audio-progress",
        json={"asset_id": "pa-l1.mp3", "seconds": 5},
        headers=tok,
    )
    assert audio.status_code == 400
    item = client.get(f"/exam/attempts/{aid}/items/pa-q4", headers=tok)
    assert item.status_code == 404


def test_abandoned_partial_attempt_is_expired_and_finalized_by_the_sweep(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.results import finalize as bg

    monkeypatch.setattr(bg, "session_scope", lambda: _fake_scope(session))
    monkeypatch.setattr(bg, "_writing_grader", lambda: None)
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: None)
    _seed(session, a2_test(), {"e1": ["listening"]})
    repo = AttemptRepository(session)
    delivery = DeliveryService(ContentRepository(session), repo)
    long_ago = datetime.now(UTC) - timedelta(hours=3)
    a = delivery.start("e1", now=long_ago)
    delivery.save_answer(a.id, "pa-q6", "Tom", now=long_ago + timedelta(minutes=1))
    assert a.deadline == long_ago + timedelta(minutes=35)
    session.commit()

    done = bg.sweep_missing_reports()
    assert a.id in done
    assert repo.get_attempt(a.id, fresh=True).status == "expired"
    report = repo.get_report(a.id)
    assert report is not None and report.status == "complete"
    assert {g.item_id for g in report.grading.items} == {"pa-q4", "pa-q5", "pa-q6"}
    assert (report.grading.score, report.grading.max_score) == (1, 3)
    assert {s.skill for s in report.skills} <= {"listening"}


# --------------------------------------------------------------------------- #
# 5. Roster warning when the choice no longer matches the test.
# --------------------------------------------------------------------------- #
def test_roster_warns_when_the_chosen_papers_are_gone(
    session: Session, storage: FilesystemStorage
) -> None:
    _seed(session, a2_test(), {"gone": None, "ok": ["listening"]})
    # A stale choice (e.g. stored before the test was edited): bypasses validation.
    session.get(pm.RosterEntryRow, "gone").papers = ["writing"]
    session.commit()
    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    rows = {r.roster_entry_id: r for r in svc.roster_status("pa")}
    assert rows["gone"].papers_warning == PAPERS_GONE_WARNING
    assert rows["gone"].papers is None and rows["gone"].minutes == 95
    assert rows["ok"].papers_warning is None and rows["ok"].minutes == 35
    assert "papers_warning" in TEACHER_JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# XSS: paper labels, picture names and alt texts in teacher.js renderers.
# --------------------------------------------------------------------------- #
_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8").replace(/\r\n/g, "\n");
const fn = (name) => {
  const i = src.indexOf(`\nfunction ${name}(`);
  if (i < 0) throw new Error("missing function " + name);
  return src.slice(i, src.indexOf("\n}\n", i) + 3);
};
const cst = (name) => {
  // A one-line arrow ends on its line; a block arrow ("=> {") ends at "\n};\n".
  const i = src.indexOf(`\nconst ${name} = `);
  if (i < 0) throw new Error("missing const " + name);
  const eol = src.indexOf("\n", i + 1);
  const end = src.slice(i, eol).trimEnd().endsWith("{") ? src.indexOf("\n};\n", i) + 3 : eol;
  return src.slice(i, end) + "\n";
};
const P = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const body =
  "let currentPapers = P.papers; let currentTest = { duration_minutes: 95 };\n" +
  [cst("assetUrl"), cst("paperLabel"), cst("papersText")].join("\n") +
  ["esc", "papersMinutes", "papersChooser", "papersCell", "renderStimulusReview"]
    .map(fn).join("\n") +
  "\nreturn { papersChooser, papersCell, renderStimulusReview };";
const api = new Function("P", body)(P);
process.stdout.write(JSON.stringify({
  chooser: api.papersChooser(P.evil, null),
  cell: api.papersCell(P.row),
  locked: api.papersCell({ ...P.row, status: "in_progress", attempt_id: "a1" }),
  stim: api.renderStimulusReview(P.stim),
}));
"""

EVIL = '<img src=x onerror="alert(1)">'
QUOTE = 'x" onerror="alert(2)'


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_teacher_renderers_escape_papers_and_pictures(tmp_path: Path) -> None:
    payload = {
        "papers": [
            {"paper": QUOTE, "label": EVIL, "minutes": EVIL},
            {"paper": "listening", "label": "Listening", "minutes": 35},
        ],
        "evil": QUOTE,
        "row": {
            "status": "not_started",
            "attempt_id": None,
            "papers": [QUOTE],
            "papers_label": EVIL,
            "minutes": EVIL,
            "papers_warning": EVIL,
        },
        "stim": {
            "kind": "matching_pool",
            "text": EVIL,
            "images": [QUOTE],
            "options": [{"key": EVIL, "text": EVIL, "image": QUOTE}],
            "each_option_once": False,
        },
    }
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    data = tmp_path / "p.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(TEACHER_JS), str(data)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    for name, html in out.items():
        assert "<img src=x" not in html, name
        assert 'onerror="' not in html, name
        assert "<script" not in html, name
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in out["cell"]
    # The picture name is percent-encoded into /assets/... (assetUrl), the alt escaped.
    assert "/assets/x%22%20onerror%3D%22alert(2)" in out["stim"]
    assert "🔒" in out["locked"]
