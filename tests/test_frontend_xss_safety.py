"""Correction v1.2 #7: no backend-sourced string (strategy justification,
risk reason, AI reasoning summary, error detail) may ever be interpreted as
HTML/markup by the browser. Two layers of proof:

1. Static: frontend/app.js must contain zero uses of `.innerHTML` -- the
   vulnerable pattern that used to insert this data directly.
2. Dynamic: actually executes app.js's row-building functions in a minimal
   Node.js DOM shim and feeds them a classic XSS payload, then asserts the
   payload landed as plain text (`textContent`) with zero child nodes --
   i.e. never parsed as markup.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
APP_JS = FRONTEND_DIR / "app.js"

NODE = shutil.which("node")

_HARNESS = r"""
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this._text = "";
    this.className = "";
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); return child; }
  get firstChild() { return this.children[0] || null; }
  querySelector() { return new FakeElement("tbody"); }
  getContext() {
    return { clearRect(){}, fillText(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){} };
  }
  addEventListener() {}
}

global.document = {
  createElement: (tag) => new FakeElement(tag),
  getElementById: (id) => new FakeElement("div"),
  querySelector: (sel) => new FakeElement("tbody"),
};
global.fetch = () => Promise.resolve({ json: () => Promise.resolve([]) });
global.setInterval = () => {};
global.window = global;
// app.js kicks off its own async refreshAll() at load time against the
// stubbed fetch above; this harness only cares about buildRow()/kvRow(),
// so background rejections from that unrelated call are irrelevant noise.
process.on("unhandledRejection", () => {});

const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");
eval(code);

const payload = "<img src=x onerror=\"alert('xss')\">";
const tr = buildRow([payload, { text: payload, className: "negative" }]);
const td1 = tr.children[0];
const td2 = tr.children[1];

const kvContainer = new FakeElement("div");
kvRow(kvContainer, "Motivo", payload);
const kvValueSpan = kvContainer.children[0].children[1];

console.log(JSON.stringify({
  td1_text: td1.textContent,
  td1_child_count: td1.children.length,
  td2_text: td2.textContent,
  td2_class: td2.className,
  kv_text: kvValueSpan.textContent,
  kv_child_count: kvValueSpan.children.length,
}));
"""


def test_app_js_never_uses_innerhtml():
    source = APP_JS.read_text(encoding="utf-8")
    assert ".innerHTML" not in source, (
        "frontend/app.js must never assign .innerHTML -- use textContent/DOM "
        "element creation for any backend-sourced content."
    )


@pytest.mark.skipif(NODE is None, reason="Node.js not available in this environment")
def test_xss_payload_renders_as_plain_text_never_as_markup(tmp_path):
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness_path), str(APP_JS)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"harness failed: {result.stderr}"
    data = json.loads(result.stdout.strip().splitlines()[-1])

    payload = "<img src=x onerror=\"alert('xss')\">"
    assert data["td1_text"] == payload
    assert data["td1_child_count"] == 0  # never parsed into a real <img> node
    assert data["td2_text"] == payload
    assert data["td2_class"] == "negative"
    assert data["kv_text"] == payload
    assert data["kv_child_count"] == 0
