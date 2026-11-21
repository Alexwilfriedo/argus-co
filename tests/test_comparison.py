"""Synthetic campaigns exercised through the PDF renderer."""

import json
import tempfile
import unittest
from pathlib import Path

from test_report import report


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.old = self.root / "before"
        self.new = self.root / "after"
        self.old.mkdir()
        self.new.mkdir()

    def mobile(self, base, statuses, folder="run", device="sim-01", app="local.demo"):
        run = base / folder
        run.mkdir()
        (run / "target.txt").write_text(f"{device}\n{app}\n")
        (run / "exit-code.txt").write_text("1" if "fail" in statuses.values() else "0")
        body = {"pass": "", "fail": "<failure/>", "blocked": "<error/>", "skip": "<skipped/>"}
        cases = "".join(
            f'<testcase name="{name}">{body[status]}</testcase>' for name, status in statuses.items()
        )
        (run / "junit.xml").write_text(f"<testsuite>{cases}</testsuite>")
        return {
            "label": "Mobile",
            "maestro": folder,
            "planned": list(statuses),
            "notes": {name: {"expected": "Règle A"} for name in statuses},
        }

    def campaign(self, base, targets, scope="Recette locale"):
        return {"title": "Synthèse", "scope": scope, "build": base.name, "targets": targets}

    def compare(self, before, after, scope="Recette locale"):
        ref = self.old / "campaign.json"
        ref.write_text(json.dumps(self.campaign(self.old, before, scope)))
        html = report.build_html(self.campaign(self.new, after), self.new, reference=ref)
        return html.split("<h2>Comparaison des campagnes</h2>")[1].split("</section>")[0]

    def test_stable_transitions_and_sources(self):
        first = self.mobile(self.old, {"A": "pass", "B": "fail", "C": "fail", "D": "pass"})
        last = self.mobile(self.new, {"A": "fail", "B": "pass", "C": "fail", "D": "pass"})
        html = self.compare([first], [last])
        for verdict in ("Régression", "Correction", "Défaut persistant", "Succès maintenu"):
            self.assertIn(">" + verdict + "<", html)
        self.assertIn("../before/run/junit.xml", html)
        self.assertIn("run/junit.xml", html)

    def test_unmatched_identity_and_missing_or_changed_expectations(self):
        old = self.mobile(self.old, {"Old": "fail", "Legacy": "fail", "Changed": "fail", "Steps": "fail"})
        new = self.mobile(self.new, {"New": "pass", "Legacy": "pass", "Changed": "pass", "Steps": "pass"})
        old["notes"].pop("Legacy")
        new["notes"]["Changed"]["expected"] = "Règle B"
        new["notes"]["Steps"]["repro"] = "Hors ligne"
        html = self.compare([old], [new])
        for reason in (
            "sans correspondance",
            "Attendu absent",
            "Attendu modifié",
            "conditions de reproduction",
        ):
            self.assertIn(reason, html)
        self.assertIn("Régressions · 0 | Corrections · 0", html)

    def test_actual_target_is_required(self):
        old = self.mobile(self.old, {"A": "fail"})
        new = self.mobile(self.new, {"A": "pass"})
        for recorded in ("sim-02\nlocal.demo", "sim-01\nlocal.other", ""):
            with self.subTest(recorded=recorded):
                (self.new / "run/target.txt").write_text(recorded)
                html = self.compare([old], [new])
                self.assertIn("Régressions · 0 | Corrections · 0", html)
                self.assertIn("sans correspondance", html)

    def test_blocked_case_conflict_and_partial_run(self):
        old = self.mobile(self.old, {"Good": "fail", "Blocked": "fail", "Conflict": "fail"})
        new = self.mobile(self.new, {"Good": "pass", "Blocked": "blocked", "Conflict": "pass"})
        new["notes"]["Conflict"]["conflict"] = "<contrat>"
        broken = self.mobile(self.new, {"Other": "fail"}, folder="broken")
        (self.new / "broken/junit.xml").write_text("<testsuite>")
        for target in ([broken, new], [new, broken]):
            html = self.compare([old], target)
            self.assertIn("Corrections · 1", html)
            for reason in ("illisible", "Non vérifié", "à arbitrer", "&lt;contrat&gt;"):
                self.assertIn(reason, html)

    def test_retry_disagreement_and_unreadable_retry(self):
        old = self.mobile(self.old, {"A": "pass"})
        new = self.mobile(self.new, {"A": "pass"})
        self.mobile(self.new, {"A": "fail"}, folder="retry")
        for runs in (["run", "retry"], ["retry", "run"], ["retry", "missing"]):
            new["maestro"] = runs
            html = self.compare([old], [new])
            self.assertIn("Régressions · 0 | Corrections · 0", html)
            self.assertRegex(html, "instable|illisible")

    def web(self, base, tests):
        specs = []
        names = []
        for title, project, outcome, attempts in tests:
            specs.append(
                {
                    "title": title,
                    "tests": [
                        {
                            "projectName": project,
                            "status": outcome,
                            "results": [{"status": a} for a in attempts],
                        }
                    ],
                }
            )
            names.append(" › ".join(filter(None, [project, "suite", title])))
        (base / "pw.json").write_text(json.dumps({"suites": [{"title": "suite", "specs": specs}]}))
        return {
            "label": "Web",
            "playwright": "pw.json",
            "planned": names,
            "notes": {name: {"expected": "Message visible"} for name in names},
        }

    def test_web_stability_requires_raw_attempt_and_named_project(self):
        rows = [
            ("Valid", "Firefox", "unexpected", ["failed"]),
            ("Empty", "Firefox", "unexpected", []),
            ("Stopped", "Firefox", "unexpected", ["interrupted"]),
            ("Skipped", "Firefox", "skipped", []),
            ("Unstable", "Firefox", "flaky", ["failed", "passed"]),
            ("Mixed", "Firefox", "unexpected", ["passed", "failed"]),
            ("Unnamed", "", "unexpected", ["failed"]),
        ]
        new = self.web(self.new, rows)
        old = self.web(self.old, [(title, project, "expected", ["passed"]) for title, project, _, _ in rows])
        html = self.compare([old], [new])
        self.assertIn("Régressions · 1 | Corrections · 0", html)
        self.assertIn("Non comparables · 6", html)
        self.assertIn("../before/pw.json", html)
        self.assertIn("Candidat · Échec · pw.json", html)
        self.assertIn("Identité de la cible absente", html)

    def test_different_scope_or_project_and_unreadable_web_source(self):
        old = self.web(self.old, [("Case", "Chrome", "expected", ["passed"])])
        new = self.web(self.new, [("Case", "Chrome", "unexpected", ["failed"])])
        self.assertIn("Périmètre", self.compare([old], [new], scope="Autre environnement"))
        new = self.web(self.new, [("Case", "Firefox", "unexpected", ["failed"])])
        self.assertIn("Régressions · 0", self.compare([old], [new]))
        (self.new / "pw.json").write_text('{"suites":')
        self.assertIn("illisible", self.compare([old], [new]))

    def test_observations_are_excluded_and_old_export_has_no_comparison(self):
        item = {"name": "Manuel", "status": "pass", "evidence": "trace.txt"}
        target = {"label": "QA", "planned": ["Manuel"], "observations": [item]}
        for base in (self.old, self.new):
            (base / "trace.txt").write_text("synthetic")
        html = self.compare([target], [target])
        self.assertIn("Observation déclarée", html)
        self.assertIn("Régressions · 0 | Corrections · 0", html)
        single = report.build_html(self.campaign(self.new, [target]), self.new)
        self.assertNotIn("Comparaison des campagnes", single)
        self.assertIn("Parcours vérifiés", single)

    def test_build_mismatch_and_ambiguous_name(self):
        old = self.mobile(self.old, {"A": "fail"})
        new = self.mobile(self.new, {"A": "pass"})
        (self.new / "run/build.txt").write_text("wrong")
        self.assertIn("diffère du manifeste", self.compare([old], [new]))
        (self.new / "run/build.txt").write_text(self.new.name)
        duplicate = self.mobile(self.new, {"A": "fail"}, folder="repeat")
        self.assertIn("Plusieurs résultats", self.compare([old], [new, duplicate]))

    def test_malformed_target_does_not_hide_verified_peer(self):
        old = self.mobile(self.old, {"Good": "fail"})
        new = self.mobile(self.new, {"Good": "pass"})
        bad = {"planned": ["Sans source"], "label": "Autre"}
        html = self.compare([bad, old], [new, bad])
        self.assertIn("Corrections · 1", html)
        self.assertIn("Sans source", html)
        self.assertIn("Cible inexploitable", html)

    def test_driver_error_is_not_a_correction(self):
        old = self.mobile(self.old, {"A": "fail"})
        new = self.mobile(self.new, {"A": "pass"})
        (self.new / "run/exit-code.txt").write_text("1")
        html = self.compare([old], [new])
        self.assertIn("Corrections · 0", html)
        self.assertIn("erreur sans cas correspondant", html)

    def test_unreadable_junit_keeps_device_identity_and_vetoes_other_copy(self):
        old = self.mobile(self.old, {"A": "fail"})
        new = self.mobile(self.new, {"A": "pass"})
        broken = self.mobile(self.new, {"A": "pass"}, folder="extra")
        (self.new / "extra/junit.xml").write_text("<testsuite>")
        for targets in ([broken], [broken, new], [new, broken]):
            with self.subTest(targets=targets):
                html = self.compare([old], targets)
                self.assertIn("Corrections · 0", html)
                self.assertIn("Non comparables · 1", html)
                self.assertIn("illisible", html)

    def test_same_build_cannot_establish_transition(self):
        old = self.mobile(self.old, {"A": "pass", "B": "fail"})
        new = self.mobile(self.new, {"A": "fail", "B": "pass"})
        ref = self.old / "campaign.json"
        ref.write_text(json.dumps(self.campaign(self.new, [old])))
        html = report.build_html(self.campaign(self.new, [new]), self.new, reference=ref)
        self.assertIn("Régressions · 0 | Corrections · 0", html)
        self.assertIn("même build", html)

    def test_web_errors_on_attempt_or_source(self):
        names = ("Good", "Problem")
        old = self.web(self.old, [(n, "Chrome", "unexpected", ["failed"]) for n in names])
        new = self.web(self.new, [(n, "Chrome", "expected", ["passed"]) for n in names])
        path = self.new / "pw.json"
        data = json.loads(path.read_text())
        attempt = data["suites"][0]["specs"][1]["tests"][0]["results"][0]
        for key, value in (("errors", [{"message": "synthetic"}]), ("error", {"message": "synthetic"})):
            with self.subTest(key=key):
                attempt[key] = value
                path.write_text(json.dumps(data))
                html = self.compare([old], [new])
                self.assertIn("Corrections · 1", html)
                self.assertIn("erreur", html)
                attempt.pop(key)
        path = self.old / "pw.json"
        data = json.loads(path.read_text())
        data["errors"] = [{"message": "fixture"}]
        path.write_text(json.dumps(data))
        html = self.compare([old], [new])
        self.assertIn("Corrections · 0", html)
        self.assertIn("erreur globale", html)

    def test_web_collision_and_malformed_attempt_preserve_peer(self):
        names = ("Problem", "Good")
        old = self.web(self.old, [(n, "Chrome", "expected", ["passed"]) for n in names])
        for collision in (True, False):
            with self.subTest(collision=collision):
                new = self.web(self.new, [(n, "Chrome", "unexpected", ["failed"]) for n in names])
                path = self.new / "pw.json"
                data = json.loads(path.read_text())
                specs = data["suites"][0]["specs"]
                if collision:
                    specs.append(specs[0])
                else:
                    specs[0]["tests"][0]["results"][0]["status"] = ["failed"]
                path.write_text(json.dumps(data))
                html = self.compare([old], [new])
                self.assertIn("Régressions · 1", html)
                self.assertIn("Non comparables · 1", html)

    def test_maestro_contradictions_are_not_transitions(self):
        old = self.mobile(self.old, {"A": "pass", "B": "pass"})
        new = self.mobile(self.new, {"A": "fail", "B": "fail"})
        path = self.new / "run/junit.xml"
        doc = path.read_text()
        path.write_text(doc.replace('name="A"', 'name="A" status="SUCCESS"'))
        html = self.compare([old], [new])
        self.assertIn("Régressions · 1", html)
        self.assertIn("contradictoires", html)
        path.write_text(doc)
        (self.new / "run/exit-code.txt").write_text("0")
        self.assertIn("Régressions · 0", self.compare([old], [new]))

    def test_identical_junit_bytes_on_distinct_devices(self):
        old = [self.mobile(self.old, {"A": "pass"}, folder=d, device=d) for d in ("ios", "android")]
        new = [self.mobile(self.new, {"A": "fail"}, folder=d, device=d) for d in ("ios", "android")]
        self.assertIn("Régressions · 2", self.compare(old, new))

    def test_invalid_notes_leave_other_results_intact(self):
        old = self.mobile(self.old, {"A": "pass", "B": "pass"})
        new = self.mobile(self.new, {"A": "fail", "B": "fail"})
        new["notes"]["B"] = None
        html = self.compare([old], [new])
        self.assertIn("Régressions · 1", html)
        self.assertIn("Non comparables · 1", html)
        new["notes"] = dict(old["notes"], Unplanned=None)
        self.assertIn("Régressions · 2", self.compare([old], [new]))
