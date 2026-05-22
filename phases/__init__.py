#!/usr/bin/env python3
"""
phases 包 — V4 重构阶段处理器集合

用法：
    from phases import AgentEngine
    from phases import PlanningHandler, DebateHandler, CodingHandler, ...

    engine = AgentEngine()
    engine.register(State.PLANNING, PlanningHandler(ai_client=client))
    engine.register(State.DEBATING, DebateHandler(ai_client=client))
    engine.register(State.CONSENSUS, ConsensusHandler(ai_client=client, notification_service=notify))
    engine.register(State.CODING, CodingHandler(ai_client=client, git_service=git))
    engine.register(State.BUILDING, BuildingHandler(build_service=build))
    engine.register(State.CODEX_REVIEW, CodexReviewHandler(ai_client=client))
    engine.register(State.REQUIREMENT_REVIEW, RequirementReviewHandler(ai_client=client))
    engine.register(State.CORRECTING, CorrectingHandler())
    engine.register(State.GIT_COMMITTING, GitCommittingHandler(git_service=git))
    engine.register(State.CREATING_PR, CreatingPRHandler(git_service=git))
    engine.register(State.NOTIFYING, NotifyingHandler(notification_service=notify))
    engine.process_task(task)
"""

from __future__ import annotations

from phases.base import PhaseHandler, PhaseResult
from phases.architect_review import ArchitectReviewHandler
from phases.building import BuildingHandler
from phases.codex_review import CodexReviewHandler
from phases.consensus import ConsensusHandler
from phases.correcting import CorrectingHandler
from phases.creating_pr import CreatingPRHandler
from phases.coding import CodingHandler
from phases.debate import DebateHandler
from phases.git_committing import GitCommittingHandler
from phases.notifying import NotifyingHandler
from phases.planning import PlanningHandler
from phases.red_team_review import RedTeamReviewHandler
from phases.requirement_review import RequirementReviewHandler
from phases.self_review import SelfReviewHandler

__all__ = [
    # 基类
    "PhaseHandler",
    "PhaseResult",
    # 阶段处理器
    "PlanningHandler",
    "DebateHandler",
    "ConsensusHandler",
    "CodingHandler",
    "BuildingHandler",
    "SelfReviewHandler",
    "CodexReviewHandler",
    "ArchitectReviewHandler",
    "RedTeamReviewHandler",
    "RequirementReviewHandler",
    "CorrectingHandler",
    "GitCommittingHandler",
    "CreatingPRHandler",
    "NotifyingHandler",
]
