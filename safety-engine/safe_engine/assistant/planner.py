"""Answer generators.

* :class:`RulePlanner` – deterministic intent rules + tool calls. Used when no
  LLM key is configured (fully offline), and as the reference generator in the
  evaluation harness.
* :class:`GroqPlanner` – tool-calling LLM via the Groq OpenAI-compatible API
  (the original project already used Groq). Produces the same JSON schema.

Both only *propose* claims; nothing reaches the user without passing the gate.
"""
from __future__ import annotations

import json
import re

import httpx

from .tools import TOOL_SPECS, Tools

SYSTEM_PROMPT = """You are the safety assistant of a disaster-aware navigation system in India.
You MUST obtain every fact by calling tools (get_route, get_hazards, get_shelters, get_sop, get_route_explanation, get_point_forecast).
Never state a route, road, shelter, hazard or safety fact from your own knowledge.
When you are done, reply with ONLY a JSON object:
{"claims": [ {"type": "<type>", "ids": ["<ids returned by tools>"], ...} ], "explanation": "<optional short non-safety context>"}
Claim types:
- route_recommendation: ids = [one route id with feasible=true]
- no_safe_route: ids = [all route ids returned, all infeasible]
- shelter_recommendation: ids = [shelter_id, route_id leading to it with reachable=true]
- hazard_warning: ids = [hazard ids]
- instruction: ids = [SOP clause ids from get_sop]
- hazard_report: {"type":"hazard_report","hazard":"flood|landslide|storm","depth":"ankle|knee|waist|unknown","text":"..."} when the user reports a hazard at their location
Always add at least one instruction claim citing an SOP clause. Do not invent ids."""


def _coords(text: str):
    m = re.findall(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", text)
    pts = []
    for a, b in m:
        a, b = float(a), float(b)
        pts.append([b, a] if abs(a) < 40 and abs(b) > 40 else [a, b])  # accept "lat,lon" or "lon,lat" for India
    return pts


class RulePlanner:
    name = "rule-planner"

    def propose(self, question: str, tools: Tools, location, destination=None, feedback=None) -> dict:
        q = question.lower()
        claims, used_hazard = [], "any"
        pts = _coords(question)
        dest = destination or (pts[-1] if pts else None)
        if re.search(r"\b(flooded|water logging|waterlogged|landslide here|tree fell|blocked here|report)\b", q) and location:
            hz = "landslide" if "landslide" in q else ("storm" if "tree" in q else "flood")
            depth = "waist" if "waist" in q else "knee" if "knee" in q else "ankle" if "ankle" in q else "unknown"
            claims.append({"type": "hazard_report", "hazard": hz, "depth": depth, "text": question[:200]})
            used_hazard = hz
        elif re.search(r"shelter|evacuat|where (should|can) i go|safe place|relief camp|आश्रय|தங்குமிட", q) and location:
            res = tools.get_shelters(location[0], location[1])
            ok = [s for s in res["shelters"] if s["reachable"]]
            if ok:
                claims.append({"type": "shelter_recommendation", "ids": [ok[0]["shelter_id"], ok[0]["route_id"]]})
            else:
                rids = [k for k, v in tools.ledger.objects.items() if v["kind"] == "route" and v["data"].get("shelter_id")]
                if rids:
                    claims.append({"type": "no_safe_route", "ids": rids})
            used_hazard = "flood"
        elif re.search(r"hazard|danger|near me|around me|खतरा|ஆபத்து", q) and location:
            hz = _top_hazards(tools.get_hazards(location[0], location[1], 3)["hazards"])
            if hz:
                claims.append({"type": "hazard_warning", "ids": [h["id"] for h in hz]})
                used_hazard = hz[0]["class"]
        elif dest and location:
            res = tools.get_route(location, dest)
            if res["chosen"]:
                claims.append({"type": "route_recommendation", "ids": [res["chosen"]]})
            elif res["routes"]:
                claims.append({"type": "no_safe_route", "ids": [r["id"] for r in res["routes"]]})
            used_hazard = "flood"
        elif location:
            hz = _top_hazards(tools.get_hazards(location[0], location[1], 3)["hazards"])
            if hz:
                claims.append({"type": "hazard_warning", "ids": [h["id"] for h in hz]})
                used_hazard = hz[0]["class"]
        sop = tools.get_sop(used_hazard if used_hazard != "closure" else "any")
        crit = [c["id"] for c in sop["clauses"] if c["critical"]][:2]
        if crit:
            claims.append({"type": "instruction", "ids": crit})
        return {"claims": claims, "explanation": ""}


class GroqPlanner:
    name = "groq-llm"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None, max_tool_rounds: int = 5):
        self.key, self.model = api_key, model
        self.client = client or httpx.Client(timeout=40)
        self.rounds = max_tool_rounds

    def _chat(self, messages):
        r = self.client.post("https://api.groq.com/openai/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.key}"},
                             json={"model": self.model, "messages": messages, "tools": TOOL_SPECS, "temperature": 0.1})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def propose(self, question: str, tools: Tools, location, destination=None, feedback=None) -> dict:
        ctx = f"User location (lon,lat): {location}. Vehicle: {tools.vehicle}. Answer language: {tools.lang}."
        if destination:
            ctx += f" Destination (lon,lat): {destination}."
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": f"{ctx}\n\nQuestion: {question}"}]
        if feedback:
            messages.append({"role": "user", "content": "Your previous answer failed verification: " + "; ".join(feedback) +
                             ". Call the tools again and return corrected JSON."})
        for _ in range(self.rounds):
            msg = self._chat(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                return _parse_json(msg.get("content") or "")
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = tools.call(c["function"]["name"], args)
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out)[:6000]})
        return {"claims": []}


def _top_hazards(items: list, n: int = 3) -> list:
    """Most relevant hazard per class (nearest, then most likely), up to n."""
    best = {}
    for h in sorted(items, key=lambda h: (h["distance_m"], -h["probability"], h["t_start"])):
        best.setdefault(h["class"], h)
    return list(best.values())[:n]


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"claims": [], "explanation": text[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"claims": [], "explanation": text[:300]}
