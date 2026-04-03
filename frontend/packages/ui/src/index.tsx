import type { PropsWithChildren } from "react";

type PanelTone = "default" | "emphasis" | "success" | "warning";

export function SectionHeading(props: { title: string; subtitle?: string }) {
  return (
    <header style={{ display: "grid", gap: 4 }}>
      <h2 style={{ margin: 0, fontSize: "1.1rem", letterSpacing: "0.01em" }}>{props.title}</h2>
      {props.subtitle ? (
        <p style={{ margin: 0, fontSize: "0.86rem", opacity: 0.74 }}>{props.subtitle}</p>
      ) : null}
    </header>
  );
}

export function Panel(props: PropsWithChildren<{ tone?: PanelTone; title?: string }>) {
  const tone = props.tone ?? "default";
  const borderColor =
    tone === "emphasis"
      ? "rgba(248, 154, 68, 0.75)"
      : tone === "success"
        ? "rgba(55, 166, 123, 0.8)"
        : tone === "warning"
          ? "rgba(202, 101, 84, 0.8)"
          : "rgba(64, 74, 89, 0.55)";

  return (
    <section
      style={{
        borderRadius: 18,
        border: `1px solid ${borderColor}`,
        background: "linear-gradient(180deg, rgba(19, 24, 34, 0.94), rgba(13, 17, 24, 0.94))",
        boxShadow: "0 18px 42px rgba(0, 0, 0, 0.3)",
        padding: 14,
        display: "grid",
        gap: 10
      }}
    >
      {props.title ? <h3 style={{ margin: 0, fontSize: "0.95rem", opacity: 0.95 }}>{props.title}</h3> : null}
      {props.children}
    </section>
  );
}

export function Badge(props: { label: string; tone?: PanelTone }) {
  const tone = props.tone ?? "default";
  const color =
    tone === "emphasis"
      ? "rgba(255, 182, 107, 0.92)"
      : tone === "success"
        ? "rgba(122, 224, 166, 0.92)"
        : tone === "warning"
          ? "rgba(251, 151, 117, 0.9)"
          : "rgba(193, 207, 227, 0.92)";
  return (
    <span
      style={{
        fontSize: "0.73rem",
        letterSpacing: "0.07em",
        textTransform: "uppercase",
        color,
        padding: "4px 8px",
        borderRadius: 999,
        border: `1px solid ${color.replace("0.92", "0.45").replace("0.9", "0.45")}`,
        width: "fit-content"
      }}
    >
      {props.label}
    </span>
  );
}

export function Divider() {
  return <hr style={{ margin: 0, border: "none", borderTop: "1px solid rgba(75, 90, 108, 0.45)" }} />;
}

