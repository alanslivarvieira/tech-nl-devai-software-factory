"""Graph wiring.

intake → recon → grill_turn ⇄ ask_human (interrupt) → freeze → plan → schedule
schedule ─Send→ worker* → integrate → schedule … → verify (clean-context subgraph) → triage
triage → pr | schedule (fix round) | escalate (interrupt) → schedule | grill_turn (amend) | END
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ..services import Services
from ..state import FactoryState, VerifyState
from .nodes import Nodes


def build_verify_graph(nodes: Nodes) -> Any:
    g = StateGraph(VerifyState)
    g.add_node("checkout", nodes.v_checkout)
    g.add_node("gates", nodes.v_gates)
    g.add_node("audit", nodes.v_audit)
    g.add_edge(START, "checkout")
    g.add_edge("checkout", "gates")
    g.add_edge("gates", "audit")
    g.add_edge("audit", END)
    return g.compile()


def build_graph(services: Services, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    nodes = Nodes(services)
    nodes.verify_graph = build_verify_graph(nodes)  # type: ignore[attr-defined]

    g = StateGraph(FactoryState)
    for name in (
        "intake",
        "recon",
        "grill_turn",
        "ask_human",
        "freeze",
        "plan",
        "schedule",
        "worker",
        "integrate",
        "verify",
        "triage",
        "escalate",
        "pr",
    ):
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, "intake")
    g.add_edge("intake", "recon")
    g.add_edge("recon", "grill_turn")
    g.add_conditional_edges(
        "grill_turn", lambda s: "ask_human" if s.get("pending") else "grill_turn", ["ask_human", "grill_turn"]
    )
    g.add_conditional_edges(
        "ask_human",
        lambda s: {"aborted": "end", "freezing": "freeze"}.get(s.get("status", ""), "grill_turn"),
        {"end": END, "freeze": "freeze", "grill_turn": "grill_turn"},
    )
    g.add_conditional_edges("freeze", nodes.route_after_freeze, ["plan", "ask_human"])
    g.add_edge("plan", "schedule")
    g.add_conditional_edges("schedule", nodes.route_schedule, ["worker", "verify"])
    g.add_edge("worker", "integrate")
    g.add_edge("integrate", "schedule")
    g.add_edge("verify", "triage")
    g.add_conditional_edges("triage", nodes.route_triage, ["pr", "schedule", "escalate"])
    g.add_conditional_edges(
        "escalate", nodes.route_escalate, {"end": END, "grill_turn": "grill_turn", "schedule": "schedule"}
    )
    g.add_edge("pr", END)
    return g.compile(checkpointer=checkpointer)
