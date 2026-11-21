import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Editor from "@monaco-editor/react";
import {
  API,
  UNAUTHORIZED_EVENT,
  clearAccessPassword,
  getAccessPassword,
  setAccessPassword,
} from "../utils/api";

type VCDVariable = {
  id: string;
  name: string;
  fullName: string;
  width: number;
};

type VCDSignal = {
  variable: VCDVariable;
  events: { time: number; value: string }[];
};

type SimulationResult = {
  success?: boolean;
  output?: string;
  error?: string;
  vcd?: string;
  waveform?: string;
  vcd_content?: string;
  warnings?: string;
  waveform_error?: string;
  verdict?: "PASS" | "FAIL" | "NO_TESTS" | "UNVERIFIED" | "ERROR";
  total_tests?: number | null;
  failures?: number | null;
};

// Compile / dry-run / auto-repair report returned by /generate and
// /generate-testbench.
type Verification = {
  compiled: boolean;
  ready: boolean;
  verdict?: string | null;
  total_tests?: number | null;
  failures?: number | null;
  repair_attempts: number;
  attempts?: number;
  problems?: string | null;
  log?: string[];
  method?: "template" | "free-form";
  independent?: boolean;
  model?: string;
  gave_up?: boolean;
  message?: string;
};

// True only when the text contains a complete Verilog module.
const isVerilogSource = (code?: string | null): boolean => {
  if (!code) return false;
  const withoutComments = code
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/\/\/[^\n]*/g, " ");
  return (
    /\bmodule\s+[A-Za-z_][\w$]*/.test(withoutComments) &&
    /\bendmodule\b/.test(withoutComments)
  );
};

const describeVerification = (v?: Verification | null): string => {
  if (!v) return "";
  const tries =
    v.attempts && v.attempts > 1
      ? ` after ${v.attempts} attempts`
      : v.repair_attempts > 0
        ? ` after ${v.repair_attempts} automatic repair${v.repair_attempts === 1 ? "" : "s"}`
        : "";
  const how =
    v.method === "template"
      ? v.independent
        ? "Testbench built from the design's ports; expected values were written separately from the design, so PASS means two independent versions agree."
        : "Testbench built from the design's ports; expected values were derived from your design."
      : "";
  if (v.ready) {
    const result =
      v.verdict === "PASS"
        ? `all ${v.total_tests} tests passed`
        : `${v.failures} of ${v.total_tests} tests failed`;
    const lines = [
      `Compiled with Icarus Verilog${tries}. Pre-check: ${result}.`,
      how,
      v.verdict === "FAIL"
        ? "The FAIL lines show the inputs where the design and the expected values differ; check them against what you asked for."
        : "",
      "Run Simulation to see the full output and waveform.",
    ];
    if (v.verdict === "FAIL" && v.log?.length) lines.push("", "Log:", ...v.log);
    return lines.filter((line, index) => line || index > 3).join("\n");
  }
  return [
    `Code still has problems${tries}${v.compiled ? " (it compiles)" : " (it does not compile)"}:`,
    v.problems || "",
    ...(v.log && v.log.length ? ["", "Log:", ...v.log] : []),
  ].join("\n");
};

type ProjectItem = {
  name: string;
  path: string;
  type: "file" | "folder";
};

function configureMonaco(monaco: any) {
  monaco.editor.defineTheme("logicforge-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "6C6C75", fontStyle: "italic" },
      { token: "keyword", foreground: "F0A43A" },
      { token: "number", foreground: "8AB4E8" },
      { token: "string", foreground: "9CC79B" },
      { token: "identifier", foreground: "E4E4E7" },
      { token: "delimiter", foreground: "A0A0A8" },
    ],
    colors: {
      "editor.background": "#0E0E10",
      "editor.foreground": "#ECECEE",
      "editorLineNumber.foreground": "#45454C",
      "editorLineNumber.activeForeground": "#A0A0A8",
      "editorCursor.foreground": "#F0A43A",
      "editor.selectionBackground": "#2C2C33",
      "editor.inactiveSelectionBackground": "#222227",
      "editor.lineHighlightBackground": "#141417",
      "editor.lineHighlightBorder": "#00000000",
      "editorIndentGuide.background1": "#1F1F23",
      "editorIndentGuide.activeBackground1": "#303036",
      "editorWidget.background": "#161619",
      "editorWidget.border": "#303036",
      "editorGutter.background": "#0E0E10",
      "scrollbarSlider.background": "#30303680",
      "scrollbarSlider.hoverBackground": "#3A3A42",
    },
  });
}

const EDITOR_OPTIONS = {
  minimap: { enabled: false },
  fontSize: 13,
  lineHeight: 21,
  fontFamily: "'SF Mono', ui-monospace, Menlo, Consolas, monospace",
  automaticLayout: true,
  scrollBeyondLastLine: false,
  padding: { top: 12, bottom: 12 },
  renderLineHighlight: "line" as const,
  overviewRulerLanes: 0,
  hideCursorInOverviewRuler: true,
  scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
  cursorBlinking: "smooth" as const,
  smoothScrolling: true,
};

function parseVCD(vcd: string): {
  variables: VCDVariable[];
  signals: VCDSignal[];
  duration: number;
  timescale: string;
} {
  const variables: VCDVariable[] = [];
  const values = new Map<string, { time: number; value: string }[]>();
  let scope: string[] = [];
  let currentTime = 0;
  let duration = 0;
  let timescale = "unknown";
  let inHeader = true;

  const timescaleMatch = vcd.match(/\$timescale\s+([\s\S]*?)\s+\$end/);
  if (timescaleMatch) {
    timescale = timescaleMatch[1].trim().replace(/\s+/g, " ");
  }

  for (const rawLine of vcd.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    if (line.startsWith("$scope")) {
      const parts = line.split(/\s+/);
      if (parts.length >= 3) scope.push(parts[2]);
      continue;
    }
    if (line.startsWith("$upscope")) {
      scope.pop();
      continue;
    }
    if (line.startsWith("$var")) {
      const parts = line.split(/\s+/);
      if (parts.length >= 6) {
        const width = Number(parts[2]) || 1;
        const id = parts[3];
        const name = parts[4];
        variables.push({
          id,
          name,
          fullName: [...scope, name].join("."),
          width,
        });
        if (!values.has(id)) values.set(id, []);
      }
      continue;
    }
    if (line.startsWith("$enddefinitions")) {
      inHeader = false;
      continue;
    }
    if (inHeader || line.startsWith("$")) continue;

    if (line.startsWith("#")) {
      const parsedTime = Number(line.slice(1));
      if (Number.isFinite(parsedTime)) {
        currentTime = parsedTime;
        duration = Math.max(duration, currentTime);
      }
      continue;
    }

    let value = "";
    let id = "";

    if (/^[01xXzZ]/.test(line)) {
      value = line[0].toLowerCase();
      id = line.slice(1).trim();
    } else if (/^[bB][01xXzZ]+/i.test(line)) {
      const parts = line.split(/\s+/);
      value = parts[0].slice(1).toLowerCase();
      id = parts[1] || "";
    } else if (/^[rR]/.test(line)) {
      continue;
    }

    if (!id || !values.has(id)) continue;
    values.get(id)!.push({ time: currentTime, value });
  }

  const signals = variables.map((variable) => ({
    variable,
    events: values.get(variable.id) || [],
  }));

  return { variables, signals, duration, timescale };
}

function WaveformViewer({ vcd }: { vcd: string }) {
  // zoom 1 = the whole simulation fits the panel width.
  const [zoom, setZoom] = useState(1);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(0);
  const parsed = useMemo(() => parseVCD(vcd), [vcd]);

  // Track the scroll area's width so zoom 1 always fits the panel.
  const observer = useRef<ResizeObserver | null>(null);
  const measureViewport = useCallback((node: HTMLDivElement | null) => {
    observer.current?.disconnect();
    if (!node) return;
    observer.current = new ResizeObserver(([entry]) =>
      setViewportWidth(entry.contentRect.width)
    );
    observer.current.observe(node);
  }, []);

  const signals = parsed.signals.filter(
    (signal, index, all) =>
      all.findIndex(
        (other) => other.variable.fullName === signal.variable.fullName
      ) === index
  );

  const defaultSignals = signals.filter((signal) => {
    const name = signal.variable.name.toLowerCase();
    const isCounter = ["failures", "total_tests"].includes(name);
    const isTopLevel = signal.variable.fullName.split(".").length <= 2;
    return isTopLevel && !isCounter;
  });

  const fallbackSignals = signals.filter((signal) => {
    const name = signal.variable.name.toLowerCase();
    return (
      !["failures", "total_tests"].includes(name) &&
      !signal.variable.fullName.includes(".uut.")
    );
  });

  const visibleSignals =
    defaultSignals.length > 0
      ? defaultSignals
      : fallbackSignals.length > 0
        ? fallbackSignals
        : signals;

  const advancedSignals = signals.filter(
    (signal) =>
      !visibleSignals.some(
        (visible) =>
          visible.variable.fullName === signal.variable.fullName
      )
  );
  const displayedSignals = showAdvanced ? signals : visibleSignals;

  if (!vcd.trim() || signals.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-surface p-6 text-sm text-fg-muted">
        No waveform signals were found in the VCD file.
      </div>
    );
  }

  const duration = Math.max(parsed.duration, 1);
  const rowHeight = 52;
  const leftWidth = viewportWidth > 0 && viewportWidth < 640 ? 112 : 176;
  const fitWidth = Math.max(240, (viewportWidth || 900) - leftWidth - 1);
  const plotWidth = Math.min(60000, fitWidth * zoom);
  const totalHeight = displayedSignals.length * rowHeight + 35;
  const scale = plotWidth / duration;

  // A "nice" tick step (1, 2 or 5 x 10^n) that keeps labels ~100px apart.
  const rawStep = duration / Math.max(1, plotWidth / 100);
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const tickStep =
    [1, 2, 5, 10].map((m) => m * magnitude).find((step) => step >= rawStep) ||
    rawStep;
  const ticks: number[] = [];
  for (let t = 0; t <= duration + 1e-9; t += tickStep) ticks.push(t);

  const getY = (value: string) => {
    const binary = value.toLowerCase();
    if (binary === "1") return 13;
    if (binary === "0") return 39;
    return 26;
  };

  const getPath = (signal: VCDSignal) => {
    if (signal.events.length === 0) return "";
    // Before the first event the value is unknown, so start on the X/Z line.
    let path = `M 0 ${signal.events[0].time > 0 ? 26 : getY(signal.events[0].value)}`;
    for (const event of signal.events) {
      path += ` H ${event.time * scale} V ${getY(event.value)}`;
    }
    return `${path} H ${plotWidth}`;
  };

  // Multi-bit buses are drawn as value segments with a hex label, since a
  // single high/low line cannot show values like 0101.
  const formatBusValue = (value: string) => {
    if (/[xz]/.test(value)) {
      return /^x+$/.test(value) ? "X" : /^z+$/.test(value) ? "Z" : value;
    }
    const parsedValue = parseInt(value, 2);
    return Number.isFinite(parsedValue)
      ? `0x${parsedValue.toString(16).toUpperCase()}`
      : value;
  };

  const getBusSegments = (signal: VCDSignal) =>
    signal.events.map((event, eventIndex) => {
      const start = event.time * scale;
      const next = signal.events[eventIndex + 1];
      const end = next ? next.time * scale : plotWidth;
      return { start, end, value: event.value };
    }).filter((segment) => segment.end > segment.start);

  return (
    <div className="overflow-hidden rounded-lg border border-line bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div>
          <h3 className="text-sm font-medium text-fg">Waveform</h3>
          <p className="mt-0.5 font-mono text-[11px] text-fg-subtle">
            Duration: {parsed.duration} {parsed.timescale}
            {" · "}Showing {displayedSignals.length} of {signals.length} signals
          </p>
        </div>

        <div className="flex items-center gap-1 rounded-md border border-line bg-surface-2 p-0.5">
          <button
            aria-label="Zoom out"
            disabled={zoom <= 1}
            onClick={() => setZoom((value) => Math.max(1, value / 2))}
            className="h-6 min-w-6 rounded px-1.5 text-xs text-fg-muted hover:bg-surface-3 hover:text-fg disabled:opacity-35 disabled:hover:bg-transparent"
          >
            −
          </button>
          <span className="min-w-10 text-center font-mono text-[11px] text-fg-muted">
            {zoom === 1 ? "fit" : `${zoom}×`}
          </span>
          <button
            aria-label="Zoom in"
            disabled={zoom >= 64}
            onClick={() => setZoom((value) => Math.min(64, value * 2))}
            className="h-6 min-w-6 rounded px-1.5 text-xs text-fg-muted hover:bg-surface-3 hover:text-fg disabled:opacity-35 disabled:hover:bg-transparent"
          >
            +
          </button>
          <button
            onClick={() => setZoom(1)}
            disabled={zoom === 1}
            className="h-6 min-w-6 rounded px-1.5 text-xs text-fg-muted hover:bg-surface-3 hover:text-fg disabled:opacity-35 disabled:hover:bg-transparent"
          >
            Fit
          </button>
        </div>
      </div>

      <div ref={measureViewport} className="overflow-auto">
        <div
          style={{
            width: leftWidth + plotWidth,
            minWidth: "100%",
            minHeight: totalHeight,
          }}
        >
          <div className="flex border-b border-line bg-surface-2">
            <div
              className="shrink-0 border-r border-line px-3 py-2.5 font-mono text-[11px] uppercase tracking-wider text-fg-subtle"
              style={{ width: leftWidth }}
            >
              Signal
            </div>
            <div className="relative h-10" style={{ width: plotWidth }}>
              {ticks.map((tick, index) => (
                <div
                  key={index}
                  className="absolute top-0 h-full border-l border-line-strong"
                  style={{ left: tick * scale }}
                >
                  <span className="ml-1 font-mono text-[10px] text-fg-subtle">
                    {Number(tick.toFixed(3))}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {displayedSignals.map((signal, index) => (
            <div
              key={`${signal.variable.fullName}-${index}`}
              className="flex border-b border-line/70 hover:bg-surface-2"
              style={{ height: rowHeight }}
            >
              <div
                className="flex shrink-0 items-center overflow-hidden border-r border-line px-3"
                style={{ width: leftWidth }}
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs text-fg">
                    {signal.variable.name}
                  </div>
                  <div className="truncate font-mono text-[10px] text-fg-subtle">
                    {signal.variable.width}-bit
                    {showAdvanced && signal.variable.fullName.includes(".")
                      ? ` · ${signal.variable.fullName}`
                      : ""}
                  </div>
                </div>
              </div>
              <svg width={plotWidth} height={rowHeight} className="shrink-0">
                {ticks.map((tick, tickIndex) => (
                  <line
                    key={tickIndex}
                    x1={tick * scale}
                    x2={tick * scale}
                    y1="0"
                    y2={rowHeight}
                    stroke="#232328"
                    strokeDasharray="2 4"
                  />
                ))}
                <line
                  x1="0"
                  x2={plotWidth}
                  y1={rowHeight / 2}
                  y2={rowHeight / 2}
                  stroke="#18181B"
                />
                {signal.variable.width > 1 ? (
                  getBusSegments(signal).map((segment, segmentIndex) => {
                    const slant = Math.min(5, (segment.end - segment.start) / 2);
                    const unknown = /[xz]/.test(segment.value);
                    return (
                      <g key={segmentIndex}>
                        <polygon
                          points={`${segment.start},26 ${segment.start + slant},13 ${segment.end - slant},13 ${segment.end},26 ${segment.end - slant},39 ${segment.start + slant},39`}
                          fill={unknown ? "rgba(239,95,95,0.10)" : "rgba(240,164,58,0.10)"}
                          stroke={unknown ? "#EF5F5F" : "#F0A43A"}
                          strokeWidth="1.25"
                        />
                        {segment.end - segment.start > 28 && (
                          <text
                            x={(segment.start + segment.end) / 2}
                            y={30}
                            textAnchor="middle"
                            fontSize="10"
                            fontFamily="ui-monospace, Menlo, monospace"
                            fill="#ECECEE"
                          >
                            {formatBusValue(segment.value)}
                          </text>
                        )}
                      </g>
                    );
                  })
                ) : (
                  <>
                    <path
                      d={getPath(signal)}
                      fill="none"
                      stroke="#F0A43A"
                      strokeWidth="1.5"
                      strokeLinejoin="round"
                      strokeLinecap="round"
                    />
                  </>
                )}
              </svg>
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-2.5">
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-fg-subtle">
          <span>high = 1</span>
          <span>low = 0</span>
          <span><span className="text-fail">red</span> = X/Z</span>
          <span>buses in hex</span>
        </div>
        {advancedSignals.length > 0 && (
          <button
            onClick={() => setShowAdvanced((value) => !value)}
            className="rounded-md px-2 py-1 text-xs text-fg-muted hover:bg-surface-3 hover:text-fg"
          >
            {showAdvanced
              ? "Hide internal signals"
              : `Show internal signals (${advancedSignals.length})`}
          </button>
        )}
      </div>
    </div>
  );
}

// Small line icons (24px grid, 1.75 stroke). Kept inline so the UI has
// no icon-font or emoji dependency.
const ICON_PATHS: Record<string, string> = {
  folder: "M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9A1.5 1.5 0 0 1 21 9.5v8a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  plus: "M12 5v14M5 12h14",
  pencil: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  x: "M6 6l12 12M18 6L6 18",
  up: "M12 19V5M5 12l7-7 7 7",
  play: "M7 5v14l11-7z",
  wrench: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z",
  beaker: "M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7.5 15h9",
  check: "M5 12.5l4.5 4.5L19 7.5",
  arrow: "M5 12h14M13 6l6 6-6 6",
  save: "M5 3h11l3 3v13a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM8 3v5h7M8 21v-7h8v7",
  chip: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
};

function Icon({ name, className = "h-4 w-4" }: { name: string; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
      aria-hidden="true"
    >
      <path d={ICON_PATHS[name]} />
    </svg>
  );
}

// Brand mark: a square-wave step inside a rounded square.
function LogoMark() {
  return (
    <svg viewBox="0 0 24 24" className="h-6 w-6" aria-hidden="true">
      <rect x="1" y="1" width="22" height="22" rx="5" fill="#F0A43A" />
      <path
        d="M5 15h4V9h5v6h5"
        fill="none"
        stroke="#1B1206"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function PanelHeader({
  title,
  meta,
  children,
}: {
  title: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="flex min-h-11 flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2">
      <div className="flex min-w-0 items-baseline gap-2">
        <h2 className="truncate text-[13px] font-medium text-fg">{title}</h2>
        {meta && <span className="truncate font-mono text-[11px] text-fg-subtle">{meta}</span>}
      </div>
      {children && <div className="flex items-center gap-1">{children}</div>}
    </div>
  );
}

const EXAMPLE_PROMPTS = [
  "Full adder",
  "4-to-1 multiplexer with a 2-bit select",
  "4-bit up counter with synchronous reset",
  "8-bit even parity generator",
  "2-to-4 decoder with enable",
];

// Shown over an empty editor; pointer-events-none so typing still works.
function EditorHint({ title, body }: { title: string; body: string }) {
  return (
    <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center p-6">
      <div className="max-w-xs text-center">
        <p className="text-[13px] font-medium text-fg-muted">{title}</p>
        <p className="mt-1 text-xs leading-5 text-fg-subtle">{body}</p>
      </div>
    </div>
  );
}

// Colour simulation output by line: PASS green, FAIL / errors red,
// the summary line bright, everything else muted.
function OutputLine({ line }: { line: string }) {
  let tone = "text-fg-muted";
  if (/^\s*(OVERALL\s+)?PASS\b/.test(line)) tone = "text-pass";
  else if (/^\s*(OVERALL\s+)?FAIL(ED)?\b/.test(line) || /\berror\b/i.test(line)) tone = "text-fail";
  else if (/^\s*Total tests/i.test(line)) tone = "text-fg";
  else if (/^\s*VCD info|\$finish called/.test(line)) tone = "text-fg-subtle";
  return <div className={tone}>{line || " "}</div>;
}

type Schematic = {
  svg: string;
  view: "gates" | "rtl";
  cells?: number;
  gate_cells?: number | null;
  rtl_cells?: number;
  note?: string;
  source: string; // design code the drawing was made from
};

function SchematicPanel({
  schematic,
  loading,
  error,
  stale,
  onDraw,
  onClose,
}: {
  schematic: Schematic | null;
  loading: boolean;
  error: string;
  stale: boolean;
  onDraw: (view: "auto" | "gates" | "rtl") => void;
  onClose: () => void;
}) {
  const [zoom, setZoom] = useState<"fit" | 1 | 2>("fit");
  const size = useMemo(() => {
    const match = schematic?.svg.match(/<svg[^>]*\swidth="(\d+(?:\.\d+)?)"[^>]*\sheight="(\d+(?:\.\d+)?)"/);
    return match ? { w: Number(match[1]), h: Number(match[2]) } : null;
  }, [schematic?.svg]);
  const src = schematic
    ? `data:image/svg+xml;charset=utf-8,${encodeURIComponent(schematic.svg)}`
    : "";
  const segment = (active: boolean) =>
    `h-6 rounded px-2 text-xs ${active ? "bg-surface-3 text-fg" : "text-fg-muted hover:text-fg"}`;

  return (
    <section className="overflow-hidden rounded-lg border border-line bg-surface">
      <PanelHeader
        title="Schematic"
        meta={
          schematic
            ? schematic.view === "gates"
              ? `${schematic.cells} gates & flip-flops`
              : `${schematic.cells} RTL blocks`
            : undefined
        }
      >
        <div className="flex items-center gap-0.5 rounded-md border border-line bg-surface-2 p-0.5">
          <button
            onClick={() => onDraw("gates")}
            disabled={loading}
            className={segment(schematic?.view === "gates")}
            title="Logic gates and flip-flops"
          >
            Gates
          </button>
          <button
            onClick={() => onDraw("rtl")}
            disabled={loading}
            className={segment(schematic?.view === "rtl")}
            title="Word-level blocks: adders, multiplexers, registers"
          >
            RTL
          </button>
        </div>
        {schematic && (
          <div className="ml-1 flex items-center gap-0.5 rounded-md border border-line bg-surface-2 p-0.5">
            {(["fit", 1, 2] as const).map((level) => (
              <button key={String(level)} onClick={() => setZoom(level)} className={segment(zoom === level)}>
                {level === "fit" ? "Fit" : `${level}×`}
              </button>
            ))}
          </div>
        )}
        {schematic && (
          <a
            href={src}
            download={`schematic-${schematic.view}.svg`}
            className="inline-flex h-7 items-center rounded-md px-2 text-fg-muted hover:bg-surface-3 hover:text-fg"
            title="Download SVG"
            aria-label="Download schematic as SVG"
          >
            <Icon name="download" className="h-3.5 w-3.5" />
          </a>
        )}
        <button
          onClick={onClose}
          className="inline-flex h-7 items-center rounded-md px-2 text-fg-muted hover:bg-surface-3 hover:text-fg"
          aria-label="Close schematic"
        >
          <Icon name="x" className="h-3.5 w-3.5" />
        </button>
      </PanelHeader>

      {stale && schematic && !loading && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-accent-soft px-4 py-2 text-xs text-accent">
          The design has changed since this was drawn.
          <button onClick={() => onDraw("auto")} className="font-medium underline-offset-2 hover:underline">
            Redraw
          </button>
        </div>
      )}

      <div className="relative max-h-[680px] min-h-40 overflow-auto bg-code p-4">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center gap-2 bg-code/70 text-xs text-fg-muted">
            <span className="lf-spinner text-accent" /> Synthesizing with Yosys…
          </div>
        )}
        {error && !loading && (
          <p className="text-sm whitespace-pre-wrap text-fail">{error}</p>
        )}
        {schematic && !error && (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={src}
            alt={`${schematic.view === "gates" ? "Gate-level" : "RTL"} schematic of the design`}
            className={zoom === "fit" ? "mx-auto block h-auto" : "block max-w-none"}
            style={
              zoom === "fit"
                ? {
                    // Use the panel width (small circuits scale up to 2.5x);
                    // tall circuits scroll vertically instead of shrinking.
                    width: size ? `min(100%, ${size.w * 2.5}px)` : "100%",
                  }
                : size
                  ? { width: size.w * zoom * 1.6, height: size.h * zoom * 1.6 }
                  : undefined
            }
          />
        )}
      </div>
      {schematic?.note && (
        <p className="border-t border-line px-4 py-2 text-[11px] leading-4 text-fg-subtle">
          {schematic.note}
        </p>
      )}
    </section>
  );
}

// Shown when the deployed backend requires the shared access password.
function PasswordGate({
  error,
  checking,
  onSubmit,
}: {
  error: string;
  checking: boolean;
  onSubmit: (password: string) => void;
}) {
  const [password, setPassword] = useState("");
  return (
    <main className="flex min-h-screen items-center justify-center bg-bg px-4 text-fg">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (password.trim()) onSubmit(password.trim());
        }}
        className="w-full max-w-sm rounded-lg border border-line bg-surface p-6"
      >
        <div className="mb-5 flex items-center gap-2.5">
          <LogoMark />
          <span className="text-[15px] font-semibold tracking-tight">LogicForge</span>
        </div>
        <h1 className="text-sm font-medium">This workspace is private</h1>
        <p className="mt-1 text-xs leading-5 text-fg-muted">
          Enter the access password you were given.
        </p>
        <label htmlFor="access-password" className="sr-only">
          Access password
        </label>
        <input
          id="access-password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="mt-4 block h-9 w-full rounded-md border border-line-strong bg-surface-2 px-3 text-sm text-fg placeholder:text-fg-subtle focus:border-accent/60 focus:outline-none"
          placeholder="Password"
        />
        {error && <p className="mt-2 text-xs text-fail">{error}</p>}
        <button
          type="submit"
          disabled={checking || !password.trim()}
          className="mt-4 inline-flex h-9 w-full items-center justify-center gap-2 rounded-md bg-accent text-[13px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50"
        >
          {checking && <span className="lf-spinner" />}
          {checking ? "Checking" : "Unlock"}
        </button>
      </form>
    </main>
  );
}

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [moduleCode, setModuleCode] = useState("");
  const [testbenchCode, setTestbenchCode] = useState("");
  const [explanation, setExplanation] = useState("");
  const [output, setOutput] = useState("");
  const [vcd, setVcd] = useState("");
  const [loading, setLoading] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [simulationSuccess, setSimulationSuccess] = useState<boolean | null>(null);
  const [simulationVerdict, setSimulationVerdict] = useState<string | null>(null);
  const [modelLabel, setModelLabel] = useState("");
  const [locked, setLocked] = useState(false);
  const [unlocking, setUnlocking] = useState(false);
  const [unlockError, setUnlockError] = useState("");
  const [schematic, setSchematic] = useState<Schematic | null>(null);
  const [schematicOpen, setSchematicOpen] = useState(false);
  const [schematicLoading, setSchematicLoading] = useState(false);
  const [schematicError, setSchematicError] = useState("");
  const [simulationTests, setSimulationTests] = useState<number | null>(null);
  // Raw output and error of the last simulation, sent to the AI debugger.
  const [lastSimulation, setLastSimulation] = useState<{
    output: string;
    error: string;
  } | null>(null);

  const [debugging, setDebugging] = useState(false);
  const [debugResult, setDebugResult] = useState<any>(null);

  const [generatingTestbench, setGeneratingTestbench] = useState(false);
  const [testbenchExplanation, setTestbenchExplanation] = useState("");
  const [testbenchError, setTestbenchError] = useState("");

  const [projectFiles, setProjectFiles] = useState<ProjectItem[]>([]);
  const [currentFolder, setCurrentFolder] = useState("");
  // Each editor is linked to its own workspace file (or none), together with
  // the content last loaded from or saved to that file.
  const [moduleFile, setModuleFile] = useState("");
  const [testbenchFile, setTestbenchFile] = useState("");
  const [moduleSaved, setModuleSaved] = useState("");
  const [testbenchSaved, setTestbenchSaved] = useState("");
  const [projectLoading, setProjectLoading] = useState(false);
  const [projectError, setProjectError] = useState("");
  const [projectMessage, setProjectMessage] = useState("");

  const busy = loading || simulating || debugging || generatingTestbench;
  const moduleDirty = moduleCode !== moduleSaved;
  const testbenchDirty = testbenchCode !== testbenchSaved;

  const showProjectMessage = (message: string) => {
    setProjectMessage(message);
    setProjectError("");
  };

  const showProjectError = (message: string) => {
    setProjectError(message);
    setProjectMessage("");
  };

  const loadProjectFiles = async (path = currentFolder) => {
    setProjectLoading(true);
    setProjectError("");
    try {
      const response = await API.get("/project/list", { params: { path } });
      setProjectFiles(response.data.items || []);
      setCurrentFolder(path);
    } catch (error: any) {
      showProjectError(
        error.response?.data?.detail || "Failed to load project files."
      );
    } finally {
      setProjectLoading(false);
    }
  };

  useEffect(() => {
    const showGate = () => {
      setLocked(true);
    };
    window.addEventListener(UNAUTHORIZED_EVENT, showGate);

    // Ask the backend whether a password is needed before loading anything.
    API.get("/auth/status")
      .then((response) => {
        if (response.data?.password_required && !getAccessPassword()) {
          setLocked(true);
        } else {
          loadWorkspace();
        }
      })
      .catch(() => loadWorkspace());

    return () => window.removeEventListener(UNAUTHORIZED_EVENT, showGate);
    // Run once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadWorkspace = () => {
    void loadProjectFiles("");
    API.get("/model")
      .then((response) => setModelLabel(response.data?.label || ""))
      .catch(() => setModelLabel(""));
  };

  const unlock = async (password: string) => {
    setUnlocking(true);
    setUnlockError("");
    setAccessPassword(password);
    try {
      await API.get("/model");
      setLocked(false);
      loadWorkspace();
    } catch (error: any) {
      clearAccessPassword();
      setUnlockError(
        error.response?.status === 401
          ? "That password is not correct."
          : "The server could not be reached. Try again shortly."
      );
    } finally {
      setUnlocking(false);
    }
  };

  // Judge by the file name only, so a folder called "testbench" does not
  // turn every design file inside it into a testbench.
  const isTestbenchPath = (path: string) => {
    const fileName = (path.split("/").pop() || path).toLowerCase();
    return (
      /(^|[_\-.])tb\.s?v$/.test(fileName) ||
      /^tb[_\-.]/.test(fileName) ||
      fileName.includes("testbench")
    );
  };

  const confirmDiscard = (editorName: string, file: string) =>
    window.confirm(
      `The ${editorName} editor has unsaved changes${
        file ? ` to ${file}` : ""
      }. Discard them?`
    );

  const openProjectFile = async (path: string) => {
    const isTestbench = isTestbenchPath(path);
    const dirty = isTestbench ? testbenchDirty : moduleDirty;
    const linkedFile = isTestbench ? testbenchFile : moduleFile;

    if (
      dirty &&
      linkedFile !== path &&
      !confirmDiscard(isTestbench ? "testbench" : "module", linkedFile)
    ) {
      return;
    }

    try {
      const response = await API.post("/project/read", { path });
      const content = response.data.content || "";
      if (isTestbench) {
        setTestbenchCode(content);
        setTestbenchSaved(content);
        setTestbenchFile(path);
      } else {
        setModuleCode(content);
        setModuleSaved(content);
        setModuleFile(path);
      }
      showProjectMessage(`Opened ${path}`);
    } catch (error: any) {
      showProjectError(error.response?.data?.detail || "Failed to open file.");
    }
  };

  const createProjectItem = async (itemType: "file" | "folder") => {
    const name = window.prompt(
      `Enter ${itemType} name${itemType === "file" ? " (.v, .sv or .txt)" : ""}:`
    );
    if (!name?.trim()) return;

    const path = currentFolder
      ? `${currentFolder}/${name.trim()}`
      : name.trim();

    try {
      await API.post("/project/create", { path, item_type: itemType });
      await loadProjectFiles();
      showProjectMessage(`${itemType} created successfully.`);
    } catch (error: any) {
      showProjectError(
        error.response?.data?.detail || `Failed to create ${itemType}.`
      );
    }
  };

  const saveProjectFile = async () => {
    const targets = [
      moduleFile && moduleDirty
        ? { path: moduleFile, content: moduleCode, markSaved: setModuleSaved }
        : null,
      testbenchFile && testbenchDirty
        ? {
            path: testbenchFile,
            content: testbenchCode,
            markSaved: setTestbenchSaved,
          }
        : null,
    ].filter(Boolean) as {
      path: string;
      content: string;
      markSaved: (value: string) => void;
    }[];

    if (!moduleFile && !testbenchFile) {
      showProjectError("Open a project file before saving.");
      return;
    }
    if (targets.length === 0) {
      showProjectMessage("No unsaved changes.");
      return;
    }

    const saved: string[] = [];
    for (const target of targets) {
      try {
        await API.post("/project/save", {
          path: target.path,
          content: target.content,
        });
        target.markSaved(target.content);
        saved.push(target.path);
      } catch (error: any) {
        showProjectError(
          `${saved.length ? `Saved ${saved.join(", ")}. ` : ""}Failed to save ${
            target.path
          }: ${error.response?.data?.detail || "unknown error"}`
        );
        return;
      }
    }
    showProjectMessage(`Saved ${saved.join(" and ")}.`);
  };

  const renamedPath = (file: string, oldPath: string, newPath: string) => {
    if (file === oldPath) return newPath;
    if (file.startsWith(`${oldPath}/`)) {
      return `${newPath}${file.slice(oldPath.length)}`;
    }
    return file;
  };

  const renameProjectItem = async (path: string) => {
    const oldName = path.split("/").pop() || path;
    const newName = window.prompt("Enter the new name:", oldName);
    if (!newName?.trim() || newName.trim() === oldName) return;

    const parent = path.includes("/")
      ? path.substring(0, path.lastIndexOf("/"))
      : "";
    const newPath = parent ? `${parent}/${newName.trim()}` : newName.trim();

    try {
      await API.post("/project/rename", {
        old_path: path,
        new_path: newPath,
      });
      setModuleFile((file) => renamedPath(file, path, newPath));
      setTestbenchFile((file) => renamedPath(file, path, newPath));
      await loadProjectFiles();
      showProjectMessage("Renamed successfully.");
    } catch (error: any) {
      showProjectError(error.response?.data?.detail || "Failed to rename item.");
    }
  };

  const deleteProjectItem = async (path: string) => {
    if (!window.confirm(`Delete "${path}"? This cannot be undone.`)) return;
    const isAffected = (file: string) =>
      file === path || file.startsWith(`${path}/`);
    try {
      await API.post("/project/delete", { path });
      // The editor keeps its content but is no longer linked to a file.
      if (isAffected(moduleFile)) {
        setModuleFile("");
        setModuleSaved("");
      }
      if (isAffected(testbenchFile)) {
        setTestbenchFile("");
        setTestbenchSaved("");
      }
      await loadProjectFiles();
      showProjectMessage("Deleted successfully.");
    } catch (error: any) {
      showProjectError(error.response?.data?.detail || "Failed to delete item.");
    }
  };

  const resetResults = () => {
    setOutput("");
    setVcd("");
    setSimulationSuccess(null);
    setSimulationVerdict(null);
    setSimulationTests(null);
    setLastSimulation(null);
    setDebugResult(null);
  };

  const drawSchematic = async (
    view: "auto" | "gates" | "rtl" = "auto",
    code: string = moduleCode
  ) => {
    if (!code.trim()) return;
    setSchematicOpen(true);
    setSchematicLoading(true);
    setSchematicError("");
    try {
      const response = await API.post("/schematic", { module_code: code, view });
      const data = response.data;
      if (!data.success) {
        setSchematic(null);
        setSchematicError(data.error || "Could not draw the schematic.");
        return;
      }
      setSchematic({ ...data, source: code });
    } catch (error: any) {
      setSchematic(null);
      setSchematicError(
        error.response?.data?.detail?.toString() || error.message || "Could not draw the schematic."
      );
    } finally {
      setSchematicLoading(false);
    }
  };

  const generateCode = async () => {
    if (!prompt.trim()) {
      setOutput("Please enter a circuit description.");
      return;
    }
    if (
      (moduleDirty || testbenchDirty) &&
      !window.confirm(
        "Generating a new design replaces both editors and you have unsaved changes. Continue?"
      )
    ) {
      return;
    }
    setLoading(true);
    resetResults();
    setTestbenchExplanation("");
    setTestbenchError("");
    try {
      const response = await API.post("/generate", { prompt });
      const data = response.data;
      if (data.verification?.gave_up) {
        // The model could not make anything work: keep the editors as they
        // are and show the attempt only as text, clearly marked.
        setOutput(
          [
            data.verification.message,
            ...(data.verification.log?.length ? ["", "What was tried:", ...data.verification.log] : []),
            "",
            "Last attempt (not loaded into the editors):",
            "",
            data.module_code || "",
          ].join("\n")
        );
        return;
      }
      setModuleCode(data.module_code || data.code || "");
      setTestbenchCode(data.testbench_code || "");
      setExplanation(data.explanation || "");
      // A newly generated design does not belong to any opened file, so
      // unlink both editors to stop Save from overwriting them.
      setModuleFile("");
      setTestbenchFile("");
      setModuleSaved("");
      setTestbenchSaved("");
      setOutput(
        ["Verilog module and testbench generated.", describeVerification(data.verification)]
          .filter(Boolean)
          .join("\n")
      );
      if (data.verification?.compiled !== false && (data.module_code || data.code)) {
        void drawSchematic("auto", data.module_code || data.code);
      }
    } catch (error: any) {
      setOutput(
        error.response?.data?.detail ||
          error.message ||
          "Failed to generate Verilog."
      );
    } finally {
      setLoading(false);
    }
  };

  const runSimulation = async () => {
    if (!moduleCode.trim() || !testbenchCode.trim()) {
      setOutput("Please generate or enter both the Verilog module and testbench.");
      return;
    }
    setSimulating(true);
    resetResults();
    try {
      const response = await API.post("/simulate", {
        module_code: moduleCode,
        testbench_code: testbenchCode,
      });
      const data: SimulationResult = response.data;
      setOutput(
        [data.output, data.error, data.warnings, data.waveform_error]
          .filter(Boolean)
          .join("\n") || "Simulation completed."
      );
      setVcd(data.vcd || data.waveform || data.vcd_content || "");
      setSimulationSuccess(Boolean(data.success));
      setSimulationVerdict(data.verdict ?? null);
      setSimulationTests(data.total_tests ?? null);
      setLastSimulation({
        output: data.output || "",
        error: [data.error, data.warnings].filter(Boolean).join("\n"),
      });
    } catch (error: any) {
      const message =
        error.response?.data?.detail || error.message || "Simulation failed.";
      setOutput(message);
      setSimulationSuccess(false);
      setLastSimulation({ output: "", error: message });
    } finally {
      setSimulating(false);
    }
  };

  const debugVerilog = async () => {
    if (!moduleCode.trim() || !testbenchCode.trim()) {
      setOutput("Please provide both module code and testbench code.");
      return;
    }
    setDebugging(true);
    setDebugResult(null);
    try {
      const response = await API.post("/debug", {
        module_code: moduleCode,
        testbench_code: testbenchCode,
        error: lastSimulation?.error || "",
        output: lastSimulation?.output || "",
      });
      setDebugResult(response.data);
    } catch (error: any) {
      setDebugResult({
        success: false,
        error:
          error.response?.data?.detail ||
          error.message ||
          "AI debugging failed. Please try again.",
      });
    } finally {
      setDebugging(false);
    }
  };

  const generateTestbench = async () => {
    if (!moduleCode.trim()) {
      setTestbenchError("Please provide a Verilog module first.");
      return;
    }
    if (testbenchDirty && !confirmDiscard("testbench", testbenchFile)) {
      return;
    }
    setGeneratingTestbench(true);
    setTestbenchError("");
    setTestbenchExplanation("");
    try {
      const response = await API.post("/generate-testbench", {
        module_code: moduleCode,
      });
      if (!response.data.success || !response.data.testbench_code) {
        throw new Error("AI did not return valid testbench code.");
      }
      setTestbenchCode(response.data.testbench_code);
      setTestbenchExplanation(response.data.explanation || "");
      resetResults();
      setOutput(
        describeVerification(response.data.verification) ||
          "AI testbench generated. Run simulation to verify it."
      );
    } catch (error: any) {
      setTestbenchError(
        error.response?.data?.detail ||
          error.message ||
          "Testbench generation failed. Please try again."
      );
    } finally {
      setGeneratingTestbench(false);
    }
  };

  const applyDebugFix = () => {
    // Only real Verilog may replace editor content. Anything else (e.g. an
    // AI explanation sentence) leaves that editor untouched.
    const newModule = isVerilogSource(debugResult?.fixed_module_code)
      ? debugResult.fixed_module_code
      : null;
    const newTestbench = isVerilogSource(debugResult?.fixed_testbench_code)
      ? debugResult.fixed_testbench_code
      : null;
    if (!newModule && !newTestbench) {
      setOutput("The AI did not return valid Verilog. Your code was left unchanged.");
      return;
    }
    // Files stay linked; the editors become unsaved until the user saves.
    if (newModule) setModuleCode(newModule);
    if (newTestbench) setTestbenchCode(newTestbench);
    resetResults();
    setOutput(
      [
        "AI fix applied. Run simulation again to verify the changes.",
        describeVerification(debugResult?.verification),
      ]
        .filter(Boolean)
        .join("\n")
    );
  };

  const btnPrimary =
    "inline-flex h-9 items-center justify-center gap-2 rounded-md bg-accent px-3.5 text-[13px] font-medium text-accent-fg hover:bg-accent-hover disabled:opacity-50 disabled:hover:bg-accent";
  const btnSecondary =
    "inline-flex h-9 items-center justify-center gap-2 rounded-md border border-line-strong bg-surface-2 px-3.5 text-[13px] font-medium text-fg hover:border-fg-subtle/60 hover:bg-surface-3 disabled:opacity-50";
  const btnGhost =
    "inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs text-fg-muted hover:bg-surface-3 hover:text-fg disabled:opacity-40 disabled:hover:bg-transparent";
  const panel = "overflow-hidden rounded-lg border border-line bg-surface";

  const activity = loading
    ? "Generating design"
    : generatingTestbench
      ? "Writing testbench"
      : simulating
        ? "Simulating"
        : debugging
          ? "Debugging"
          : "";

  const fileLabel = (file: string, fallback: string) =>
    file ? file.split("/").pop() || file : fallback;

  const verdictBadge =
    simulationSuccess === null
      ? null
      : simulationSuccess
        ? simulationVerdict === "UNVERIFIED"
          ? { tone: "border-line-strong text-fg-muted", text: "Ran · no summary" }
          : {
              tone: "border-pass/30 bg-pass-soft text-pass",
              text: `Pass${simulationTests != null ? ` · ${simulationTests} tests` : ""}`,
            }
        : { tone: "border-fail/30 bg-fail-soft text-fail", text: "Fail" };

  if (locked) {
    return <PasswordGate error={unlockError} checking={unlocking} onSubmit={unlock} />;
  }

  return (
    <main className="min-h-screen bg-bg text-fg">
      <header className="sticky top-0 z-20 border-b border-line bg-bg/85 backdrop-blur-md">
        <div className="mx-auto flex h-12 max-w-[1600px] items-center justify-between gap-4 px-4 md:px-6">
          <div className="flex min-w-0 items-center gap-2.5">
            <LogoMark />
            <span className="text-[15px] font-semibold tracking-tight">LogicForge</span>
            <span className="hidden h-4 w-px bg-line-strong sm:block" />
            <span className="hidden truncate text-[13px] text-fg-subtle sm:block">
              Verilog workbench
            </span>
          </div>
          <div className="flex items-center gap-2 font-mono text-[11px] text-fg-subtle">
            {activity ? (
              <>
                <span className="lf-spinner text-accent" />
                <span className="text-fg-muted">{activity}…</span>
              </>
            ) : (
              <>
                <span className="h-1.5 w-1.5 rounded-full bg-fg-subtle" />
                <span>Ready{modelLabel ? ` · ${modelLabel}` : ""}</span>
              </>
            )}
          </div>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1600px] grid-cols-1 items-start gap-5 px-4 py-5 md:px-6 lg:grid-cols-[248px_minmax(0,1fr)]">
        {/* ---------------- Explorer ---------------- */}
        <aside className="min-w-0 lg:sticky lg:top-[68px]">
          <section className={panel}>
            <PanelHeader title="Files">
              <button
                onClick={() => void createProjectItem("file")}
                className={btnGhost}
                title="New file"
                aria-label="New file"
              >
                <Icon name="plus" className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => void createProjectItem("folder")}
                className={btnGhost}
                title="New folder"
                aria-label="New folder"
              >
                <Icon name="folder" className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => void loadProjectFiles()}
                disabled={projectLoading}
                className={btnGhost}
                title="Refresh"
                aria-label="Refresh"
              >
                <Icon
                  name="refresh"
                  className={`h-3.5 w-3.5 ${projectLoading ? "animate-spin" : ""}`}
                />
              </button>
            </PanelHeader>

            <div className="flex h-8 items-center justify-between gap-2 border-b border-line px-3">
              <span className="min-w-0 truncate font-mono text-[11px] text-fg-subtle">
                ~/workspace{currentFolder ? `/${currentFolder}` : ""}
              </span>
              {currentFolder && (
                <button
                  onClick={() => {
                    const parent = currentFolder.includes("/")
                      ? currentFolder.slice(0, currentFolder.lastIndexOf("/"))
                      : "";
                    void loadProjectFiles(parent);
                  }}
                  className="inline-flex items-center gap-1 text-[11px] text-fg-muted hover:text-fg"
                >
                  <Icon name="up" className="h-3 w-3" />
                  Up
                </button>
              )}
            </div>

            <ul className="max-h-[50vh] overflow-y-auto p-1.5">
              {projectFiles.length === 0 && !projectLoading && (
                <li className="px-2 py-6 text-center text-xs text-fg-subtle">
                  Empty folder
                </li>
              )}
              {projectFiles.map((item) => {
                const active =
                  moduleFile === item.path || testbenchFile === item.path;
                return (
                  <li
                    key={item.path}
                    className={`group flex items-center gap-1 rounded-md pr-1 ${
                      active ? "bg-surface-3 text-fg" : "text-fg-muted hover:bg-surface-2 hover:text-fg"
                    }`}
                  >
                    <button
                      onClick={() =>
                        item.type === "folder"
                          ? void loadProjectFiles(item.path)
                          : void openProjectFile(item.path)
                      }
                      className="flex h-8 min-w-0 flex-1 items-center gap-2 px-2 text-left text-[13px]"
                      title={item.path}
                    >
                      <Icon
                        name={item.type === "folder" ? "folder" : "file"}
                        className={`h-3.5 w-3.5 ${active ? "text-accent" : "text-fg-subtle"}`}
                      />
                      <span className="truncate">{item.name}</span>
                    </button>
                    <span className="flex items-center opacity-100 group-focus-within:opacity-100 md:opacity-0 md:group-hover:opacity-100">
                      <button
                        onClick={() => void renameProjectItem(item.path)}
                        title="Rename"
                        aria-label={`Rename ${item.name}`}
                        className="rounded p-1 text-fg-subtle hover:text-fg"
                      >
                        <Icon name="pencil" className="h-3 w-3" />
                      </button>
                      <button
                        onClick={() => void deleteProjectItem(item.path)}
                        title="Delete"
                        aria-label={`Delete ${item.name}`}
                        className="rounded p-1 text-fg-subtle hover:text-fail"
                      >
                        <Icon name="x" className="h-3 w-3" />
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>

            <div className="border-t border-line p-3">
              <dl className="space-y-1.5 text-xs">
                {[
                  { label: "Design", file: moduleFile, dirty: moduleDirty },
                  { label: "Testbench", file: testbenchFile, dirty: testbenchDirty },
                ].map(({ label, file, dirty }) => (
                  <div key={label} className="flex items-center justify-between gap-2">
                    <dt className="text-fg-subtle">{label}</dt>
                    <dd className="flex min-w-0 items-center gap-1.5 font-mono text-[11px] text-fg-muted">
                      <span className="truncate" title={file || undefined}>
                        {file ? fileLabel(file, "") : "unlinked"}
                      </span>
                      {file && dirty && (
                        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent" title="Unsaved changes" />
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
              <button
                onClick={() => void saveProjectFile()}
                disabled={
                  (!moduleFile || !moduleDirty) &&
                  (!testbenchFile || !testbenchDirty)
                }
                className={`${btnSecondary} mt-3 h-8 w-full text-xs`}
              >
                <Icon name="save" className="h-3.5 w-3.5" />
                Save changes
              </button>
              {projectMessage && (
                <p className="mt-2 break-words text-[11px] text-fg-muted">{projectMessage}</p>
              )}
              {projectError && (
                <p className="mt-2 break-words text-[11px] text-fail">{projectError}</p>
              )}
              <p className="mt-3 text-[11px] leading-4 text-fg-subtle">
                .v, .sv and .txt files, stored in the backend workspace.
              </p>
            </div>
          </section>
        </aside>

        <div className="min-w-0 space-y-5">
          {/* ---------------- Prompt composer ---------------- */}
          <section className={`${panel} focus-within:border-accent/40`}>
            <label htmlFor="circuit-prompt" className="sr-only">
              Circuit description
            </label>
            <textarea
              id="circuit-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !busy) {
                  event.preventDefault();
                  void generateCode();
                }
              }}
              placeholder="Describe a circuit — e.g. 4-bit binary adder with carry in and carry out"
              rows={3}
              className="block w-full resize-y border-0 bg-transparent px-4 pt-3.5 pb-2 text-[14px] leading-6 text-fg placeholder:text-fg-subtle focus:shadow-none focus:outline-none focus-visible:outline-none"
            />
            <div className="flex flex-wrap items-center justify-between gap-3 px-4 pb-3">
              <p className="text-xs text-fg-subtle">
                Name the inputs, outputs and behaviour.{" "}
                <span className="hidden font-mono text-[11px] sm:inline">
                  <kbd className="rounded border border-line-strong px-1 py-px">⌘</kbd>{" "}
                  <kbd className="rounded border border-line-strong px-1 py-px">↵</kbd> to generate
                </span>
              </p>
              <button onClick={generateCode} disabled={busy} className={btnPrimary}>
                {loading ? <span className="lf-spinner" /> : null}
                {loading ? "Generating" : "Generate"}
                {!loading && <Icon name="arrow" className="h-3.5 w-3.5" />}
              </button>
            </div>
            {!moduleCode.trim() && (
              <div className="flex flex-wrap items-center gap-1.5 border-t border-line px-4 py-2.5">
                <span className="mr-1 text-xs text-fg-subtle">Try</span>
                {EXAMPLE_PROMPTS.map((example) => (
                  <button
                    key={example}
                    onClick={() => {
                      setPrompt(example);
                      document.getElementById("circuit-prompt")?.focus();
                    }}
                    className={`rounded-full border px-2.5 py-1 text-xs ${
                      prompt === example
                        ? "border-accent/50 bg-accent-soft text-accent"
                        : "border-line-strong text-fg-muted hover:border-fg-subtle/60 hover:text-fg"
                    }`}
                  >
                    {example}
                  </button>
                ))}
              </div>
            )}
          </section>

          {/* ---------------- Editors ---------------- */}
          <div className="grid grid-cols-1 gap-5 xl:grid-cols-2">
            <section className={panel}>
              <PanelHeader
                title={fileLabel(moduleFile, "design.v")}
                meta={moduleFile && moduleDirty ? "modified" : "design"}
              >
                <button
                  onClick={() => void drawSchematic("auto")}
                  disabled={!moduleCode.trim() || schematicLoading}
                  className={btnGhost}
                  title="Draw the design as a circuit schematic"
                >
                  {schematicLoading ? (
                    <span className="lf-spinner" />
                  ) : (
                    <Icon name="chip" className="h-3.5 w-3.5" />
                  )}
                  Schematic
                </button>
              </PanelHeader>
              <div className="relative">
                {!moduleCode && (
                  <EditorHint
                    title="No design yet"
                    body="Describe a circuit above and press Generate, open a .v file from Files, or start typing here."
                  />
                )}
                <Editor
                  height="380px"
                  language="verilog"
                  theme="logicforge-dark"
                  beforeMount={configureMonaco}
                  loading={<div className="h-full w-full bg-code" />}
                  value={moduleCode}
                  onChange={(value) => setModuleCode(value || "")}
                  options={EDITOR_OPTIONS}
                />
              </div>
            </section>

            <section className={panel}>
              <PanelHeader
                title={fileLabel(testbenchFile, "testbench.v")}
                meta={testbenchFile && testbenchDirty ? "modified" : "testbench"}
              >
                <button
                  onClick={generateTestbench}
                  disabled={busy || !moduleCode.trim()}
                  className={btnGhost}
                  title="Write a self-checking testbench for the design"
                >
                  {generatingTestbench ? (
                    <span className="lf-spinner" />
                  ) : (
                    <Icon name="beaker" className="h-3.5 w-3.5" />
                  )}
                  {generatingTestbench ? "Writing…" : "Generate testbench"}
                </button>
              </PanelHeader>
              {testbenchError && (
                <div className="border-b border-line bg-fail-soft px-4 py-2.5 text-xs leading-5 whitespace-pre-wrap text-fail">
                  {testbenchError}
                </div>
              )}
              {testbenchExplanation && (
                <div className="border-b border-line px-4 py-2.5 text-xs leading-5 whitespace-pre-wrap text-fg-muted">
                  {testbenchExplanation}
                </div>
              )}
              <div className="relative">
                {!testbenchCode && (
                  <EditorHint
                    title="No testbench yet"
                    body={
                      moduleCode.trim()
                        ? "Press Generate testbench for a self-checking test of your design, or write your own."
                        : "It is created together with the design, or from any design with Generate testbench."
                    }
                  />
                )}
                <Editor
                  height="380px"
                  language="verilog"
                  theme="logicforge-dark"
                  beforeMount={configureMonaco}
                  loading={<div className="h-full w-full bg-code" />}
                  value={testbenchCode}
                  onChange={(value) => setTestbenchCode(value || "")}
                  options={EDITOR_OPTIONS}
                />
              </div>
            </section>
          </div>

          {/* ---------------- Schematic ---------------- */}
          {schematicOpen && (
            <SchematicPanel
              schematic={schematic}
              loading={schematicLoading}
              error={schematicError}
              stale={Boolean(schematic && schematic.source !== moduleCode)}
              onDraw={(view) => void drawSchematic(view)}
              onClose={() => setSchematicOpen(false)}
            />
          )}

          {/* ---------------- Action bar ---------------- */}
          <section className={`${panel} flex flex-wrap items-center justify-between gap-3 px-3 py-2.5`}>
            <div className="flex flex-wrap items-center gap-2">
              <button onClick={runSimulation} disabled={busy} className={btnPrimary}>
                {simulating ? <span className="lf-spinner" /> : <Icon name="play" className="h-3.5 w-3.5" />}
                {simulating ? "Running" : "Run simulation"}
              </button>
              <button onClick={debugVerilog} disabled={busy} className={btnSecondary}>
                {debugging ? <span className="lf-spinner" /> : <Icon name="wrench" className="h-3.5 w-3.5" />}
                {debugging ? "Analysing" : "Debug & fix"}
              </button>
            </div>
            <p className="font-mono text-[11px] text-fg-subtle">
              iverilog → vvp · results below
            </p>
          </section>

          {/* ---------------- Debug report ---------------- */}
          {debugResult && (
            <section className={panel}>
              <PanelHeader title="Debug report" meta={debugResult.success ? undefined : "failed"}>
                <button
                  onClick={() => setDebugResult(null)}
                  className={btnGhost}
                  aria-label="Close debug report"
                >
                  <Icon name="x" className="h-3.5 w-3.5" />
                </button>
              </PanelHeader>
              {debugResult.success ? (
                <div className="space-y-4 p-4">
                  <div className="border-l-2 border-fail pl-3">
                    <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-fg-subtle">
                      Root cause
                    </h3>
                    <p className="text-sm leading-6 whitespace-pre-wrap text-fg">
                      {debugResult.root_cause}
                    </p>
                  </div>
                  <div className="pl-3.5">
                    <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-fg-subtle">
                      Explanation
                    </h3>
                    <p className="text-sm leading-6 whitespace-pre-wrap text-fg-muted">
                      {debugResult.explanation}
                    </p>
                  </div>
                  <div className="pl-3.5">
                    <h3 className="mb-1 text-[11px] font-medium uppercase tracking-wider text-fg-subtle">
                      Changes
                    </h3>
                    <p className="font-mono text-xs leading-5 whitespace-pre-wrap text-fg-muted">
                      {debugResult.changes_made}
                    </p>
                  </div>
                  {Array.isArray(debugResult.notes) && debugResult.notes.length > 0 && (
                    <p className="rounded-md border border-accent/25 bg-accent-soft px-3 py-2 text-xs leading-5 whitespace-pre-wrap text-accent">
                      {debugResult.notes.join("\n")}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
                    {debugResult.module_changed === false &&
                    debugResult.testbench_changed === false ? (
                      <span className="text-xs text-fg-muted">No code changes suggested.</span>
                    ) : (
                      <button onClick={applyDebugFix} disabled={busy} className={btnPrimary}>
                        <Icon name="check" className="h-3.5 w-3.5" />
                        Apply fix
                        {debugResult.module_changed === false
                          ? " to testbench"
                          : debugResult.testbench_changed === false
                            ? " to design"
                            : ""}
                      </button>
                    )}
                    <button onClick={() => setDebugResult(null)} className={btnSecondary}>
                      Dismiss
                    </button>
                    <span className="text-xs text-fg-subtle">
                      Review before applying, then run the simulation again.
                    </span>
                  </div>
                </div>
              ) : (
                <p className="p-4 text-sm whitespace-pre-wrap text-fail">
                  {debugResult.error || "Unable to analyse the code."}
                </p>
              )}
            </section>
          )}

          {/* ---------------- Design notes ---------------- */}
          {explanation && (
            <section className={panel}>
              <PanelHeader title="Design notes" />
              <p className="px-4 py-3 text-sm leading-6 whitespace-pre-wrap text-fg-muted">
                {explanation}
              </p>
            </section>
          )}

          {/* ---------------- Output ---------------- */}
          {output && (
            <section className={panel}>
              <PanelHeader title="Output">
                {verdictBadge && (
                  <span
                    className={`rounded-full border px-2 py-0.5 font-mono text-[11px] ${verdictBadge.tone}`}
                  >
                    {verdictBadge.text}
                  </span>
                )}
              </PanelHeader>
              <pre className="max-h-[420px] overflow-auto bg-code px-4 py-3 font-mono text-xs leading-5 whitespace-pre-wrap">
                {output.split("\n").map((line, index) => (
                  <OutputLine key={index} line={line} />
                ))}
              </pre>
            </section>
          )}

          {/* ---------------- Waveform ---------------- */}
          {vcd && (
            <section className="space-y-3">
              <WaveformViewer vcd={vcd} />
              <details className="group overflow-hidden rounded-lg border border-line bg-surface">
                <summary className="flex h-10 cursor-pointer list-none items-center gap-2 px-4 text-xs text-fg-muted hover:text-fg">
                  <span className="transition-transform group-open:rotate-90">›</span>
                  Raw VCD data
                </summary>
                <pre className="max-h-96 overflow-auto border-t border-line bg-code p-4 font-mono text-[11px] leading-5 whitespace-pre-wrap text-fg-subtle">
                  {vcd}
                </pre>
              </details>
            </section>
          )}
        </div>
      </div>

      <footer className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-5 font-mono text-[11px] text-fg-subtle md:px-6">
        <span>LogicForge</span>
        <span>Icarus Verilog · Ollama</span>
      </footer>
    </main>
  );
}
