# Specification: End-to-End Agent Traceability, Conversation Threading, and Golden Evaluation with LangSmith

Published in Issue Tracker: [#1](https://github.com/nreyesh/data-analyst-ai-agent-backend/issues/1) (Label: `ready-for-agent`)

## Problem Statement

Currently, the conversational financial analytics agent lacks unified observability across its multi-turn conversational lifecycle. When deployed locally or in Google Cloud (Cloud Run), turns are either unmonitored or produce disconnected, isolated traces without an overarching thread identity. As a result, developers and operators cannot inspect the full conversational flow of a user session, lack visibility into the internal decisions of the supervisor and deep analytics sub-graph, cannot monitor token usage and latency of the underlying Google GenAI SDK calls, and have no systematic mechanism to capture user feedback (thumbs up/down) or convert real-world failures into regression test benchmarks.

## Solution

Incorporate end-to-end observability and traceability using LangSmith across local and GCP Cloud Run environments:
1. Establish a standard session-level thread identifier generated at chat start and propagated across every turn, activating LangSmith's native multi-turn Threads dashboard.
2. Structure a hierarchical trace tree where each turn's root run nests child spans for Supervisor routing, Fast-Path tools, the Deep Analytics workflow, and underlying LLM invocations.
3. Instrument Google GenAI SDK calls to capture exact prompt payloads, token counts, latency, and fallback events.
4. Segregate telemetry by environment (`local`, `dev`, `prod`) using separate LangSmith projects, with credentials provisioned securely via Google Secret Manager and Terraform.
5. Provide fail-open asynchronous telemetry delivery and container lifespan flush guarantees on Cloud Run.
6. Enable user feedback actions (thumbs up/down and comments) in the chat UI, sending ratings to LangSmith linked to run IDs.
7. Implement an independent, standalone evaluation harness (`make eval`) that tests agent accuracy against curated golden datasets derived from verified user corrections.

## User Stories

1. As a condominium resident, I want my multi-turn conversation with the financial agent to maintain contextual coherence across several questions, so that I can ask follow-ups without repeating my building or apartment details.
2. As a condominium resident, I want to give thumbs up or thumbs down feedback on an agent's answer, so that I can flag inaccurate calculations or helpful explanations directly in the chat UI.
3. As a condominium resident, I want to submit an optional text comment when giving feedback, so that I can explain why a response was incorrect or what information was missing.
4. As a committee administrator, I want each of my separate chat sessions to be tracked as distinct conversations, so that my queries regarding budget planning don't get mixed up with my queries about maintenance bills.
5. As an AI engineer, I want all turns of a user's chat session to be grouped under a single chronological conversation thread in LangSmith, so that I can inspect the entire conversational history from start to finish in the Threads dashboard.
6. As an AI engineer, I want the root trace in LangSmith to show child spans for the Supervisor routing decision, Fast-Path tool execution, and Deep Financial Analytics sub-graph, so that I can identify exactly which component introduced latency or errors.
7. As an AI engineer, I want Gemini LLM calls made via the Google GenAI SDK to be instrumented with token counts, prompt inputs, and latency metrics, so that I can track token consumption and detect slow generation steps.
8. As an AI engineer, I want LLM fallback executions (primary model error leading to secondary model fallback) to be explicitly recorded in the trace, so that I can monitor fallback frequency and model reliability.
9. As a platform engineer, I want traces to be segregated into distinct LangSmith projects for local development, development, and production environments, so that test queries do not skew production performance and cost analytics.
10. As a security officer, I want authentication tokens, passwords, and sensitive credentials to be excluded from LangSmith run metadata, so that sensitive resident credentials are never stored in external telemetry SaaS.
11. As a security officer, I want user identity attributes (UID, role, knowledge domain) and routing decisions to be attached to trace metadata, so that we have an auditable trail of RBAC guardrail evaluations and security strikes.
12. As a DevOps engineer, I want `LANGSMITH_API_KEY` to be securely managed in Google Secret Manager and deployed via Terraform, so that secrets are not hardcoded or exposed in repository code.
13. As a DevOps engineer, I want the backend service on Cloud Run to flush pending telemetry during container shutdown, so that autoscaling scale-down events do not result in lost traces or user feedback.
14. As an application developer, I want trace dispatching to be asynchronous and fail-open, so that any LangSmith downtime or network issues never cause user chat requests to fail or experience latency spikes.
15. As a QA engineer, I want flagged (thumbs-down) interactions in LangSmith to be easily filtered and tagged for review, so that domain experts can attach verified ground-truth financial figures.
16. As an AI researcher, I want a dedicated evaluation command (`make eval`) that is completely isolated from standard unit tests, so that I can benchmark agent accuracy against the golden evaluation dataset on demand without slowing down regular development testing.
17. As an AI researcher, I want evaluation benchmarks to grade both structural routing/SQL safety and numerical accuracy, so that I can catch subtle accounting calculation regressions before pushing updates to production.

## Implementation Decisions

- **Session and Thread Identification Contract**:
  - The chat turn request contract will be extended to accept a persistent conversation identifier (`conversation_id: str`) generated as a standard UUIDv4 when a chat session starts in the frontend.
  - The backend supervisor turn handler will bind this conversation identifier to LangSmith run metadata as `thread_id` and `session_id`, and inject it into LangGraph runtime configuration under `configurable.thread_id`.
  - The chat turn response contract will be updated to return the execution's unique LangSmith `run_id` to allow downstream feedback referencing.

- **Hierarchical Tracing Structure**:
  - The conversational turn execution will form the root span with run type `chain`.
  - Supervisor routing and individual Fast-Path tool functions will be decorated as child tool or chain runs.
  - The Deep Financial Analytics workflow will run as a child graph, inheriting the parent conversation's thread identifier in its metadata while maintaining its own internal checkpointing identifier.

- **Google GenAI SDK Instrumentation**:
  - Direct invocations to the Gemini API via the Google GenAI SDK will be decorated with LangSmith's traceable decorator with run type `llm`.
  - The decorator will capture input prompts, model names, temperature settings, raw responses, error details, and token usage statistics from generation metadata.

- **Feedback Endpoint and Ingestion**:
  - A new feedback ingestion route will accept the target `run_id`, metric key, numeric/categorical rating (e.g. score `1` or `0`), and optional text comments.
  - The route will asynchronously invoke the LangSmith client to attach the feedback event to the specified run without blocking response delivery.

- **Environment Profiles and Project Segregation**:
  - Application configuration profiles (`local`, `dev`, `prod`) will define environment-specific LangSmith project names (e.g., `building-analytics-local`, `building-analytics-dev`, `building-analytics-prod`).
  - Configuration will support environment variable overrides for API keys and endpoint URLs.

- **Cloud Infrastructure and Secret Management**:
  - Terraform configuration will declare a new Google Secret Manager resource for the LangSmith API key per environment, granting accessor permissions to the Cloud Run execution service account.
  - The backend Cloud Run service definition will bind the secret as an environment variable and set tracing flags.
  - The backend application lifespan shutdown hook will trigger an explicit telemetry flush on the LangSmith client to prevent data loss during container scaling and termination.

- **Dedicated Evaluation Harness**:
  - A standalone CLI evaluation runner will be created, completely decoupled from standard pytest unit and integration suites.
  - The evaluation harness will load examples from the designated LangSmith golden dataset, execute the agent workflow, and run evaluators for route accuracy, SQL syntax validity, and numeric tolerance matching.

## Testing Decisions

- **Good Test Criteria**:
  - Tests will evaluate observable external behavior at system boundaries rather than inspecting private tracer variables or internal SDK state.
  - Tracing assertions will verify that invoking the chat endpoint produces the appropriate response contract containing a valid run identifier, and that mock tracer clients receive properly structured thread and metadata payloads.
  - Tests will assert that missing API keys or simulated network timeouts in the telemetry client fail open silently and do not degrade HTTP response codes or message delivery.

- **Modules and Seams to Test**:
  - *Primary API Seam*: The chat turn endpoint and feedback endpoint, verifying payload contracts, thread propagation, and feedback recording under authenticated user contexts.
  - *Supervisor and Analytics Seam*: Verifying that graph execution passes configurable thread identifiers and child span metadata.
  - *LLM Wrapper Seam*: Verifying fallback execution tracking and token extraction with simulated Gemini responses.
  - *Evaluation Runner Seam*: Testing the evaluation CLI harness against mock dataset records to verify evaluator scoring functions.

- **Prior Art**:
  - Existing API contract and RBAC tests in the backend test suite (`tests/test_api.py`, `tests/test_agent_tools_and_rbac.py`).
  - Existing supervisor and analytics workflow tests (`tests/test_supervisor_and_workflow.py`).

## Out of Scope

- Modifying the core relational database schema in Cloud SQL PostgreSQL to persist user feedback (feedback is stored authoritatively in LangSmith).
- Rewriting the Google GenAI SDK integration to use LangChain's `ChatGoogleGenAI` class.
- Automated automatic promotion of unreviewed negative feedback runs directly into the golden dataset without human intervention.
- Tracing or telemetry on static assets, health check endpoints (`/` and `/healthz`), or database migration jobs.

## Further Notes

- The architecture is designed to be 100% fail-open: telemetry collection must never impact user-facing availability or latency.
- Golden dataset curation will be managed through the LangSmith UI by filtering for `score=0` runs, verifying ground-truth values with condominium administration records, and appending them to the evaluation suite.
