"""Validate grafana/aqua_cortex_panel.json against a hand-written minimal schema.

Acceptance (Phase 3, Lane A):
  * The file parses as JSON.
  * Top-level shape matches the Grafana dashboard JSON model.
  * It declares ≥3 panels with `type == "table"` in the row.
  * Every panel target has a `datasource.uid` referencing
    `${DS_AQUA_CORTEX_JSON}` (the JSON API input we declare in
    `__inputs`).

This is a deliberately permissive check — Grafana's published panel
JSON schema is hundreds of fields deep and changes between minor
versions. We catch the failures we actually care about (missing
panels, wrong datasource, broken JSON) without coupling the test to
Grafana internals.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PANEL_PATH = REPO_ROOT / "grafana" / "aqua_cortex_panel.json"


REQUIRED_TOP_LEVEL = {
    "title",
    "uid",
    "schemaVersion",
    "panels",
    "__inputs",
    "__requires",
    "tags",
    "timezone",
}

REQUIRED_INPUTS_FIELDS = {"name", "label", "type", "pluginId", "pluginName"}

PANEL_DS_UID = "${DS_AQUA_CORTEX_JSON}"


class TestAquaCortexPanelJSON(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(PANEL_PATH.read_text(encoding="utf-8"))

    def test_file_parses(self) -> None:
        self.assertIsInstance(self.data, dict)
        self.assertGreater(len(self.data), 0)

    def test_top_level_keys(self) -> None:
        missing = REQUIRED_TOP_LEVEL - set(self.data.keys())
        self.assertFalse(
            missing, msg=f"missing top-level keys: {sorted(missing)}"
        )

    def test_uid_and_title(self) -> None:
        self.assertEqual(self.data["uid"], "aqua-cortex")
        self.assertEqual(self.data["title"], "aqua_cortex")

    def test_declares_json_api_input(self) -> None:
        inputs = self.data["__inputs"]
        self.assertIsInstance(inputs, list)
        self.assertGreaterEqual(len(inputs), 1)
        json_inputs = [i for i in inputs if i.get("pluginId") == "marcusolsson-json-datasource"]
        self.assertEqual(
            len(json_inputs), 1,
            msg=f"expected exactly 1 JSON API input, got {len(json_inputs)}",
        )
        inp = json_inputs[0]
        missing = REQUIRED_INPUTS_FIELDS - set(inp.keys())
        self.assertFalse(missing, msg=f"__input missing fields: {sorted(missing)}")
        self.assertEqual(inp["name"], "DS_AQUA_CORTEX_JSON")

    def test_declares_table_panel_plugin(self) -> None:
        requires = self.data["__requires"]
        plugin_ids = {r.get("id") for r in requires}
        self.assertIn("table", plugin_ids)
        self.assertIn("marcusolsson-json-datasource", plugin_ids)
        self.assertIn("grafana", plugin_ids)

    def test_at_least_three_table_panels(self) -> None:
        panels = self.data["panels"]
        # Filter out row-type pseudo-panels (the row separator is itself
        # a panel with type="row").
        table_panels = [p for p in panels if p.get("type") == "table"]
        self.assertGreaterEqual(
            len(table_panels), 3,
            msg=f"expected ≥3 table sub-panels, got {len(table_panels)}: "
                f"{[p.get('title') for p in table_panels]}",
        )

    def test_subpanel_titles(self) -> None:
        titles = {p.get("title") for p in self.data["panels"] if p.get("type") == "table"}
        # The body spec names these three; we accept those exact titles.
        for required in ("Doc vs Running", "Doc Freshness", "Recent Activity"):
            self.assertIn(required, titles, msg=f"missing sub-panel: {required}")

    def test_panels_reference_json_datasource(self) -> None:
        """Every table panel's targets must point at ${DS_AQUA_CORTEX_JSON}."""
        for panel in self.data["panels"]:
            if panel.get("type") != "table":
                continue
            targets = panel.get("targets") or []
            self.assertGreater(len(targets), 0, msg=f"panel {panel.get('title')!r} has no targets")
            for tgt in targets:
                ds = tgt.get("datasource") or {}
                uid = ds.get("uid")
                self.assertEqual(
                    uid, PANEL_DS_UID,
                    msg=f"panel {panel.get('title')!r} target uid={uid!r} "
                        f"(expected {PANEL_DS_UID!r})",
                )
                # The path must read one of the snapshot sections.
                path = tgt.get("path", "")
                self.assertIn(
                    path, {"doc_vs_running", "doc_freshness", "recent_activity"},
                    msg=f"panel {panel.get('title')!r} unexpected path={path!r}",
                )

    def test_refresh_interval_set(self) -> None:
        self.assertIn("refresh", self.data)
        self.assertTrue(
            re.match(r"^\d+[smhd]$", self.data["refresh"]),
            msg=f"refresh={self.data['refresh']!r} not a valid Grafana interval",
        )


if __name__ == "__main__":
    unittest.main()
