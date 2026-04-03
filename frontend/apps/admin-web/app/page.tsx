"use client";

import { useEffect, useState } from "react";
import { IntentRouterApiClient } from "@intent-router/api-client";
import type { IntentDefinition, IntentInput } from "@intent-router/shared-types";
import { Badge, Divider, Panel, SectionHeading } from "@intent-router/ui";

const api = new IntentRouterApiClient();

const initialForm: IntentInput = {
  intentCode: "",
  name: "",
  description: "",
  examples: [],
  agentUrl: "mock://",
  status: "active",
  dispatchPriority: 100,
  requestSchema: {
    type: "object",
    required: ["sessionId", "taskId", "intentCode", "input"]
  },
  fieldMapping: {
    sessionId: "$session.id",
    taskId: "$task.id",
    intentCode: "$intent.code",
    input: "$message.current"
  },
  resumePolicy: "resume_same_task"
};

function parseJsonObject(input: string, fallback: Record<string, unknown>): Record<string, unknown> {
  if (!input.trim()) return fallback;
  return JSON.parse(input) as Record<string, unknown>;
}

export default function AdminPage() {
  const [intents, setIntents] = useState<IntentDefinition[]>([]);
  const [form, setForm] = useState(initialForm);
  const [schemaText, setSchemaText] = useState(JSON.stringify(initialForm.requestSchema, null, 2));
  const [mappingText, setMappingText] = useState(JSON.stringify(initialForm.fieldMapping, null, 2));
  const [statusText, setStatusText] = useState("Idle");
  const [errorText, setErrorText] = useState<string | null>(null);

  async function refresh() {
    const next = await api.listIntents();
    setIntents(next);
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function onSubmit() {
    try {
      setErrorText(null);
      setStatusText("Saving intent...");
      await api.createIntent({
        ...form,
        examples: form.examples.filter(Boolean),
        requestSchema: parseJsonObject(schemaText, {}),
        fieldMapping: parseJsonObject(mappingText, {}) as Record<string, string>
      });
      setStatusText("Intent saved.");
      setForm(initialForm);
      setSchemaText(JSON.stringify(initialForm.requestSchema, null, 2));
      setMappingText(JSON.stringify(initialForm.fieldMapping, null, 2));
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      setErrorText(message);
      setStatusText("Save failed.");
    }
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div>
          <h1>Intent Router Admin Console</h1>
          <p>Register intents, map request payloads, and control serial dispatch priority.</p>
        </div>
        <Badge label={statusText} tone={errorText ? "warning" : "success"} />
      </header>

      <main className="main-grid">
        <Panel tone="emphasis" title="Create Intent">
          <SectionHeading
            title="Registry Form"
            subtitle="This drives recognizer grounding, request payload assembly, and task routing."
          />
          <Divider />
          <div className="form-grid">
            <label>
              Intent Code
              <input value={form.intentCode} onChange={(event) => setForm({ ...form, intentCode: event.target.value })} />
            </label>
            <label>
              Name
              <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
            </label>
            <label className="span-2">
              Description
              <textarea
                value={form.description}
                onChange={(event) => setForm({ ...form, description: event.target.value })}
              />
            </label>
            <label className="span-2">
              Examples
              <input
                placeholder="Comma-separated examples"
                value={form.examples.join(", ")}
                onChange={(event) =>
                  setForm({
                    ...form,
                    examples: event.target.value
                      .split(",")
                      .map((item) => item.trim())
                      .filter(Boolean)
                  })
                }
              />
            </label>
            <label>
              Agent URL
              <input value={form.agentUrl} onChange={(event) => setForm({ ...form, agentUrl: event.target.value })} />
            </label>
            <label>
              Dispatch Priority
              <input
                type="number"
                value={form.dispatchPriority}
                onChange={(event) => setForm({ ...form, dispatchPriority: Number(event.target.value) })}
              />
            </label>
            <label>
              Status
              <select
                value={form.status}
                onChange={(event) =>
                  setForm({ ...form, status: event.target.value as IntentInput["status"] })
                }
              >
                <option value="active">active</option>
                <option value="inactive">inactive</option>
                <option value="grayscale">grayscale</option>
              </select>
            </label>
            <label>
              Resume Policy
              <input
                value={form.resumePolicy}
                onChange={(event) => setForm({ ...form, resumePolicy: event.target.value })}
              />
            </label>
            <label className="span-2">
              Request Schema JSON
              <textarea value={schemaText} onChange={(event) => setSchemaText(event.target.value)} />
            </label>
            <label className="span-2">
              Field Mapping JSON
              <textarea value={mappingText} onChange={(event) => setMappingText(event.target.value)} />
            </label>
          </div>
          {errorText ? <p className="error-text">{errorText}</p> : null}
          <button className="primary-button" onClick={onSubmit} type="button">
            Save Intent
          </button>
        </Panel>

        <Panel title="Intent Registry">
          <SectionHeading title="Available Intents" subtitle="The router only recognizes configured intents." />
          <Divider />
          <div className="intent-list">
            {intents.length === 0 ? (
              <small>No intents registered yet. Create one on the left.</small>
            ) : (
              intents.map((intent) => (
                <article key={intent.intentCode} className="intent-card">
                  <div className="line-item">
                    <strong>{intent.intentCode}</strong>
                    <Badge label={intent.status} tone={intent.status === "active" ? "success" : "warning"} />
                  </div>
                  <p>{intent.description}</p>
                  <small>{intent.agentUrl}</small>
                  <div className="line-item">
                    <small>Priority {intent.dispatchPriority}</small>
                    <small>{intent.resumePolicy}</small>
                  </div>
                </article>
              ))
            )}
          </div>
        </Panel>
      </main>
    </div>
  );
}

