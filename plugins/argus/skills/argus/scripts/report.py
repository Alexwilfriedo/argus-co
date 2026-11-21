"""Build an offline PDF from a campaign plan and the results of its own run."""
import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

LABEL = {"pass": "Réussi", "fail": "Échec", "flaky": "Instable",
         "skip": "Ignoré", "blocked": "Non vérifié", "xfail": "Échec attendu"}


def add(results, name, status):
    if not name or name in results:
        raise ValueError(f"Nom de scénario vide ou en double ({name})")
    results[name] = status


def mobile_results(folder, trace):
    cases = {}
    issues = []
    code = (folder / "exit-code.txt").read_text().strip()
    if code != "0":
        issues.append(f"Maestro a terminé avec le code {code}.")
    junit = folder / "junit.xml"
    if not junit.exists():
        return cases, [*issues, "Rapport JUnit manquant."]
    root = ET.parse(junit).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Format JUnit inconnu")
    suite_error = False
    for suite in root.iter("testsuite"):
        if suite.get("errors", "0") != "0" or suite.get("failures", "0") != "0":
            suite_error = True
    for case in root.iter("testcase"):
        reported = case.get("status", "").upper()
        status = {"": "pass", "SUCCESS": "pass", "PASSED": "pass", "FAILED": "fail",
                  "FAILURE": "fail", "ERROR": "blocked", "SKIPPED": "skip"}.get(reported, "blocked")
        if case.find("error") is not None:
            status = "blocked"
            issues.append(f"Erreur d’exécution · {case.get('name')}")
        elif case.find("failure") is not None:
            status = "fail"
        elif case.find("skipped") is not None:
            status = "skip"
        add(cases, case.get("name"), status)
    if suite_error and "fail" not in cases.values():
        issues.append("La suite JUnit signale une erreur hors des cas rapportés.")
    if code not in {"0", "1"} or (code != "0" or suite_error) and not {"fail", "blocked"}.intersection(cases.values()):
        trace.incomplete.append("Exécution Maestro interrompue ou erreur sans cas correspondant.")
    return cases, issues


def web_results(path, build, identities, case_issues):
    report = json.loads(path.read_text())
    cases = {}
    issues = ["Playwright a signalé une erreur globale."] if report.get("errors") else []

    recorded = report.get("config", {}).get("metadata", {}).get("argusBuild")
    if recorded and recorded != build:
        return {}, ["Le build consigné par Playwright diffère du manifeste."]

    def visit(suite, parents):
        parents = [*parents, suite["title"]]
        for spec in suite.get("specs", []):
            for test in spec["tests"]:
                name = " › ".join(filter(None, [test.get("projectName"), *parents, spec["title"]]))
                project = test.get("projectName")
                if isinstance(project, str) and project.strip():
                    identities[name] = (project, test.get("projectId", ""), *parents, spec["title"])
                attempts = [item.get("status") if isinstance(item, dict) else None
                            for item in (test.get("results") or [])]
                outcome = test.get("status")
                final = attempts[-1] if attempts else None
                status = {"unexpected": "fail", "flaky": "flaky", "skipped": "skip"}.get(outcome, "blocked")
                if status != "skip" and final in {None, "interrupted"}:
                    status = "blocked"
                elif outcome == "expected" and test.get("expectedStatus") == final == "failed":
                    status = "xfail"
                elif outcome == "expected":
                    status = "pass" if final == "passed" else "skip"
                if status in {"pass", "fail"}:
                    expected_attempts = {"passed"} if status == "pass" else {"failed", "timedOut"}
                    if test.get("expectedStatus", "passed") != "passed":
                        case_issues[name] = "Statut attendu du moteur différent d’un succès."
                    elif any(value not in expected_attempts for value in attempts):
                        case_issues[name] = "Tentatives incomplètes, instables ou discordantes."
                add(cases, name, status)
        for child in suite.get("suites", []):
            visit(child, parents)

    for suite in report["suites"]:
        visit(suite, [])
    return cases, issues


def local_evidence(base, reference):
    if not isinstance(reference, str) or not reference.strip():
        return False
    path = (base / reference).resolve()
    return base.resolve() in path.parents and path.is_file() and path.stat().st_size > 0


@dataclass
class Trace:
    # Keep individual attempts alongside the existing campaign-level verdict.
    comparing: bool = False
    attempts: dict = field(default_factory=lambda: defaultdict(list))
    incomplete: list = field(default_factory=list)
    case_issues: dict = field(default_factory=dict)


def read_runs(kind, source, base, build, target, used, trace):
    sources = source if isinstance(source, list) else [source]
    if not sources or (len(sources) > 1 and kind != "maestro"):
        raise ValueError("Choisir un JSON Playwright ou une suite de runs Maestro.")
    cases, issues, origins = {}, [], []
    first_device = None
    for ref in sources:
        folder = base / ref
        artifact = folder / "junit.xml" if kind == "maestro" else folder
        identities = {}
        origins.append(ref)
        try:
            if not folder.exists():
                raise FileNotFoundError(ref)
            if trace.comparing:
                paths = [artifact]
                if kind == "maestro":
                    paths.extend(folder / name for name in ("target.txt", "build.txt", "exit-code.txt"))
                if any(base.resolve() not in path.resolve().parents for path in paths):
                    raise ValueError("Source en dehors du dossier de la campagne.")
            if kind == "maestro":
                identity = folder / "target.txt"
                device, app = identity.read_text().splitlines() if identity.exists() else ("", "")
                if device:
                    origins[-1] += f" · {device} · {app}"
                else:
                    origins[-1] += " · appareil non consigné"
                if (target.get("device", device) != device or target.get("appId", app) != app
                        or first_device is not None and first_device != (device, app)
                        or len(sources) > 1 and not (device and app)):
                    issues.append(f"{ref} · Identité de la cible incompatible ou absente.")
                    trace.incomplete.append(issues[-1])
                    continue
                recorded = folder / "build.txt"
                if recorded.exists() and recorded.read_text().strip() != build:
                    issues.append(f"{ref} · Le build consigné par Maestro diffère du manifeste.")
                    trace.incomplete.append(issues[-1])
                    continue
                current, warnings = mobile_results(folder, trace)
                if device.strip() and app.strip():
                    identities = dict.fromkeys(current, (device, app))
            else:
                current, warnings = web_results(folder, build, identities, trace.case_issues)
            issues.extend(warnings)
            if not current:
                trace.incomplete.extend(warnings or [f"{ref} · Aucun résultat brut."])
                continue
            fingerprint = (kind, hashlib.sha256(artifact.read_bytes()).digest())
            if fingerprint in used:
                issues.append(f"{ref} · Résultat déjà utilisé dans cette campagne.")
                trace.incomplete.append(issues[-1])
                continue
            used.add(fingerprint)
            if kind == "maestro":
                first_device = (device, app)
            for name, status in current.items():
                if trace.comparing:
                    trace.attempts[name].append((identities.get(name), status, artifact))
                previous = cases.get(name)
                if status == "pass" and previous in {"fail", "flaky"}:
                    status = "flaky"
                elif status in {"skip", "blocked"} and previous in {"fail", "flaky"}:
                    status = previous
                cases[name] = status
        except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, ET.ParseError):
            issues.append(f"{ref} · Pièce d’exécution absente ou illisible.")
            trace.incomplete.append(issues[-1])
    if not cases:
        issues.append("Aucun scénario enregistré.")
    return cases, issues, " / ".join(origins)


def read_target(target, base, build, used, trace):
    plan = target["planned"]
    if not isinstance(plan, list) or not plan or len(plan) != len(set(plan)):
        raise ValueError("Chaque cible demande une liste planned de noms distincts.")
    sources = [kind for kind in ("maestro", "playwright", "observations") if kind in target]
    if len(sources) != 1:
        raise ValueError("Choisir une source de résultats par cible.")
    kind = sources[0]
    source = target[kind]
    if kind == "observations":
        cases = {}
        issues = []
        for observation in source:
            status = observation["status"]
            if status not in LABEL:
                raise ValueError(f"Statut inconnu ({status})")
            if status in {"pass", "fail", "flaky"} and not observation.get("evidence"):
                raise ValueError("Une observation exécutée demande une preuve.")
            if status in {"pass", "fail", "flaky", "xfail"} and not local_evidence(base, observation.get("evidence")):
                issues.append(f"{observation['name']} · Pièce justificative absente ou vide.")
                status = "blocked"
            add(cases, observation["name"], status)
        return cases, issues, "Observations consignées"
    return read_runs(kind, source, base, build, target, used, trace)


def h(value):
    return escape(str(value))


def screenshot(base, relative):
    path = (base / relative).resolve()
    if base.resolve() not in path.parents or path.suffix.lower() != ".png":
        raise ValueError("Choisir une capture PNG dans le dossier de la campagne.")
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Capture PNG illisible ({relative})")
    encoded = base64.b64encode(data).decode("ascii")
    return f'<img alt="Capture du constat" src="data:image/png;base64,{encoded}">'


@dataclass
class ComparedCase:
    expected: str
    repro: str
    status: str
    artifacts: tuple
    reason: str


def index_target(index, target, trace, cases):
    kind = next(key for key in ("playwright", "maestro", "observations") if key in target)
    label = target.get("label") or ""
    for name in dict.fromkeys([*target["planned"], *cases]):
        attempts = trace.attempts.get(name, [])
        identity = attempts[0][0] if attempts else None
        statuses = {status for _, status, _ in attempts}
        status = cases.get(name, "blocked")
        note = target.get("notes", {}).get(name, {})
        reason = ""
        if kind == "observations":
            reason = "Observation déclarée sans résultat brut du moteur."
        elif trace.incomplete:
            reason = " ".join(trace.incomplete)
        elif not attempts:
            reason = "Résultat brut absent."
        elif not label.strip() or not identity:
            reason = "Identité de la cible absente."
        elif note.get("conflict"):
            reason = f"Règle à arbitrer · {note['conflict']}"
        elif name in trace.case_issues:
            reason = trace.case_issues[name]
        elif len(statuses) > 1 or status == "flaky":
            reason = "Résultat instable ou tentatives discordantes."
        elif status not in {"pass", "fail"}:
            reason = f"Résultat inexploitable · {LABEL[status]}."
        artifacts = tuple((path, raw_status) for _, raw_status, path in attempts)
        key = (label, kind, name, identity)
        index[key].append(ComparedCase(note.get("expected"), note.get("repro"), status, artifacts, reason))


TRANSITIONS = {("pass", "fail"): "Régression", ("fail", "pass"): "Correction",
               ("fail", "fail"): "Défaut persistant", ("pass", "pass"): "Succès maintenu"}


def classify(before, after, same_scope):
    for side, entries in (("Référence", before), ("Candidat", after)):
        if len(entries) > 1:
            return "Non comparable", f"{side} · Plusieurs résultats pour la même identité."
        if entries and entries[0].reason:
            return "Non comparable", f"{side} · {entries[0].reason}"
    if not before or not after:
        side = "la référence" if not before else "le candidat"
        return "Non comparable", f"Scénario ou cible sans correspondance dans {side}."
    left, right = before[0], after[0]
    if not same_scope or left.repro != right.repro:
        return "Non comparable", "Périmètre ou conditions de reproduction différents ou inconnus."
    if not all(isinstance(case.expected, str) and case.expected.strip() for case in (left, right)):
        return "Non comparable", "Attendu absent."
    if left.expected != right.expected:
        return "Non comparable", "Attendu modifié."
    if {path.resolve() for path, _ in left.artifacts}.intersection(path.resolve() for path, _ in right.artifacts):
        return "Non comparable", "Même pièce d’exécution dans les deux campagnes."
    return TRANSITIONS[left.status, right.status], ""


def proof_text(entries, base):
    proofs = []
    for entry in entries:
        for path, status in entry.artifacts:
            proofs.append(f"{LABEL[status]} · {os.path.relpath(path, base)}")
    return " / ".join(proofs) or "Aucune pièce exploitable"


def comparison_target(target, base, build, used, index):
    trace = Trace(comparing=True)
    try:
        cases, issues, source = read_target(target, base, build, used, trace)
        index_target(index, target, trace, cases)
        return cases, issues, source
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        reason = f"Cible inexploitable · {error}"
        names = target.get("planned") if isinstance(target, dict) else None
        names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
        label = target.get("label") if isinstance(target, dict) else None
        for name in names or ["Scénario inconnu"]:
            key = (str(label or ""), "source inconnue", name, None)
            index[key].append(ComparedCase(None, None, "blocked", (), reason))
        return {}, [reason], None


def comparison_html(reference, campaign, base, candidate):
    heading = "<section><h2>Comparaison des campagnes</h2>"
    baseline = defaultdict(list)
    try:
        previous = json.loads(reference.read_text(encoding="utf-8"))
        used = set()
        for target in previous["targets"]:
            comparison_target(target, reference.parent, previous["build"], used, baseline)
        build = previous["build"]
        if not all(isinstance(b, str) and b.strip() for b in (build, campaign["build"])):
            return f"{heading}<p>Build absent du manifeste de référence ou du candidat.</p></section>"
        same_scope = bool(campaign["scope"].strip()) and previous.get("scope") == campaign["scope"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return f"{heading}<p>Manifeste de référence absent ou illisible · {h(reference.name)}</p></section>"
    rows = []
    totals = Counter()
    for key in dict.fromkeys([*candidate, *baseline]):
        label, kind, name, identity = key
        before, after = baseline.get(key, []), candidate.get(key, [])
        verdict, reason = classify(before, after, same_scope)
        totals[verdict] += 1
        target = " · ".join((label, kind, *identity[:2])) if identity and kind == "maestro" else f"{label} · {kind}"
        rows.append(f"<tr><td><strong>{h(name)}</strong><div>{h(target)}</div>"
                    f"<div>Référence · {h(proof_text(before, base))}</div>"
                    f"<div>Candidat · {h(proof_text(after, base))}</div></td>"
                    f"<td><b>{h(verdict)}</b><div>{h(reason)}</div></td></tr>")
    return (f"{heading}<p>Référence · {h(build)} · {h(os.path.relpath(reference, base))}<br>"
            f"Candidat · {h(campaign['build'])}</p>"
            f"<p>Régressions · {totals['Régression']} | Corrections · {totals['Correction']} | "
            f"Défauts persistants · {totals['Défaut persistant']} | Non comparables · {totals['Non comparable']}</p>"
            "<table><thead><tr><th>Scénario, cible et preuves</th><th>Évolution</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></section>")


def build_html(campaign, base, reference=None):
    candidate = defaultdict(list)
    totals = Counter()
    sections = []
    planned = reached = 0
    all_issues = []
    used = set()
    if not campaign["targets"]:
        raise ValueError("La campagne demande au moins une cible.")
    for target in campaign["targets"]:
        if reference is None:
            cases, issues, source = read_target(target, base, campaign["build"], used, Trace())
        else:
            cases, issues, source = comparison_target(target, base, campaign["build"], used, candidate)
            if source is None:
                all_issues.extend(issues)
                sections.append(f"<section><h2>Cible inexploitable</h2><p>{h(issues[0])}</p></section>")
                if isinstance(target, dict) and isinstance(target.get("planned"), list):
                    planned += len(target["planned"])
                    totals["blocked"] += len(target["planned"])
                continue
        names = target["planned"]
        planned += len(names)
        notes = dict(target.get("notes", {}))
        if "observations" in target:
            for obs in target["observations"]:
                notes[obs["name"]] = {**notes.get(obs["name"], {}), **obs}
        raw = cases.copy()
        for name, note in notes.items():
            if note.get("conflict"):
                cases[name] = "blocked"
        reached += sum(cases.get(name) in {"pass", "fail", "flaky", "xfail"} for name in names)
        names = list(dict.fromkeys([*names, *cases]))
        priority = {"fail": 0, "blocked": 1, "flaky": 2, "xfail": 3, "skip": 4, "pass": 5}
        names.sort(key=lambda name: priority[cases.get(name, "blocked")])
        rows = []
        for name in names:
            status = cases.get(name, "blocked")
            totals[status] += 1
            note = notes.get(name, {})
            details = []
            if note.get("conflict"):
                details.append(f"<div><b>À arbitrer</b> {h(note['conflict'])}</div>")
                source_label = "Résultat déclaré" if "observations" in target else "Résultat brut"
                details.append(f"<div><b>{source_label}</b> {LABEL[raw.get(name, 'blocked')]}</div>")
            for key, label in (("severity", "Sévérité"), ("expected", "Attendu"),
                               ("actual", "Observé"), ("repro", "Reproduire"),
                               ("evidence", "Référence"), ("action", "Suite")):
                if note.get(key):
                    details.append(f"<div><b>{label}</b> {h(note[key])}</div>")
            if note.get("screenshot"):
                try:
                    details.append(screenshot(base, note["screenshot"]))
                except (OSError, ValueError):
                    issues.append(f"{name} · Capture supplémentaire absente ou illisible.")
            rows.append(f"<tr><td><strong>{h(name)}</strong>{''.join(details)}</td>"
                        f"<td class='{status}'>{LABEL[status]}</td></tr>")
        all_issues.extend(issues)
        warnings = "".join(f"<p class='blocked'>{h(issue)}</p>" for issue in issues)
        sections.append(f"<section><h2>{h(target.get('label') or 'Cible absente')}</h2><small>Source · {h(source)}</small>"
                        f"{warnings}<table><thead><tr><th>Scénario et constats</th><th>Résultat</th>"
                        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>")
    if totals["fail"]:
        verdict = "Échecs observés"
    elif all_issues or totals["blocked"] or totals["skip"] or totals["flaky"] or totals["xfail"]:
        verdict = "À examiner"
    else:
        verdict = "Parcours vérifiés"
    cards = "".join(f"<div><strong>{totals[key]}</strong>{label}</div>" for key, label in LABEL.items())
    if reference is not None:
        sections.insert(0, comparison_html(reference, campaign, base, candidate))
    css = Path(__file__).with_name("report.css").read_text()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>Argus · Rapport de tests</title><style>{css}</style></head><body>
<header>ARGUS <span>Rapport de tests</span></header><h1>{h(campaign['title'])}</h1>
<p>{h(campaign['scope'])}</p><small>Build déclaré · {h(campaign['build'])}</small>
<h2>{verdict}</h2><div class="metrics">{cards}</div>
<p><b>{reached} / {planned}</b> scénarios prévus avec un résultat exploitable.</p>
{''.join(sections)}<footer>Argus · Document généré le {generated}</footer></body></html>"""


def export(html, output):
    chrome = os.environ.get("ARGUS_CHROME") or next((path for name in
        ("google-chrome", "chromium", "chromium-browser", "chrome") if (path := shutil.which(name))), None)
    if not chrome:
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(chrome).is_file():
        raise ValueError("Définir ARGUS_CHROME avec le chemin de Chrome ou Chromium.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as temp:
        work = Path(temp).resolve()
        page = work / "report.html"
        page.write_text(html, encoding="utf-8")
        pdf = work / "report.pdf"
        subprocess.run([chrome, "--headless", "--disable-gpu", "--disable-background-networking",
                        "--no-pdf-header-footer", f"--user-data-dir={work / 'profile'}",
                        f"--print-to-pdf={pdf}", page.as_uri()], check=True, timeout=90)
        if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
            raise ValueError("Le navigateur n’a pas produit de PDF.")
        pdf.replace(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reference", type=Path, help="Manifeste de la campagne de référence")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".pdf":
        parser.error("Choisir un fichier de sortie .pdf.")
    try:
        campaign = json.loads(args.campaign.read_text(encoding="utf-8"))
        export(build_html(campaign, args.campaign.parent, args.reference), args.output)
    except (OSError, KeyError, TypeError, ValueError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Rapport Argus interrompu · {error}\n")
    print(args.output)


if __name__ == "__main__":
    main()
