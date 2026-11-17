"""Testing agent — automated checks on the generated prototype."""
from __future__ import annotations
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from agents.base import BaseAgent
from services import evidence_ledger


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.features, self.scripts, self.external, self.title = set(), [], [], ""
        self._in_script = self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("data-feature"):
            self.features.add(a["data-feature"])
        if tag == "script":
            self._in_script = True
            self.scripts.append("")
            if a.get("src"):
                self.external.append(a["src"])
        if tag == "link" and a.get("href", "").startswith("http"):
            self.external.append(a["href"])
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_script and self.scripts:
            self.scripts[-1] += data
        if self._in_title:
            self.title += data


class TestingAgent(BaseAgent):
    name = "testing"
    description = "Runs automated checks against the generated prototype."

    def generate_smoke_test(self, venture, spec, output_dir: Path):
        page = output_dir / "index.html"
        features = (spec.get("features") or [])[:5]
        checks = []

        def check(name, ok, detail=""):
            checks.append({"check": name, "passed": bool(ok), "detail": detail})

        text = page.read_text(encoding="utf-8") if page.exists() else ""
        check("Prototype file exists", bool(text), f"{len(text):,} bytes")
        scan = _Scan()
        try:
            scan.feed(text)
        except Exception as e:
            check("HTML parses", False, str(e))
        check("Has a page title", bool(scan.title.strip()), scan.title.strip()[:60])
        js = "\n".join(scan.scripts)
        check("Contains working JavaScript", len(js.strip()) > 100, f"{len(js):,} chars of JS")
        check("Self-contained (no external files)", not scan.external, ", ".join(scan.external[:3]))
        check("No network calls", not re.search(r"\bfetch\s*\(|XMLHttpRequest", js))
        missing = [f.get("name") for i, f in enumerate(features)
                   if f"f{i + 1}" not in scan.features and (f.get("name") or "").lower() not in text.lower()]
        check(f"All {len(features)} MVP features present", not missing,
              ("Missing: " + ", ".join(missing)) if missing else "")
        node = shutil.which("node")
        if node and js.strip():
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as tf:
                tf.write(js)
            r = subprocess.run([node, "--check", tf.name], capture_output=True, text=True, timeout=20)
            Path(tf.name).unlink(missing_ok=True)
            check("JavaScript syntax valid", r.returncode == 0, (r.stderr or "")[-200:])

        passed = all(c["passed"] for c in checks)
        failed = [c["check"] for c in checks if not c["passed"]]
        output = f"{sum(c['passed'] for c in checks)}/{len(checks)} checks passed" + (
            f" — failed: {', '.join(failed)}" if failed else "")
        evidence_ledger.add(venture["id"], type="experimental" if passed else "assumption",
                            claim=f"Prototype tests: {'PASSED' if passed else 'FAILED'} ({output})",
                            agent=self.name, stage="build")
        return {"passed": passed, "checks": checks, "output": output}
