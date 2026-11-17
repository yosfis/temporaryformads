"""Development agent — builds a working, clickable prototype of the MVP.

Output is a single self-contained `index.html` (inline CSS + JS, no network
calls) that implements the MVP features with realistic sample data, so the
founder can open and use it straight from the workspace.
"""
from __future__ import annotations
import html
import json
import re
from pathlib import Path

from agents.base import BaseAgent
from services import evidence_ledger

BUILD_TIMEOUT = 240  # a whole app is a long generation; don't use the short default


def _extract_html(text):
    text = text or ""
    m = re.search(r"(<!DOCTYPE html.*?</html>|<html.*?</html>)", text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else None


class DevelopmentAgent(BaseAgent):
    name = "development"
    description = "Builds a working, clickable MVP prototype."
    system_prompt = ("You are a senior front-end engineer. You build small but genuinely WORKING product "
                     "prototypes. Never leave TODOs or placeholder buttons that do nothing.")

    def scaffold_mvp(self, venture, spec, output_dir: Path, on_progress=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        features = (spec.get("features") or [])[:5]
        brief = venture.get("brief") or {}
        name = venture.get("name") or brief.get("name") or "MVP"

        page, source = None, "fallback"
        for attempt in range(2):
            if on_progress:
                on_progress("Writing the prototype code…" if attempt == 0 else "Retrying prototype generation…")
            try:
                page = _extract_html(self._generate(venture, name, brief, features))
            except Exception:
                page = None
            if page and len(page) > 1500 and "<script" in page.lower():
                source = "ai"
                break
            page = None
        if not page:
            page = fallback_prototype(name, brief, features)

        (output_dir / "index.html").write_text(page, encoding="utf-8")
        (output_dir / "spec.json").write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        readme = [f"# {name} — MVP prototype", "", brief.get("concept") or venture.get("idea", ""), "",
                  "## Run it", "Open `index.html` in any browser. No install or server needed — data is",
                  "saved in your browser (localStorage).", "", "## Features"]
        readme += [f"- **{f.get('name', '')}** — {f.get('purpose', '')}" for f in features]
        (output_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")

        evidence_ledger.add(venture["id"], type="verified",
                            claim=f"Built clickable prototype ({len(features)} features)",
                            details="AI-generated" if source == "ai" else "template fallback (AI generation failed)",
                            agent=self.name, stage="build")
        return {"output_dir": str(output_dir), "source": source,
                "files": sorted(p.name for p in output_dir.iterdir() if p.is_file())}

    def _generate(self, venture, name, brief, features):
        feats = [{"id": f"f{i + 1}", "name": f.get("name"), "purpose": f.get("purpose"),
                  "acceptance": f.get("acceptance")} for i, f in enumerate(features)]
        prompt = json.dumps({
            "product_name": name,
            "idea": venture.get("idea"),
            "target_audience": brief.get("target_audience"),
            "problem": brief.get("problem"),
            "solution": brief.get("solution"),
            "mvp_features": feats,
        }, ensure_ascii=False, indent=2)
        instructions = (
            "Build a single-file, fully working web prototype of this product.\n"
            "Requirements:\n"
            "- ONE complete HTML document with inline <style> and <script>. No external files, CDNs, "
            "fonts, images or network requests (no fetch/XHR).\n"
            "- Implement EVERY feature in mvp_features as a real, interactive section. Wrap each in an element "
            "with data-feature=\"<id>\" (e.g. data-feature=\"f1\") and show the feature name as its heading.\n"
            "- Use realistic sample data specific to this product and audience; persist user changes in "
            "localStorage. Anything that would need a real backend or AI must be simulated convincingly in JS.\n"
            "- Header with product name and a one-line value proposition. Clean, modern, responsive design.\n"
            "- Every button must do something. No TODOs, no lorem ipsum.\n"
            "Output ONLY the HTML document, starting with <!DOCTYPE html>."
        )
        return self.client.chat([{"role": "user", "content": instructions + "\n\nProduct:\n" + prompt}],
                                system=self.system_prompt, temperature=0.4, timeout=BUILD_TIMEOUT)


def fallback_prototype(name, brief, features):
    """Deterministic, still-working prototype used if AI generation fails."""
    e = html.escape
    sections = []
    for i, f in enumerate(features):
        fid = f"f{i + 1}"
        sections.append(f"""
  <section class="card" data-feature="{fid}">
    <h2>{e(f.get('name') or f'Feature {i + 1}')}</h2>
    <p class="muted">{e(f.get('purpose') or '')}</p>
    <form onsubmit="add(event,'{fid}')"><input placeholder="Try it — add an entry" required><button>Add</button></form>
    <ul id="list-{fid}"></ul>
    <p class="accept"><b>Done when:</b> {e(f.get('acceptance') or '—')}</p>
  </section>""")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(name)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#0b0b12;color:#eee}}
header{{padding:32px 20px;background:linear-gradient(135deg,#4f46e5,#06b6d4)}}
h1{{margin:0}} main{{max-width:900px;margin:auto;padding:20px;display:grid;gap:16px}}
.card{{background:#16161f;border:1px solid #2a2a3a;border-radius:12px;padding:18px}}
.muted{{color:#aab}} .accept{{font-size:13px;color:#99a}}
form{{display:flex;gap:8px}} input{{flex:1;padding:8px;border-radius:8px;border:1px solid #333;background:#0f0f18;color:#eee}}
button{{padding:8px 14px;border:0;border-radius:8px;background:#6366f1;color:#fff;cursor:pointer}}
li{{display:flex;justify-content:space-between;padding:4px 0}}
</style></head><body>
<header><h1>{e(name)}</h1><p>{e(brief.get('concept') or brief.get('solution') or '')}</p></header>
<main>{''.join(sections)}
</main>
<script>
const KEY = "mvp-" + {json.dumps(name)};
let data = {{}}; try {{ data = JSON.parse(localStorage.getItem(KEY)) || {{}}; }} catch (e) {{}}
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(data)); }} catch (e) {{}} }}
function render(fid) {{
  const ul = document.getElementById("list-" + fid); ul.innerHTML = "";
  (data[fid] || []).forEach((t, i) => {{
    const li = document.createElement("li"); li.textContent = t;
    const b = document.createElement("button"); b.textContent = "Remove";
    b.onclick = () => {{ data[fid].splice(i, 1); save(); render(fid); }};
    li.appendChild(b); ul.appendChild(li);
  }});
}}
function add(ev, fid) {{
  ev.preventDefault(); const inp = ev.target.querySelector("input");
  (data[fid] = data[fid] || []).push(inp.value); inp.value = ""; save(); render(fid);
}}
document.querySelectorAll("[data-feature]").forEach((s) => render(s.dataset.feature));
</script></body></html>
"""
