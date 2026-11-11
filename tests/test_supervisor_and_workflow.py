"""Integration and unit tests for the Conversational Supervisor, 9-Node Analytics Workflow, Sandbox, and Frontend."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy.orm import Session

from backend.app.agent.analytics.config import AnalyticsSettings
from backend.app.agent.analytics.nodes import (
    SQLCoderOutput,
    VisualizerOutput,
    generate_content_with_fallback,
    planner_node,
    sandbox_runner_node,
    sql_coder_node,
    sql_gateway_node,
    synthesizer_node,
    trajectory_evaluator_node,
    trajectory_inspector_node,
    visualizer_node,
)
from backend.app.agent.analytics.sandbox import run_plotting_sandbox
from backend.app.agent.analytics.state import (
    AnalyticsResult,
    DataAnalystState,
    IntentPlan,
    SQLSubQuery,
    TrajectoryEvaluation,
)
from backend.app.agent.analytics.tools.building_tools import (
    get_building_summary,
    get_system_diagnostics,
)
from backend.app.agent.analytics.tools.unit_tools import get_unit_bill_details
from backend.app.agent.analytics.workflow import (
    build_analytics_graph,
    route_after_evaluator,
    route_after_inspector,
    route_after_planner,
    route_after_sandbox,
    route_after_sql_gateway,
    run_analysis,
)
from backend.app.agent.state import (
    FocusContext,
    SupervisorDecision,
    SupervisorTurnResult,
    UserContext,
)
from backend.app.agent.supervisor import MAX_EPHEMERAL_MESSAGES, run_supervisor_turn


@pytest.fixture(autouse=True)
def isolate_offline_llm_env(monkeypatch):
    """Ensure supervisor tests run offline deterministically unless a mock llm_client is passed."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(
        "backend.app.agent.supervisor.get_analytics_settings",
        lambda: MagicMock(gemini_api_key=""),
    )


@pytest.fixture
def resident_b504() -> UserContext:
    return UserContext(
        uid="uid_res_504b",
        email="residente504b@condominio.cl",
        role="resident",
        unit_code="B-504",
    )


@pytest.fixture
def admin_user() -> UserContext:
    return UserContext(
        uid="uid_admin_001",
        email="admin@condominio.cl",
        role="admin",
        unit_code=None,
    )


def make_mock_llm(decisions: list[SupervisorDecision] | SupervisorDecision) -> MagicMock:
    """Helper to mock Gemini client returning serialized SupervisorDecision JSON."""
    mock_llm = MagicMock()
    if isinstance(decisions, list):
        mock_llm.models.generate_content.side_effect = [
            MagicMock(text=d.model_dump_json()) for d in decisions
        ]
    else:
        mock_llm.models.generate_content.return_value = MagicMock(
            text=decisions.model_dump_json()
        )
    return mock_llm


# ============================================================================
# 1. Conversational Supervisor Tests
# ============================================================================


def test_supervisor_fastpath_unit_and_building_and_anaphora(
    seeded_db: Session,
    resident_b504: UserContext,
    admin_user: UserContext,
):
    """Verify unit fast-path, building fast-path, and 'mes anterior' follow-up."""
    mock_llm_unit = make_mock_llm(
        SupervisorDecision(
            route="lookup_unit_bill",
            reasoning="Unit bill lookup",
            resolved_query="¿Cuánto debo pagar en agosto 2026?",
            knowledge_domain="unit",
            unit_code="B-504",
            period_date="2026-08-01",
        )
    )
    turn_u, msgs_u, focus_u = run_supervisor_turn(
        user_message="¿Cuánto debo pagar en agosto 2026?",
        user_context=resident_b504,
        llm_client=mock_llm_unit,
        http_client=seeded_db,
    )
    assert turn_u.route == "lookup_unit_bill"
    assert "B-504" in turn_u.reply
    assert "2026-08-01" in turn_u.reply
    assert focus_u.last_period == "2026-08-01"

    # Building fast-path + follow-up
    mock_llm_bldg = make_mock_llm([
        SupervisorDecision(
            route="lookup_building_summary",
            reasoning="Building summary",
            resolved_query="Gasto común edificio septiembre 2026",
            knowledge_domain="building",
            period_date="2026-09-01",
        ),
        SupervisorDecision(
            route="lookup_building_summary",
            reasoning="Follow up mes anterior",
            resolved_query="¿Y el mes anterior?",
            knowledge_domain="building",
        ),
    ])
    t1, m1, f1 = run_supervisor_turn(
        user_message="¿Cuál fue el gasto del edificio en septiembre 2026?",
        user_context=admin_user,
        llm_client=mock_llm_bldg,
        http_client=seeded_db,
    )
    assert t1.route == "lookup_building_summary"
    assert f1.last_period == "2026-09-01"

    t2, m2, f2 = run_supervisor_turn(
        user_message="¿Y el mes anterior?",
        user_context=admin_user,
        messages=m1,
        focus=f1,
        llm_client=mock_llm_bldg,
        http_client=seeded_db,
    )
    assert t2.route == "lookup_building_summary"
    assert f2.last_period == "2026-08-01"
    assert len(m2) == 4


def test_supervisor_clarifications_and_3nf_tower_lookup(
    seeded_db: Session,
    resident_b504: UserContext,
    admin_user: UserContext,
):
    """Verify Turn 1 scope clarification and 3NF `units` table tower disambiguation."""
    # 1. Bare unit "504" has both A-504 and B-504 in seeded 3NF `units` table -> asks which tower
    mock_llm = make_mock_llm(
        SupervisorDecision(
            route="lookup_unit_bill",
            reasoning="Admin asking for unit 504 without tower",
            resolved_query="Muéstrame la boleta del depto 504",
            knowledge_domain="unit",
        )
    )
    t_tower, m_tower, f_tower = run_supervisor_turn(
        user_message="Muéstrame la boleta del depto 504",
        user_context=admin_user,
        llm_client=mock_llm,
        http_client=seeded_db,
    )
    assert t_tower.route == "clarify_intent"
    assert t_tower.needs_clarification is True
    assert "Torre A (A-504)" in t_tower.candidate_options
    assert "Torre B (B-504)" in t_tower.candidate_options

    # Admin selects "Torre B (B-504)" -> resolves to B-504
    t_resumed, _, f_resumed = run_supervisor_turn(
        user_message="Torre B (B-504)",
        user_context=admin_user,
        messages=m_tower,
        focus=f_tower,
        clarification_choice="Torre B (B-504)",
        llm_client=mock_llm,
        http_client=seeded_db,
    )
    assert t_resumed.route == "lookup_unit_bill"
    assert "B-504" in t_resumed.reply
    assert f_resumed.last_unit == "B-504"

    # 2. Bare unit "506" only exists in Tower B (`B-506`) in seeded 3NF `units` table -> auto-resolves!
    mock_llm_506 = make_mock_llm(
        SupervisorDecision(
            route="lookup_unit_bill",
            reasoning="Admin asking for unit 506",
            resolved_query="Boleta del depto 506",
            knowledge_domain="unit",
        )
    )
    t_506, _, _ = run_supervisor_turn(
        user_message="Boleta del depto 506",
        user_context=admin_user,
        llm_client=mock_llm_506,
        http_client=seeded_db,
    )
    assert t_506.route == "lookup_unit_bill"
    assert t_506.needs_clarification is False
    assert "B-506" in t_506.reply

    # 3. Resident Turn 1 scope clarification & selecting "Ambos" (cross_domain)
    mock_llm_res_clarify = make_mock_llm(
        SupervisorDecision(
            route="clarify_intent",
            reasoning="Ambiguous scope",
            resolved_query="¿Por qué subió el gasto común este año?",
            needs_clarification=True,
        )
    )
    mock_wf = MagicMock(
        return_value=AnalyticsResult(
            summary="El alza en B-504 se explica por mantenciones del edificio [Septiembre 2026].",
            is_authorized=True,
            needs_clarification=False,
        )
    )
    tc1, mc1, fc1 = run_supervisor_turn(
        user_message="¿Por qué subió el gasto común este año?",
        user_context=resident_b504,
        llm_client=mock_llm_res_clarify,
        http_client=seeded_db,
        workflow_fn=mock_wf,
    )
    assert tc1.route == "clarify_intent"
    assert len(tc1.candidate_options) == 3

    tc2, _, fc2 = run_supervisor_turn(
        user_message="Ambos (impacto del edificio en mi unidad B-504)",
        user_context=resident_b504,
        messages=mc1,
        focus=fc1,
        clarification_choice="Ambos (impacto del edificio en mi unidad B-504)",
        llm_client=mock_llm_res_clarify,
        http_client=seeded_db,
        workflow_fn=mock_wf,
    )
    assert tc2.route == "run_deep_financial_analysis"
    assert fc2.last_domain == "cross_domain"

    # 4. Admin two-step unit selection flow
    mock_llm_adm_clarify = make_mock_llm(
        SupervisorDecision(
            route="clarify_intent",
            reasoning="Admin ambiguous scope",
            resolved_query="Detalle gasto común agosto 2026",
            period_date="2026-08-01",
            needs_clarification=True,
        )
    )
    ta1, ma1, fa1 = run_supervisor_turn(
        user_message="Detalle gasto común agosto 2026",
        user_context=admin_user,
        llm_client=mock_llm_adm_clarify,
        http_client=seeded_db,
    )
    ta2, ma2, fa2 = run_supervisor_turn(
        user_message="Consultar un departamento específico",
        user_context=admin_user,
        messages=ma1,
        focus=fa1,
        clarification_choice="Consultar un departamento específico",
        llm_client=mock_llm_adm_clarify,
        http_client=seeded_db,
    )
    assert ta2.route == "clarify_intent"
    assert "código del departamento" in ta2.reply
    ta3, _, fa3 = run_supervisor_turn(
        user_message="B-504",
        user_context=admin_user,
        messages=ma2,
        focus=fa2,
        llm_client=mock_llm_adm_clarify,
        http_client=seeded_db,
    )
    assert ta3.route == "lookup_unit_bill"
    assert fa3.last_unit == "B-504"


def test_supervisor_spend_evolution_and_greeting_and_system_status(
    seeded_db: Session,
    resident_b504: UserContext,
    admin_user: UserContext,
):
    """Verify spend evolution route with chart_base64, pure greeting, and system status."""
    # Greeting
    t_greet, _, _ = run_supervisor_turn(
        user_message="hola",
        user_context=resident_b504,
        http_client=seeded_db,
    )
    assert t_greet.route == "direct_response"
    assert "¡Hola!" in t_greet.reply

    # Spend evolution
    mock_llm_evo = make_mock_llm(
        SupervisorDecision(
            route="lookup_spend_evolution",
            reasoning="Evolución mantenciones",
            resolved_query="¿Cómo ha evolucionado el gasto en mantenciones?",
            knowledge_domain="building",
            target_concept="mantenciones y servicios",
        )
    )
    t_evo, _, _ = run_supervisor_turn(
        user_message="¿Cómo ha evolucionado el gasto en mantenciones?",
        user_context=resident_b504,
        llm_client=mock_llm_evo,
        http_client=seeded_db,
    )
    assert t_evo.route == "lookup_spend_evolution"
    assert t_evo.chart_base64 is not None
    if t_evo.chart_file_path:
        Path(t_evo.chart_file_path).unlink(missing_ok=True)

    # System status + 8-message window cap
    mock_llm_sys = make_mock_llm(
        SupervisorDecision(
            route="check_system_status",
            reasoning="Estado del sistema",
            resolved_query="Estado del sistema",
        )
    )
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
    t_sys, msgs_sys, _ = run_supervisor_turn(
        user_message="¿Cuál es el estado del sistema?",
        user_context=admin_user,
        messages=history,
        llm_client=mock_llm_sys,
        http_client=seeded_db,
    )
    assert t_sys.route == "check_system_status"
    assert "Diagnóstico del Sistema" in t_sys.reply
    assert len(msgs_sys) == MAX_EPHEMERAL_MESSAGES


# ============================================================================
# 2. 9-Node Analytics Workflow & Sandbox Tests
# ============================================================================


def test_analytics_workflow_nodes_and_routing(seeded_db: Session):
    """Verify routing logic, planner guards, trajectory inspector (3NF), evaluator, SQL coder, visualizer, sandbox, and synthesizer."""
    graph = build_analytics_graph()
    assert "planner" in graph.nodes
    assert "trajectory_inspector" in graph.nodes
    assert "sql_coder" in graph.nodes

    # Routing checks
    assert (
        route_after_planner(
            {"intent_plan": IntentPlan(is_authorized_query=False, refusal_reason="No")}
        )
        == "synthesizer"
    )
    assert (
        route_after_planner(
            {"intent_plan": IntentPlan(is_authorized_query=True, analysis_type="variance_driver")}
        )
        == "trajectory_inspector"
    )
    assert (
        route_after_planner(
            {"intent_plan": IntentPlan(is_authorized_query=True, analysis_type="scalar_lookup")}
        )
        == "sql_coder"
    )

    # Planner unit-first focus & granularity guard
    mock_planner_client = MagicMock()
    mock_planner_client.models.generate_content.return_value = MagicMock(
        text=IntentPlan(
            is_authorized_query=True,
            knowledge_domain="cross_domain",
            analysis_goal="Evolución 506b",
            sub_analyses=["Edificio", "Unidad B-506"],
            target_unit="506B",
        ).model_dump_json()
    )
    p_out = planner_node(
        {"user_query": "muestrame la evolucion del gasto comun para el depto 506b"},
        client=mock_planner_client,
    )
    assert p_out["intent_plan"].knowledge_domain == "unit"
    assert p_out["intent_plan"].target_unit == "B-506"

    # Hop 1 Trajectory Inspector on 3NF DB (building & unit)
    insp_bldg = trajectory_inspector_node(
        {
            "user_query": "¿Por qué subió el gasto del edificio?",
            "intent_plan": IntentPlan(
                is_authorized_query=True,
                knowledge_domain="building",
                analysis_type="variance_driver",
            ),
        }
    )
    assert insp_bldg["macro_trajectory"] is not None
    assert len(insp_bldg["macro_trajectory"]) == 3
    assert route_after_inspector(insp_bldg) == "trajectory_evaluator"

    insp_unit = trajectory_inspector_node(
        {
            "user_query": "¿Por qué subió el gasto del depto B-504?",
            "intent_plan": IntentPlan(
                is_authorized_query=True,
                knowledge_domain="unit",
                analysis_type="variance_driver",
                unit_tower="B",
                unit_number=504,
                target_unit="B-504",
            ),
        }
    )
    assert insp_unit["macro_trajectory"] is not None
    assert len(insp_unit["macro_trajectory"]) == 3

    # Trajectory Evaluator
    mock_eval_client = MagicMock()
    mock_eval_client.models.generate_content.return_value = MagicMock(
        text=TrajectoryEvaluation(
            premise_status="validated",
            evaluation_summary="Alza confirmada en septiembre 2026",
            clarification_message=None,
            candidate_options=[],
        ).model_dump_json()
    )
    eval_out = trajectory_evaluator_node(
        {
            "user_query": "¿Por qué subió en septiembre?",
            "intent_plan": IntentPlan(is_authorized_query=True, analysis_type="variance_driver"),
            "macro_trajectory": insp_bldg["macro_trajectory"],
        },
        client=mock_eval_client,
    )
    assert eval_out["trajectory_evaluation"].premise_status == "validated"
    assert route_after_evaluator(eval_out) == "sql_coder"

    # SQL Coder & SQL Gateway
    mock_sql_client = MagicMock()
    mock_sql_client.models.generate_content.return_value = MagicMock(
        text=SQLCoderOutput(
            reasoning="Query 3NF unit bills",
            queries=[
                SQLSubQuery(
                    query_id="unit_trend",
                    title="Evolución B-504",
                    sql_query=(
                        "SELECT b.period_date, b.calculated_dept_subtotal, "
                        "b.calculated_consumo_total, b.calculated_unit_subtotal, b.calculated_total_month "
                        "FROM unit_bills b JOIN units u ON b.unit_code = u.unit_code "
                        "WHERE u.unit_code = 'B-504' ORDER BY b.period_date ASC;"
                    ),
                )
            ],
        ).model_dump_json()
    )
    sql_state: DataAnalystState = {
        "user_query": "Evolución B-504",
        "intent_plan": IntentPlan(
            is_authorized_query=True,
            knowledge_domain="unit",
            analysis_type="time_series_trend",
            requires_visualization=True,
            target_unit="B-504",
        ),
        "retry_count": 0,
        "max_retries": 2,
        "error_history": [],
    }
    coded = sql_coder_node(sql_state, client=mock_sql_client)
    sql_state.update(coded)
    gw_out = sql_gateway_node(sql_state)
    sql_state.update(gw_out)
    assert sql_state["sql_results"] is not None
    assert len(sql_state["sql_results"]) == 3
    assert route_after_sql_gateway(sql_state) == "visualizer"

    # Visualizer & Sandbox Runner
    valid_plot_code = """
import json
import matplotlib.pyplot as plt
with open("dataset.json", "r", encoding="utf-8") as f:
    data = json.load(f)
plt.figure(figsize=(6, 3))
plt.plot([r["period_date"] for r in data], [r["calculated_total_month"] for r in data])
plt.savefig("output.png")
"""
    mock_vis_client = MagicMock()
    mock_vis_client.models.generate_content.return_value = MagicMock(
        text=VisualizerOutput(reasoning="Plot", python_code=valid_plot_code).model_dump_json()
    )
    vis_out = visualizer_node(sql_state, client=mock_vis_client)
    sql_state.update(vis_out)
    sb_out = sandbox_runner_node(sql_state)
    sql_state.update(sb_out)
    assert sql_state["chart_file_path"] is not None
    assert route_after_sandbox(sql_state) == "synthesizer"

    # Sandbox error paths (missing output.png & syntax/runtime error)
    sb_noop = run_plotting_sandbox("print('no image saved')", [{"a": 1}])
    assert sb_noop.success is False
    sb_err = run_plotting_sandbox("raise ValueError('bad plot')", [{"a": 1}])
    assert sb_err.success is False

    # Synthesizer
    mock_syn_client = MagicMock()
    mock_syn_client.models.generate_content.return_value = MagicMock(
        text="El gasto común de la unidad B-504 fue de $132.000 CLP en [Septiembre 2026]."
    )
    syn_out = synthesizer_node(sql_state, client=mock_syn_client)
    assert "B-504" in syn_out["final_markdown"]
    if sql_state["chart_file_path"]:
        Path(sql_state["chart_file_path"]).unlink(missing_ok=True)


def test_end_to_end_run_analysis_and_endpoint(seeded_client, monkeypatch):
    """Verify run_analysis and POST /api/v1/analytics/query end-to-end with mocked Gemini client."""
    plan_json = IntentPlan(
        is_authorized_query=True,
        knowledge_domain="building",
        analysis_type="scalar_lookup",
        temporal_resolution="snapshot",
        analysis_goal="Total septiembre 2026",
        relevant_tables=["expense_reports"],
        time_scope="2026-09-01",
        requires_visualization=False,
    ).model_dump_json()
    sql_json = SQLCoderOutput(
        reasoning="Select grand total",
        sql_query="SELECT period_date, calculated_grand_total FROM expense_reports WHERE period_date = '2026-09-01';",
    ).model_dump_json()
    syn_text = "El gasto total en [Septiembre 2026] fue de $51.000.000 CLP."

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        MagicMock(text=plan_json),
        MagicMock(text=sql_json),
        MagicMock(text=syn_text),
    ]
    monkeypatch.setattr("backend.app.agent.analytics.nodes.get_gemini_client", lambda: mock_client)

    resp = seeded_client.post(
        "/api/v1/analytics/query",
        json={"query": "¿Cuál fue el gasto total en septiembre 2026?"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "$51.000.000 CLP" in body["summary"]
    assert body["is_authorized"] is True


def test_http_fallback_paths_for_tools():
    """Verify HTTP client fallback paths in unit_tools and building_tools."""
    mock_http = MagicMock()
    mock_http.get.side_effect = ConnectionError("Connection refused")
    assert "error" in get_unit_bill_details("B-504", client=mock_http)
    assert "error" in get_building_summary(client=mock_http)
    assert "error" in get_system_diagnostics(client=mock_http)


# ============================================================================
# 3. Frontend Auth, API Client, and Chainlit UI Rendering Tests
# ============================================================================


def test_frontend_auth_token_store_and_ui_rendering(monkeypatch):
    """Verify front/auth.py server-side token store and front/app.py base64 chart rendering."""
    chainlit_app = pytest.importorskip("front.app")
    front_auth = pytest.importorskip("front.auth")
    get_user_id_token = front_auth.get_user_id_token
    verify_user_credentials = front_auth.verify_user_credentials

    monkeypatch.setenv("FIREBASE_API_KEY", "")
    user_ctx = verify_user_credentials("residente504@sierraplaza.cl", "SierraPlaza2026!")
    assert user_ctx is not None
    assert user_ctx.role == "resident"
    assert user_ctx.unit_code == "504"
    # Verify token is stored in server-side memory and NOT in UserContext
    assert not hasattr(user_ctx, "id_token")
    stored_token = get_user_id_token(user_ctx.uid)
    assert stored_token is not None
    assert stored_token.startswith("dev-token:uid_res_504:residente504@sierraplaza.cl:resident:504")

    # Test Chainlit lifecycle & base64 chart rendering
    cl_user = chainlit_app.password_auth_callback("residente504@sierraplaza.cl", "SierraPlaza2026!")
    assert cl_user is not None
    chainlit_app.cl.user_session.clear()
    chainlit_app.cl.user_session.set("user", cl_user)
    welcome = asyncio.run(chainlit_app.on_chat_start())
    assert "Residente" in welcome.content

    fake_png = b"\x89PNG\r\n\x1a\nfake_png_bytes"
    fake_b64 = base64.b64encode(fake_png).decode("ascii")

    async def fake_async_runner(
        user_message: str,
        user_context,
        messages=None,
        focus=None,
        clarification_choice=None,
        status_callback=None,
    ):
        if status_callback:
            status_callback("Generando gráfico...")
        res = chainlit_app.SupervisorTurnResult(
            reply="Evolución lista.",
            route="lookup_spend_evolution",
            step_name="Evolución consultada",
            chart_base64=fake_b64,
        )
        return res, [], focus

    sent_msg = asyncio.run(
        chainlit_app.on_message(
            SimpleNamespace(content="Evolución de mi gasto"),
            supervisor_runner=fake_async_runner,
        )
    )
    assert "Evolución lista." in sent_msg.content
    assert len(sent_msg.elements) == 1
    assert sent_msg.elements[0].content == fake_png
