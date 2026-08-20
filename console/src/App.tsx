import { useEffect, useRef, useState } from "react";

type Role = "owner" | "demo_operator";

type Session = {
  email: string;
  role: Role;
  tenant_id: string;
};

type Scenario = {
  id: string;
  purpose: string;
  stream: boolean;
  tools: boolean;
  response_format: string;
  fault: boolean;
};

type LiveSession = {
  session_id: string;
  state: string;
  expires_at: string;
  request_limit: number;
  requests_charged: number;
  spend_limit_micros: number;
  spend_charged_micros: number;
  reserved_spend_micros: number;
  reconciled_spend_micros: number;
};

type LiveConfiguration = {
  proposed_targets: { provider: string; model: string }[];
  provider_processing_notice: string;
};

type Policy = {
  version: string;
  request_limit: number;
  token_limit: number;
  budget_usd_micros: number;
  price_table_version: string;
};

type Operations = {
  policy: Policy | null;
  live_session: LiveSession | null;
  live_configuration: LiveConfiguration;
};

type Receipt = {
  scenarioId: string;
  requestId: string;
  traceId: string;
  route: string;
  provider: string;
  model: string;
  attempts: string;
  costUsd: string;
  usageStatus: string;
  promptTokens?: number;
  completionTokens?: number;
};

type RunPhase = "idle" | "loading" | "streaming" | "complete" | "refused" | "partial" | "error";

type RunState = {
  phase: RunPhase;
  content: string;
  message: string;
  receipt: Receipt | null;
};

const EMPTY_RUN: RunState = {
  phase: "idle",
  content: "",
  message: "Select a committed scenario to begin.",
  receipt: null,
};

function dollars(micros = 0): string {
  return `$${(micros / 1_000_000).toFixed(4)}`;
}

function responseMetadata(response: Response, scenarioId: string): Receipt {
  const rawCost = response.headers.get("x-gateway-cost-usd") ?? "0.000000";
  return {
    scenarioId,
    requestId: response.headers.get("x-request-id") ?? "unavailable",
    traceId: response.headers.get("x-trace-id") ?? "unavailable",
    route: response.headers.get("x-gateway-route") ?? "unavailable",
    provider: response.headers.get("x-gateway-provider") ?? "unavailable",
    model: response.headers.get("x-gateway-model") ?? "unavailable",
    attempts: response.headers.get("x-gateway-attempts") ?? "0",
    costUsd: rawCost.startsWith("$") ? rawCost : `$${rawCost}`,
    usageStatus: response.headers.get("x-gateway-usage-status") ?? "unavailable",
  };
}

async function errorCode(response: Response): Promise<string> {
  try {
    const body = await response.json();
    return body.error?.code ?? "request_failed";
  } catch {
    return "request_failed";
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [run, setRun] = useState<RunState>(EMPTY_RUN);
  const [acknowledged, setAcknowledged] = useState(false);
  const [ownerMessage, setOwnerMessage] = useState("");
  const [secondsRemaining, setSecondsRemaining] = useState(0);
  const runButton = useRef<HTMLButtonElement>(null);

  const selected = scenarios.find((scenario) => scenario.id === selectedId) ?? null;
  const live = operations?.live_session;
  const liveActive = live?.state === "active";
  const proposedTargets = operations?.live_configuration?.proposed_targets ?? [];

  async function loadWorkbench() {
    setLoadState("loading");
    try {
      const [sessionResponse, operationsResponse, scenariosResponse] = await Promise.all([
        fetch("/v1/session", { cache: "no-store" }),
        fetch("/v1/operations/status", { cache: "no-store" }),
        fetch("/v1/workbench/scenarios", { cache: "no-store" }),
      ]);
      if (!sessionResponse.ok || !operationsResponse.ok || !scenariosResponse.ok) {
        throw new Error("workbench_unavailable");
      }
      const nextSession = (await sessionResponse.json()) as Session;
      const nextOperations = (await operationsResponse.json()) as Operations;
      const catalog = (await scenariosResponse.json()) as { scenarios: Scenario[] };
      setSession(nextSession);
      setOperations(nextOperations);
      setScenarios(catalog.scenarios);
      setSelectedId((current) => current || catalog.scenarios[0]?.id || "");
      setLoadState("ready");
    } catch {
      setLoadState("error");
    }
  }

  useEffect(() => {
    void loadWorkbench();
  }, []);

  useEffect(() => {
    if (!liveActive || !live?.expires_at) {
      setSecondsRemaining(0);
      return;
    }
    const update = () => {
      setSecondsRemaining(Math.max(0, Math.ceil((new Date(live.expires_at).getTime() - Date.now()) / 1000)));
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [liveActive, live?.expires_at]);

  useEffect(() => {
    if (["complete", "refused", "partial", "error"].includes(run.phase)) {
      runButton.current?.focus();
    }
  }, [run.phase]);

  async function runScenario() {
    if (!selected) return;
    setRun({ phase: "loading", content: "", message: "Request in progress.", receipt: null });
    try {
      const response = await fetch("/v1/workbench/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workbench-CSRF": "1" },
        body: JSON.stringify({ scenario_id: selected.id }),
      });
      const receipt = responseMetadata(response, selected.id);
      if (!response.ok) {
        const code = await errorCode(response);
        const refused = [403, 429].includes(response.status);
        setRun({
          phase: refused ? "refused" : "error",
          content: "",
          message: refused ? `Request refused: ${code}.` : `Request failed: ${code}.`,
          receipt,
        });
        return;
      }
      if (response.headers.get("content-type")?.includes("text/event-stream")) {
        await readStream(response, receipt);
        return;
      }
      const body = await response.json();
      const message = body.choices?.[0]?.message;
      const content = message?.content ?? JSON.stringify(message?.tool_calls ?? [], null, 2);
      setRun({
        phase: content ? "complete" : "error",
        content,
        message: content ? "Request complete and accounting reconciled." : "The provider returned an empty response.",
        receipt: {
          ...receipt,
          promptTokens: body.usage?.prompt_tokens,
          completionTokens: body.usage?.completion_tokens,
        },
      });
    } catch {
      setRun({ phase: "error", content: "", message: "The workbench could not reach the data plane.", receipt: null });
    }
  }

  async function readStream(response: Response, receipt: Receipt) {
    setRun({ phase: "streaming", content: "", message: "Streaming response in progress.", receipt });
    const reader = response.body?.getReader();
    if (!reader) {
      setRun({ phase: "error", content: "", message: "The response stream was unavailable.", receipt });
      return;
    }
    const decoder = new TextDecoder();
    let buffer = "";
    let content = "";
    let terminalError = "";
    let usage: { prompt_tokens?: number; completion_tokens?: number } = {};
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame
          .split("\n")
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trim())
          .join("");
        if (!data || data === "[DONE]") continue;
        const event = JSON.parse(data);
        if (event.error) {
          terminalError = event.error.code ?? "stream_failed";
          continue;
        }
        const delta = event.choices?.[0]?.delta;
        content += delta?.content ?? "";
        if (delta?.tool_calls) content += JSON.stringify(delta.tool_calls);
        if (event.usage) usage = event.usage;
        setRun({ phase: "streaming", content, message: "Streaming response in progress.", receipt });
      }
      if (done) break;
    }
    setRun({
      phase: terminalError ? (content ? "partial" : "error") : content ? "complete" : "error",
      content,
      message: terminalError
        ? content
          ? `Partial response. Stream ended with ${terminalError}.`
          : `Stream failed: ${terminalError}.`
        : content
          ? "Stream complete and accounting reconciled."
          : "The provider returned an empty stream.",
      receipt: { ...receipt, promptTokens: usage.prompt_tokens, completionTokens: usage.completion_tokens },
    });
  }

  async function changeLiveSession(action: "arm" | "stop") {
    setOwnerMessage(action === "arm" ? "Arming bounded live session." : "Stopping live session.");
    try {
      const response = await fetch("/v1/admin/live-session", {
        method: action === "arm" ? "POST" : "DELETE",
        headers: { "X-Workbench-CSRF": "1" },
      });
      if (!response.ok) {
        setOwnerMessage(`Live-session change failed: ${await errorCode(response)}.`);
        return;
      }
      setAcknowledged(false);
      setOwnerMessage(action === "arm" ? "Live session armed." : "Live session stopped.");
      const statusResponse = await fetch("/v1/operations/status", { cache: "no-store" });
      if (statusResponse.ok) setOperations((await statusResponse.json()) as Operations);
    } catch {
      setOwnerMessage("Live-session controls are unavailable.");
    }
  }

  if (loadState === "loading") {
    return <main className="center-state" aria-busy="true">Loading private workbench.</main>;
  }

  if (loadState === "error" || !session) {
    return (
      <main className="center-state">
        <h1>Workbench unavailable</h1>
        <p>The private session or operational state could not be verified.</p>
        <button type="button" onClick={() => void loadWorkbench()}>Retry connection</button>
      </main>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <strong>Private LLM Gateway</strong>
          <span className="topbar-subtitle">Workbench</span>
        </div>
        <dl className="topbar-status" aria-label="Current session">
          <div><dt>Environment</dt><dd>Private demo</dd></div>
          <div><dt>Provider mode</dt><dd className={liveActive ? "live-status" : "simulator-status"}>{liveActive ? "Live armed" : "Simulator"}</dd></div>
          {liveActive && <div><dt>Time remaining</dt><dd>{Math.floor(secondsRemaining / 60)}:{String(secondsRemaining % 60).padStart(2, "0")}</dd></div>}
          <div><dt>Identity</dt><dd>{session.email}</dd></div>
          <div><dt>Role</dt><dd>{session.role === "demo_operator" ? "Demo operator" : "Owner"}</dd></div>
        </dl>
      </header>

      <main className="workbench">
        <section className="safety-context" aria-labelledby="safety-title">
          <div><strong id="safety-title">Committed synthetic traffic only</strong><span>No arbitrary prompts or tool execution.</span></div>
          <div><strong>Transient content</strong><span>Prompt and response text are never retained.</span></div>
          <div><strong>Live providers</strong><span>{liveActive ? "Owner-armed bounded session." : "Simulator-only; live targets are not active."}</span></div>
        </section>

        <section className="controls panel" aria-labelledby="scenario-heading">
          <div className="section-heading">
            <div><span className="section-kicker">Request</span><h1 id="scenario-heading">Choose a scenario</h1></div>
            <span className="count">{scenarios.length} committed</span>
          </div>
          {scenarios.length === 0 ? (
            <div className="empty-state" role="status">
              <strong>No committed scenarios are available.</strong>
              <span>Ask the owner to restore the reviewed catalog.</span>
            </div>
          ) : (
            <>
              <label htmlFor="scenario">Synthetic scenario</label>
              <select id="scenario" value={selectedId} onChange={(event) => { setSelectedId(event.target.value); setRun(EMPTY_RUN); }}>
                {scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.purpose}</option>)}
              </select>
              {selected && (
                <dl className="scenario-facts">
                  <div><dt>ID</dt><dd>{selected.id}</dd></div>
                  <div><dt>Transport</dt><dd>{selected.stream ? "SSE stream" : "JSON response"}</dd></div>
                  <div><dt>Protocol</dt><dd>{selected.tools ? "Tool call" : selected.response_format === "json_schema" ? "Strict JSON schema" : "Text"}</dd></div>
                  <div><dt>Expected path</dt><dd>{selected.fault ? "Fault injection" : "Successful completion"}</dd></div>
                </dl>
              )}
              <button ref={runButton} className="primary-action" type="button" onClick={() => void runScenario()} disabled={run.phase === "loading" || run.phase === "streaming"}>
                {run.phase === "loading" || run.phase === "streaming" ? "Request running" : "Run committed scenario"}
              </button>
            </>
          )}
        </section>

        <section className={`response panel state-${run.phase}`} aria-labelledby="response-heading">
          <div className="section-heading">
            <div><span className="section-kicker">Transient</span><h2 id="response-heading">Response</h2></div>
            <span className="state-label">{run.phase}</span>
          </div>
          <p className="run-message" role={run.phase === "error" || run.phase === "refused" || run.phase === "partial" ? "alert" : "status"} aria-live="polite">
            {run.message}
          </p>
          {run.content ? <pre className="response-content" aria-label="Transient provider response">{run.content}</pre> : <div className="response-placeholder" aria-hidden="true" />}
          {["refused", "partial", "error", "complete"].includes(run.phase) && scenarios.length > 0 && (
            <button className="secondary-action" type="button" onClick={() => void runScenario()}>Run again</button>
          )}
        </section>

        <section className="receipt panel" aria-labelledby="receipt-heading">
          <div className="section-heading">
            <div><span className="section-kicker">Retained metadata</span><h2 id="receipt-heading">Receipt</h2></div>
          </div>
          {run.receipt ? (
            <dl className="receipt-grid">
              <div><dt>Request</dt><dd>{run.receipt.requestId}</dd></div>
              <div><dt>Trace</dt><dd>{run.receipt.traceId}</dd></div>
              <div><dt>Scenario</dt><dd>{run.receipt.scenarioId}</dd></div>
              <div><dt>Route</dt><dd>{run.receipt.route}</dd></div>
              <div><dt>Provider</dt><dd>{run.receipt.provider}</dd></div>
              <div><dt>Model</dt><dd>{run.receipt.model}</dd></div>
              <div><dt>Attempts</dt><dd>{run.receipt.attempts}</dd></div>
              <div><dt>Usage</dt><dd>{run.receipt.usageStatus}</dd></div>
              <div><dt>Tokens</dt><dd>{run.receipt.promptTokens ?? "pending"} in / {run.receipt.completionTokens ?? "pending"} out</dd></div>
              <div><dt>Cost</dt><dd>{run.receipt.costUsd}</dd></div>
            </dl>
          ) : (
            <div className="empty-state" role="status"><strong>No receipt yet.</strong><span>Metadata appears after admission.</span></div>
          )}
        </section>

        <aside className="operations panel" aria-labelledby="operations-heading">
          <div className="section-heading">
            <div><span className="section-kicker">Controls</span><h2 id="operations-heading">Operating state</h2></div>
          </div>
          <dl className="operations-list">
            <div><dt>Policy</dt><dd>{operations?.policy?.version ?? "Unavailable"}</dd></div>
            <div><dt>Request limit</dt><dd>{operations?.policy?.request_limit ?? "Unavailable"} / UTC hour</dd></div>
            <div><dt>Token limit</dt><dd>{operations?.policy?.token_limit?.toLocaleString() ?? "Unavailable"} / UTC hour</dd></div>
            <div><dt>Budget</dt><dd>{dollars(operations?.policy?.budget_usd_micros)} / UTC hour</dd></div>
            <div><dt>Price table</dt><dd>{operations?.policy?.price_table_version ?? "Unavailable"}</dd></div>
            <div><dt>Proposed targets</dt><dd>{proposedTargets.length ? proposedTargets.map((target) => `${target.provider} / ${target.model}`).join("; ") : "Unavailable"}</dd></div>
          </dl>
          <p className="role-note">{operations?.live_configuration?.provider_processing_notice ?? "Provider processing disclosure is unavailable; live mode must remain disabled."}</p>

          {session.role === "owner" ? (
            <section className="owner-controls" aria-labelledby="owner-heading">
              <h3 id="owner-heading">Owner live-session control</h3>
              {liveActive ? (
                <>
                  <dl className="live-counters">
                    <div><dt>Provider attempts</dt><dd>{live.requests_charged} / {live.request_limit}</dd></div>
                    <div><dt>Reserved spend</dt><dd>{dollars(live.reserved_spend_micros)}</dd></div>
                    <div><dt>Reconciled spend</dt><dd>{dollars(live.reconciled_spend_micros)}</dd></div>
                    <div><dt>Charged spend</dt><dd>{dollars(live.spend_charged_micros)} / {dollars(live.spend_limit_micros)}</dd></div>
                  </dl>
                  <button className="danger-action" type="button" onClick={() => {
                    if (window.confirm(`Stop live session ${live.session_id}?`)) void changeLiveSession("stop");
                  }}>Stop live session</button>
                </>
              ) : (
                <>
                  <p>Live providers remain disabled unless you acknowledge every fixed cap.</p>
                  <label className="acknowledgment">
                    <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
                    <span>I acknowledge the session ends at 30 minutes, 20 provider requests, or $1.00, whichever occurs first.</span>
                  </label>
                  <button className="warning-action" type="button" disabled={!acknowledged} onClick={() => void changeLiveSession("arm")}>Arm bounded live session</button>
                </>
              )}
              {ownerMessage && <p className="owner-message" role="status" aria-live="polite">{ownerMessage}</p>}
            </section>
          ) : (
            <p className="role-note">Read-only demo access. User, key, policy, route, budget, and live-session mutations require the owner role.</p>
          )}
        </aside>
      </main>
    </div>
  );
}
