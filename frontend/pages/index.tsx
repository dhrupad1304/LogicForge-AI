
import { useMemo, useState } from "react";
import Editor from "@monaco-editor/react";
import { API } from "../utils/api";

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
};

function configureMonaco(monaco: any) {
  monaco.editor.defineTheme("logicforge-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "71877A", fontStyle: "italic" },
      { token: "keyword", foreground: "55E69A" },
      { token: "number", foreground: "B7E6A5" },
      { token: "string", foreground: "A5D6A7" },
      { token: "identifier", foreground: "E0EAE3" },
    ],
    colors: {
      "editor.background": "#0B100E",
      "editor.foreground": "#E7EEE9",
      "editorLineNumber.foreground": "#53645A",
      "editorLineNumber.activeForeground": "#55E69A",
      "editorCursor.foreground": "#55E69A",
      "editor.selectionBackground": "#24563B",
      "editor.lineHighlightBackground": "#121B16",
      "editorIndentGuide.background": "#26352D",
      "editorIndentGuide.activeBackground": "#477A59",
      "editorWidget.background": "#101713",
      "editorWidget.border": "#365B43",
    },
  });
}

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

  const lines = vcd.split(/\r?\n/);

  const timescaleMatch = vcd.match(
    /\$timescale\s+([\s\S]*?)\s+\$end/
  );

  if (timescaleMatch) {
    timescale = timescaleMatch[1].trim().replace(/\s+/g, " ");
  }

  for (const rawLine of lines) {
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
        const fullName = [...scope, name].join(".");

        variables.push({ id, name, fullName, width });

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

    values.get(id)!.push({
      time: currentTime,
      value,
    });
  }

  const signals: VCDSignal[] = variables.map((variable) => ({
    variable,
    events: values.get(variable.id) || [],
  }));

  return { variables, signals, duration, timescale };
}

function WaveformViewer({ vcd }: { vcd: string }) {
  const [zoom, setZoom] = useState(1);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const parsed = useMemo(() => parseVCD(vcd), [vcd]);

  const signals = parsed.signals.filter(
    (signal, index, all) =>
      all.findIndex(
        (other) => other.variable.fullName === signal.variable.fullName
      ) === index
  );

  // Hide internal counters and nested design-instance signals by default.
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

  const displayedSignals = showAdvanced
    ? signals
    : visibleSignals;

  if (!vcd.trim() || signals.length === 0) {
    return (
      <div className="rounded-xl border border-[#26352D] bg-[#101713] p-6 text-sm text-[#91A198]">
        No waveform signals were found in the VCD file.
      </div>
    );
  }

  const duration = Math.max(parsed.duration, 1);
  const rowHeight = 58;
  const leftWidth = 190;

  const plotWidth = Math.min(
    12000,
    Math.max(650, duration * 28 * zoom)
  );

  const totalHeight = displayedSignals.length * rowHeight + 35;
  const scale = plotWidth / duration;

  const ticks = Array.from(
    { length: 6 },
    (_, index) => (duration * index) / 5
  );

  const getY = (value: string) => {
    const binary = value.toLowerCase();

    if (binary === "1") return 13;
    if (binary === "0") return 39;

    return 26;
  };

  const getPath = (signal: VCDSignal) => {
    const events = signal.events;
    if (events.length === 0) return "";

    let path = `M 0 ${getY(events[0].value)}`;

    for (const event of events) {
      const x = event.time * scale;
      const y = getY(event.value);
      path += ` H ${x} V ${y}`;
    }

    path += ` H ${plotWidth}`;
    return path;
  };

  return (
    <div className="overflow-hidden rounded-2xl border border-[#263D30] bg-[#0D130F] shadow-[0_12px_50px_rgba(0,0,0,0.25)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#26352D] bg-[#111A14] px-5 py-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-[#39E58C] shadow-[0_0_10px_#39E58C]" />
            <h3 className="font-semibold text-[#E7EEE9]">
              Digital Waveform
            </h3>
          </div>

          <p className="mt-1 text-xs text-[#91A198]">
            Duration: {parsed.duration} {parsed.timescale}
            {" · "}
            Showing {displayedSignals.length} of {signals.length} signals
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setZoom((value) => Math.max(0.25, value / 1.5))}
            className="rounded-lg border border-[#365B43] bg-[#17231B] px-3 py-1.5 text-sm text-[#B7C9BD] hover:border-[#39E58C] hover:text-[#39E58C]"
          >
            −
          </button>

          <span className="min-w-14 text-center text-xs text-[#91A198]">
            {zoom.toFixed(2)}x
          </span>

          <button
            onClick={() => setZoom((value) => Math.min(8, value * 1.5))}
            className="rounded-lg border border-[#365B43] bg-[#17231B] px-3 py-1.5 text-sm text-[#B7C9BD] hover:border-[#39E58C] hover:text-[#39E58C]"
          >
            +
          </button>

          <button
            onClick={() => setZoom(1)}
            className="rounded-lg border border-[#365B43] bg-[#17231B] px-3 py-1.5 text-sm text-[#B7C9BD] hover:border-[#39E58C] hover:text-[#39E58C]"
          >
            Reset
          </button>
        </div>
      </div>

      <div className="overflow-auto">
        <div
          style={{
            width: leftWidth + plotWidth,
            minWidth: "100%",
            minHeight: totalHeight,
          }}
        >
          <div className="flex border-b border-[#26352D] bg-[#141D17]">
            <div
              className="shrink-0 border-r border-[#26352D] px-3 py-3 text-xs font-bold tracking-wider text-[#91A198]"
              style={{ width: leftWidth }}
            >
              SIGNAL
            </div>

            <div className="relative h-10" style={{ width: plotWidth }}>
              {ticks.map((tick, index) => (
                <div
                  key={index}
                  className="absolute top-0 h-full border-l border-[#314437]"
                  style={{ left: tick * scale }}
                >
                  <span className="ml-1 text-[10px] text-[#91A198]">
                    {Math.round(tick)}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {displayedSignals.map((signal, index) => (
            <div
              key={`${signal.variable.fullName}-${index}`}
              className="flex border-b border-[#202D25] hover:bg-[#111B14]"
              style={{ height: rowHeight }}
            >
              <div
                className="flex shrink-0 items-center overflow-hidden border-r border-[#26352D] bg-[#111813] px-3"
                style={{ width: leftWidth }}
              >
                <div className="min-w-0">
                  <div className="truncate text-xs font-semibold text-[#62ECA0]">
                    {signal.variable.name}
                  </div>
                  <div className="truncate text-[10px] text-[#71877A]">
                    {signal.variable.width}-bit
                    {showAdvanced && signal.variable.fullName.includes(".")
                      ? ` · ${signal.variable.fullName}`
                      : ""}
                  </div>
                </div>
              </div>

              <svg
                width={plotWidth}
                height={rowHeight}
                className="shrink-0"
              >
                {ticks.map((tick, tickIndex) => (
                  <line
                    key={tickIndex}
                    x1={tick * scale}
                    x2={tick * scale}
                    y1="0"
                    y2={rowHeight}
                    stroke="#26352D"
                    strokeDasharray="3 4"
                  />
                ))}

                <line
                  x1="0"
                  x2={plotWidth}
                  y1={rowHeight / 2}
                  y2={rowHeight / 2}
                  stroke="#1C2920"
                />

                <path
                  d={getPath(signal)}
                  fill="none"
                  stroke="#39E58C"
                  strokeWidth="2"
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />

                {signal.events.map((event, eventIndex) => (
                  <circle
                    key={eventIndex}
                    cx={event.time * scale}
                    cy={getY(event.value)}
                    r="2.5"
                    fill="#A1FFC9"
                  />
                ))}
              </svg>
            </div>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[#26352D] bg-[#111813] px-5 py-3">
        <div className="flex flex-wrap gap-4 text-xs text-[#91A198]">
          <span>
            <span className="mr-1 inline-block h-2 w-2 rounded-full bg-[#39E58C]" />
            Signal transition
          </span>
          <span>High = 1</span>
          <span>Low = 0</span>
          <span>X/Z = Unknown</span>
        </div>

        {advancedSignals.length > 0 && (
          <button
            onClick={() => setShowAdvanced((value) => !value)}
            className="rounded-lg border border-[#365B43] bg-[#17231B] px-3 py-2 text-xs font-semibold text-[#62ECA0] hover:border-[#39E58C] hover:bg-[#1D3023]"
          >
            {showAdvanced
              ? "Hide Advanced Signals"
              : `Show Advanced Signals (${advancedSignals.length})`}
          </button>
        )}
      </div>
    </div>
  );
}

export default function Home() {
  const [prompt, setPrompt] = useState(
    ""
  );

  const [moduleCode, setModuleCode] = useState("");
  const [testbenchCode, setTestbenchCode] = useState("");
  const [explanation, setExplanation] = useState("");
  const [output, setOutput] = useState("");
  const [vcd, setVcd] = useState("");
  const [loading, setLoading] = useState(false);
  const [simulating, setSimulating] = useState(false);
  const [simulationSuccess, setSimulationSuccess] = useState<boolean | null>(
    null
  );
   // AI Debugger state
const [debugging, setDebugging] = useState(false);
const [debugResult, setDebugResult] = useState<any>(null);
const [applyingFix, setApplyingFix] = useState(false);
// AI Testbench Generator state
const [generatingTestbench, setGeneratingTestbench] = useState(false);
const [testbenchExplanation, setTestbenchExplanation] = useState("");
const [testbenchError, setTestbenchError] = useState("");

  const generateCode = async () => {
    if (!prompt.trim()) {
      setOutput("Please enter a circuit description.");
      return;
    }

    setLoading(true);
    setOutput("");
    setVcd("");
    setSimulationSuccess(null);

    try {
      const response = await API.post("/generate", { prompt });
      const data = response.data;

      setModuleCode(data.module_code || data.code || "");
      setTestbenchCode(data.testbench_code || "");
      setExplanation(data.explanation || "");

      setOutput("Verilog module and testbench generated successfully.");
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
      setOutput(
        "Please generate or enter both the Verilog module and testbench."
      );
      return;
    }

    setSimulating(true);
    setOutput("");
    setVcd("");
    setSimulationSuccess(null);

    try {
      const response = await API.post("/simulate", {
        module_code: moduleCode,
        testbench_code: testbenchCode,
      });

      const data: SimulationResult = response.data;

      setOutput(
        [data.output, data.error].filter(Boolean).join("\n") ||
          "Simulation completed."
      );

      setVcd(data.vcd || data.waveform || data.vcd_content || "");
      setSimulationSuccess(Boolean(data.success));
    } catch (error: any) {
      setOutput(
        error.response?.data?.detail ||
          error.message ||
          "Simulation failed."
      );
      setSimulationSuccess(false);
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
        error: simulationSuccess === false ? output : "",
        output: output,
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

    setVcd("");
    setSimulationSuccess(null);
    setOutput("AI testbench generated. Run simulation to verify it.");
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
    if (
      !debugResult?.fixed_module_code ||
      !debugResult?.fixed_testbench_code
    ) {
      return;
    }

    setApplyingFix(true);

    setModuleCode(debugResult.fixed_module_code);
    setTestbenchCode(debugResult.fixed_testbench_code);

    setVcd("");
    setSimulationSuccess(null);
    setOutput("AI fix applied. Run simulation again to verify the changes.");
    setDebugResult(null);

    setApplyingFix(false);
  };


  const panelClass =
    "overflow-hidden rounded-2xl border border-[#263D30] bg-[#101713] shadow-[0_8px_30px_rgba(0,0,0,0.20)]";

  const buttonClass =
    "rounded-xl bg-[#39E58C] px-5 py-3 text-sm font-bold text-[#07110B] shadow-[0_0_18px_rgba(57,229,140,0.12)] transition hover:bg-[#65F5A7] hover:shadow-[0_0_24px_rgba(57,229,140,0.22)] disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <main className="min-h-screen bg-[#080B0A] px-4 py-8 text-[#E7EEE9] md:px-8">
      <div className="mx-auto max-w-7xl">
        <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="mb-2 flex items-center gap-3">
              <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-[#365B43] bg-[#14251A] text-lg font-bold text-[#55E69A] shadow-[0_0_20px_rgba(57,229,140,0.10)]">
                {"</>"}
              </div>

              <div>
                <h1 className="text-3xl font-extrabold tracking-tight text-white">
                  LogicForge <span className="text-[#39E58C]">AI</span>
                </h1>
                <p className="text-[10px] font-semibold uppercase tracking-[0.25em] text-[#71877A]">
                  Digital Design Studio
                </p>
              </div>
            </div>

            <p className="mt-3 text-sm text-[#91A198]">
              AI-powered Verilog generation, simulation and waveform analysis.
            </p>
          </div>

          <div className="flex items-center gap-2 rounded-full border border-[#294432] bg-[#111A14] px-3 py-2 text-xs text-[#91A198]">
            <span className="h-2 w-2 rounded-full bg-[#39E58C] shadow-[0_0_8px_#39E58C]" />
            Digital Logic Workspace
          </div>
        </header>

        <section className={`${panelClass} mb-6 p-5 md:p-6`}>
          <div className="mb-4 flex items-center gap-2">
            <span className="text-[#39E58C]">✳</span>
            <h2 className="text-lg font-bold text-white">
              Describe your digital circuit
            </h2>
          </div>

          <textarea
           value={prompt}
           onChange={(event) => setPrompt(event.target.value)}
           placeholder="e.g. Design a 4-bit binary adder with carry input and carry output..."
           rows={4}
           className="w-full resize-y rounded-xl border border-[#2B4032] bg-[#0A100C] p-4 text-sm leading-6 text-[#E7EEE9] outline-none placeholder:text-[#91A198] placeholder:opacity-100 focus:border-[#39E58C] focus:ring-2 focus:ring-[#39E58C]/10"
          />

          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-[#71877A]">
              Describe the inputs, outputs and expected circuit behavior.
            </p>

            <button
              onClick={generateCode}
              disabled={loading}
              className={buttonClass}
            >
              {loading ? "Generating..." : "✦ Generate Verilog"}
            </button>
          </div>
        </section>

        <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
          <section className={panelClass}>
            <div className="flex items-center justify-between border-b border-[#26352D] bg-[#141D17] px-4 py-4">
              <div>
                <h2 className="font-bold text-white">Verilog Module</h2>
                <p className="mt-1 text-xs text-[#71877A]">
                  Your digital circuit design
                </p>
              </div>
              <span className="rounded-md border border-[#365B43] bg-[#17251B] px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-[#62ECA0]">
                Design
              </span>
            </div>

            <Editor
              height="350px"
              language="verilog"
              theme="logicforge-dark"
              beforeMount={configureMonaco}
              value={moduleCode}
              onChange={(value) => setModuleCode(value || "")}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                automaticLayout: true,
                scrollBeyondLastLine: false,
                padding: { top: 14 },
                roundedSelection: true,
                cursorBlinking: "smooth",
                smoothScrolling: true,
              }}
            />
          </section>

          <section className={panelClass}>
            <div className="flex items-center justify-between border-b border-[#26352D] bg-[#141D17] px-4 py-4">
              <div>
                <h2 className="font-bold text-white">Testbench</h2>
                <p className="mt-1 text-xs text-[#71877A]">
                  Test vectors and verification logic
                </p>
              </div>
            </div>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={generateTestbench}
                  disabled={generatingTestbench || loading || !moduleCode.trim()}
                  className="rounded-lg border border-[#365B43] bg-[#17251B] px-3 py-2 text-xs font-semibold text-[#62ECA0] transition hover:border-[#39E58C] hover:bg-[#203528] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {generatingTestbench
                    ? "Generating..."
                    : "✦ Generate with AI"}
                </button>

                <span className="rounded-md border border-[#365B43] bg-[#17251B] px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-[#62ECA0]">
                  Verification
                </span>
              </div>


                {testbenchError && (
                 <div className="mb-4 rounded-xl border border-red-900/60 bg-red-950/20 p-4 text-sm text-red-300">
                   {testbenchError}
                 </div>
                )}

        {testbenchExplanation && (
          <div className="mb-6 rounded-xl border border-[#294432] bg-[#101713] p-4">
            <h3 className="mb-2 font-bold text-[#62ECA0]">
              ✦ AI Testbench Analysis
            </h3>
            <p className="whitespace-pre-wrap text-sm leading-6 text-[#B7C9BD]">
              {testbenchExplanation}
            </p>
          </div>
        )}


            <Editor
              height="350px"
              language="verilog"
              theme="logicforge-dark"
              beforeMount={configureMonaco}
              value={testbenchCode}
              onChange={(value) => setTestbenchCode(value || "")}
              options={{
                minimap: { enabled: false },
                fontSize: 13,
                automaticLayout: true,
                scrollBeyondLastLine: false,
                padding: { top: 14 },
                roundedSelection: true,
                cursorBlinking: "smooth",
                smoothScrolling: true,
              }}
            />
          </section>
        </div>

        <div className="mb-6 flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-[#294432] bg-[#101713] p-4">
          <div>
            <h2 className="font-bold text-white">Simulation Control</h2>
            <p className="mt-1 text-xs text-[#71877A]">
              Compile and execute your Verilog testbench.
            </p>
          </div>


        <div className="mb-6 flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-[#294432] bg-[#101713] p-4">
          <div>
            <h2 className="font-bold text-white">AI Debugger</h2>
            <p className="mt-1 text-xs text-[#91A198]">
              Analyze compilation errors and failed test cases with AI.
            </p>
          </div>

          <button
            onClick={debugVerilog}
            disabled={debugging || simulating || loading}
            className={buttonClass}
          >
            {debugging ? "Analyzing Code..." : "✦ AI Debug & Fix"}
          </button>
        </div>

        {debugResult && (
          <section className={`${panelClass} mb-6 p-5`}>
            {debugResult.success ? (
              <>
                <div className="mb-4 flex items-center gap-2">
                  <span className="text-xl text-[#39E58C]">✦</span>
                  <h2 className="font-bold text-[#62ECA0]">
                    AI Debugging Analysis
                  </h2>
                </div>

                <div className="mb-4 rounded-xl border border-red-900/60 bg-red-950/20 p-4">
                  <h3 className="mb-2 text-sm font-bold text-red-400">
                    Root Cause
                  </h3>
                  <p className="whitespace-pre-wrap text-sm leading-6 text-[#D5DED8]">
                    {debugResult.root_cause}
                  </p>
                </div>

                <div className="mb-4 rounded-xl border border-[#294432] bg-[#0B120D] p-4">
                  <h3 className="mb-2 text-sm font-bold text-[#62ECA0]">
                    Explanation
                  </h3>
                  <p className="whitespace-pre-wrap text-sm leading-6 text-[#B7C9BD]">
                    {debugResult.explanation}
                  </p>
                </div>

                <div className="mb-4 rounded-xl border border-[#294432] bg-[#0B120D] p-4">
                  <h3 className="mb-2 text-sm font-bold text-[#62ECA0]">
                    Changes Made
                  </h3>
                  <p className="whitespace-pre-wrap text-sm leading-6 text-[#B7C9BD]">
                    {debugResult.changes_made}
                  </p>
                </div>

                <div className="flex flex-wrap gap-3">
                  <button
                    onClick={applyDebugFix}
                    disabled={applyingFix}
                    className={buttonClass}
                  >
                    {applyingFix ? "Applying..." : "✓ Apply AI Fix"}
                  </button>

                  <button
                    onClick={() => setDebugResult(null)}
                    className="rounded-xl border border-[#365B43] px-5 py-3 text-sm font-semibold text-[#B7C9BD] transition hover:border-[#39E58C] hover:text-white"
                  >
                    Dismiss
                  </button>
                </div>

                <p className="mt-3 text-xs text-[#71877A]">
                  Review the suggested correction before applying it.
                  Run simulation again to verify the fix.
                </p>
              </>
            ) : (
              <div>
                <h2 className="mb-2 font-bold text-red-400">
                  AI Debugging Failed
                </h2>
                <p className="whitespace-pre-wrap text-sm text-[#B7C9BD]">
                  {debugResult.error || "Unable to analyze the code."}
                </p>
              </div>
            )}
          </section>
        )}


          <button
            onClick={runSimulation}
            disabled={simulating}
            className={buttonClass}
          >
            {simulating ? "Running Simulation..." : "▶ Run Simulation"}
          </button>
        </div>

        {explanation && (
          <section className={`${panelClass} mb-6 p-5`}>
            <div className="mb-3 flex items-center gap-2">
              <span className="text-[#39E58C]">✦</span>
              <h2 className="font-bold text-[#62ECA0]">AI Explanation</h2>
            </div>
            <p className="whitespace-pre-wrap text-sm leading-7 text-[#B7C9BD]">
              {explanation}
            </p>
          </section>
        )}

        {output && (
          <section className={`${panelClass} mb-6`}>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#26352D] bg-[#141D17] px-5 py-4">
              <h2 className="font-bold text-white">Simulation Output</h2>

              {simulationSuccess !== null && (
                <span
                  className={`rounded-full border px-3 py-1 text-xs font-bold ${
                    simulationSuccess
                      ? "border-[#286A45] bg-[#143322] text-[#62ECA0]"
                      : "border-red-900 bg-red-950/50 text-red-400"
                  }`}
                >
                  {simulationSuccess ? "✓ PASS" : "✕ FAIL"}
                </span>
              )}
            </div>

            <pre className="overflow-x-auto whitespace-pre-wrap p-5 font-mono text-xs leading-6 text-[#B7C9BD]">
              {output}
            </pre>
          </section>
        )}

        {vcd && (
          <section className="mb-6">
            <WaveformViewer vcd={vcd} />

            <details className="mt-4 overflow-hidden rounded-xl border border-[#26352D] bg-[#101713]">
              <summary className="cursor-pointer px-5 py-4 text-sm font-semibold text-[#91A198] hover:text-[#62ECA0]">
                View Generated VCD Waveform Data
              </summary>

              <pre className="max-h-96 overflow-auto whitespace-pre-wrap border-t border-[#26352D] p-4 font-mono text-xs leading-5 text-[#91A198]">
                {vcd}
              </pre>
            </details>
          </section>
        )}

        <footer className="mt-12 border-t border-[#26352D] pt-6 text-center">
          <p className="text-sm font-semibold tracking-wide text-[#91A198]">
            LogicForge <span className="text-[#39E58C]">AI</span>
          </p>
          <p className="mt-1 text-xs text-[#53645A]">
            Verilog Generation · Simulation · Waveform Analysis
          </p>
        </footer>
      </div>
    </main>
  );
}
