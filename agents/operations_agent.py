"""Operations agent — turns everything learned into a 90-day operating plan."""
from __future__ import annotations
from agents.base import BaseAgent
from services import evidence_ledger


class OperationsAgent(BaseAgent):
    name = "operations"
    description = "Builds the 90-day operating plan: KPIs, roadmap, risks."
    system_prompt = ("You are a pragmatic startup operator. Every KPI needs a measurable target; every "
                     "roadmap item must be concrete and achievable by a small founding team. No generic advice.")

    def operating_plan(self, venture, context):
        schema = ('{"kpis": [{"metric": "string", "target_90_days": "string", "how_to_measure": "string"}], '
                  '"roadmap": {"days_0_30": ["string"], "days_31_60": ["string"], "days_61_90": ["string"]}, '
                  '"risks": [{"risk": "string", "mitigation": "string"}], '
                  '"weekly_rhythm": ["string"], "first_action_tomorrow": "string"}')
        result = self._chat_json({
            "venture_idea": venture.get("idea"), "venture_name": venture.get("name"),
            "everything_learned_so_far": context,
            "instruction": ("Create a 90-day operating plan to launch and run this venture: 4-6 KPIs with "
                            "targets, a 30/60/90-day roadmap (3-5 items each), top 3-4 risks with "
                            "mitigations, a weekly operating rhythm, and the single first action for tomorrow."),
        }, schema_hint=schema)
        for k in result.get("kpis") or []:
            evidence_ledger.add(venture["id"], type="assumption",
                                claim=f"KPI target: {k.get('metric', '')} → {k.get('target_90_days', '')}",
                                details=k.get("how_to_measure", ""), agent=self.name, stage="operate")
        return result
