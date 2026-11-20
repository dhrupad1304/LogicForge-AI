import { useEffect, useMemo, useState } from "react";
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
  problems?: string | null;
  log?: string[];
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
  const repairs =
    v.repair_attempts > 0
      ? ` after ${v.repair_attempts} automatic repair${v.repair_attempts === 1 ? "" : "s"}`
      : "";
  if (v.ready) {
    const result =
      v.verdict === "PASS"
        ? `all ${v.total_tests} tests passed`
        : `${v.failures} of ${v.total_tests} tests failed`;
    return `Compiled with Icarus Verilog${repairs}. Pre-check: ${result}. Run Simulation to see full output and waveform.`;
  }
  return [
    `Code still has problems${repairs}${v.compiled ? " (it compiles)" : " (it does not compile)"}:`,
    v.problems || "",
    ...(v.log && v.log.length ? ["", "Repair log:", ...v.log] : []),
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
  const [zoom, setZoom] = useState(1);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const parsed = useMemo(() => parseVCD(vcd), [vcd]);

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
      <div className="rounded-xl border border-[#26352D] bg-[#101713] p-6 text-sm text-[#91A198]">
        No waveform signals were found in the VCD file.
      </div>
    );
  }

  const duration = Math.max(parsed.duration, 1);
  const rowHeight = 58;
  const leftWidth = 190;
  const plotWidth = Math.min(12000, Math.max(650, duration * 28 * zoom));
  const totalHeight = displayedSignals.length * rowHeight + 35;
  const scale = plotWidth / duration;
  const ticks = Array.from({ length: 6 }, (_, index) => (duration * index) / 5);

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
    <div className="overflow-hidden rounded-2xl border border-[#263D30] bg-[#0D130F] shadow-[0_12px_50px_rgba(0,0,0,0.25)]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#26352D] bg-[#111A14] px-5 py-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-[#39E58C] shadow-[0_0_10px_#39E58C]" />
            <h3 className="font-semibold text-[#E7EEE9]">Digital Waveform</h3>
          </div>
          <p className="mt-1 text-xs text-[#91A198]">
            Duration: {parsed.duration} {parsed.timescale}
            {" · "}Showing {displayedSignals.length} of {signals.length} signals
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
              <svg width={plotWidth} height={rowHeight} className="shrink-0">
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
                {signal.variable.width > 1 ? (
                  getBusSegments(signal).map((segment, segmentIndex) => {
                    const slant = Math.min(5, (segment.end - segment.start) / 2);
                    const unknown = /[xz]/.test(segment.value);
                    return (
                      <g key={segmentIndex}>
                        <polygon
                          points={`${segment.start},26 ${segment.start + slant},13 ${segment.end - slant},13 ${segment.end},26 ${segment.end - slant},39 ${segment.start + slant},39`}
                          fill={unknown ? "rgba(248,113,113,0.12)" : "rgba(57,229,140,0.10)"}
                          stroke={unknown ? "#F87171" : "#39E58C"}
                          strokeWidth="1.5"
                        />
                        {segment.end - segment.start > 28 && (
                          <text
                            x={(segment.start + segment.end) / 2}
                            y={30}
                            textAnchor="middle"
                            fontSize="10"
                            fill="#D5DED8"
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
                  </>
                )}
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
          <span>Buses shown in hex</span>
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
    void loadProjectFiles("");
    // Load the workspace root once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const panelClass =
    "overflow-hidden rounded-2xl border border-[#263D30] bg-[#101713] shadow-[0_8px_30px_rgba(0,0,0,0.20)]";
  const buttonClass =
    "rounded-xl bg-[#39E58C] px-5 py-3 text-sm font-bold text-[#07110B] shadow-[0_0_18px_rgba(57,229,140,0.12)] transition hover:bg-[#65F5A7] hover:shadow-[0_0_24px_rgba(57,229,140,0.22)] disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <main className="min-h-screen bg-[#080B0A] px-4 py-8 text-[#E7EEE9] md:px-8">
      <div className="mx-auto max-w-[1600px]">
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

        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
          <aside className="min-w-0 lg:sticky lg:top-6">
            <section className={`${panelClass} p-4`}>
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-lg font-bold text-white">
                  <span className="text-[#39E58C]">▣</span> Explorer
                </h2>
                <button
                  onClick={() => void loadProjectFiles()}
                  disabled={projectLoading}
                  className="text-xs text-[#62ECA0] hover:text-white disabled:opacity-50"
                >
                  {projectLoading ? "Loading..." : "↻ Refresh"}
                </button>
              </div>

              <div className="mb-3 grid grid-cols-2 gap-2">
                <button
                  onClick={() => void createProjectItem("file")}
                  className="rounded-lg border border-[#365B43] bg-[#17251B] px-2 py-2 text-xs font-semibold text-[#62ECA0] hover:border-[#39E58C]"
                >
                  + New File
                </button>
                <button
                  onClick={() => void createProjectItem("folder")}
                  className="rounded-lg border border-[#365B43] bg-[#17251B] px-2 py-2 text-xs font-semibold text-[#62ECA0] hover:border-[#39E58C]"
                >
                  + Folder
                </button>
              </div>

              <div className="mb-3 flex items-center justify-between gap-2 rounded-lg bg-[#0B120D] px-3 py-2">
                <span className="min-w-0 truncate text-xs text-[#91A198]">
                  {currentFolder ? `/${currentFolder}` : "/ workspace"}
                </span>
                {currentFolder && (
                  <button
                    onClick={() => {
                      const parent = currentFolder.includes("/")
                        ? currentFolder.slice(0, currentFolder.lastIndexOf("/"))
                        : "";
                      void loadProjectFiles(parent);
                    }}
                    className="shrink-0 text-xs text-[#62ECA0] hover:text-white"
                  >
                    ↑ Up
                  </button>
                )}
              </div>

              <div className="max-h-[55vh] space-y-1 overflow-y-auto">
                {projectFiles.length === 0 && !projectLoading && (
                  <p className="px-2 py-4 text-center text-xs text-[#71877A]">
                    This folder is empty.
                  </p>
                )}
                {projectFiles.map((item) => (
                  <div
                    key={item.path}
                    className={`group flex items-center gap-1 rounded-lg px-2 py-2 ${
                      moduleFile === item.path || testbenchFile === item.path
                        ? "bg-[#1A3022] text-[#62ECA0]"
                        : "text-[#B7C9BD] hover:bg-[#17231B]"
                    }`}
                  >
                    <button
                      onClick={() =>
                        item.type === "folder"
                          ? void loadProjectFiles(item.path)
                          : void openProjectFile(item.path)
                      }
                      className="flex min-w-0 flex-1 items-center gap-2 text-left text-xs"
                      title={item.path}
                    >
                      <span className="shrink-0">
                        {item.type === "folder" ? "📁" : "📄"}
                      </span>
                      <span className="truncate">{item.name}</span>
                    </button>
                    <button
                      onClick={() => void renameProjectItem(item.path)}
                      title="Rename"
                      className="px-1 text-[10px] text-[#71877A] hover:text-[#62ECA0]"
                    >
                      ✎
                    </button>
                    <button
                      onClick={() => void deleteProjectItem(item.path)}
                      title="Delete"
                      className="px-1 text-[10px] text-[#71877A] hover:text-red-400"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>

              <div className="mt-4 border-t border-[#26352D] pt-3">
                <p className="text-[10px] font-bold uppercase tracking-wider text-[#71877A]">
                  Open files
                </p>
                {[
                  { label: "Module", file: moduleFile, dirty: moduleDirty },
                  { label: "Testbench", file: testbenchFile, dirty: testbenchDirty },
                ].map(({ label, file, dirty }) => (
                  <p key={label} className="mt-1 break-all text-xs text-[#B7C9BD]">
                    <span className="text-[#71877A]">{label}: </span>
                    {file || "not linked to a file"}
                    {file && dirty && (
                      <span className="ml-1 text-[#F5C451]">● unsaved</span>
                    )}
                  </p>
                ))}
                <button
                  onClick={() => void saveProjectFile()}
                  disabled={
                    (!moduleFile || !moduleDirty) &&
                    (!testbenchFile || !testbenchDirty)
                  }
                  className="mt-3 w-full rounded-lg bg-[#39E58C] px-3 py-2 text-xs font-bold text-[#07110B] hover:bg-[#65F5A7] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Save Open Files
                </button>
              </div>

              {projectMessage && (
                <p className="mt-3 break-words text-xs text-[#62ECA0]">
                  {projectMessage}
                </p>
              )}
              {projectError && (
                <p className="mt-3 break-words text-xs text-red-400">
                  {projectError}
                </p>
              )}
              <p className="mt-4 text-[10px] leading-5 text-[#53645A]">
                Files are stored in the LogicForge backend workspace. Supported
                file types: .v, .sv and .txt.
              </p>
            </section>
          </aside>

          <div className="min-w-0">
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
                className="w-full resize-y rounded-xl border border-[#2B4032] bg-[#0A100C] p-4 text-sm leading-6 text-[#E7EEE9] outline-none placeholder:text-[#91A198] focus:border-[#39E58C] focus:ring-2 focus:ring-[#39E58C]/10"
              />
              <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-[#71877A]">
                  Describe the inputs, outputs and expected circuit behavior.
                </p>
                <button
                  onClick={generateCode}
                  disabled={busy}
                  className={buttonClass}
                >
                  {loading ? "Generating..." : "✦ Generate Verilog"}
                </button>
              </div>
            </section>

            <div className="mb-6 grid grid-cols-1 gap-6 xl:grid-cols-2">
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
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#26352D] bg-[#141D17] px-4 py-4">
                  <div>
                    <h2 className="font-bold text-white">Testbench</h2>
                    <p className="mt-1 text-xs text-[#71877A]">
                      Test vectors and verification logic
                    </p>
                  </div>
                  <span className="rounded-md border border-[#365B43] bg-[#17251B] px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-[#62ECA0]">
                    Verification
                  </span>
                </div>
                <div className="p-3">
                  <button
                    onClick={generateTestbench}
                    disabled={busy || !moduleCode.trim()}
                    className="rounded-lg border border-[#365B43] bg-[#17251B] px-3 py-2 text-xs font-semibold text-[#62ECA0] transition hover:border-[#39E58C] hover:bg-[#203528] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {generatingTestbench
                      ? "Generating..."
                      : "✦ Generate with AI"}
                  </button>
                </div>
                {testbenchError && (
                  <div className="mx-3 mb-3 rounded-xl border border-red-900/60 bg-red-950/20 p-4 text-sm text-red-300">
                    {testbenchError}
                  </div>
                )}
                {testbenchExplanation && (
                  <div className="mx-3 mb-3 rounded-xl border border-[#294432] bg-[#101713] p-4">
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

            <section className={`${panelClass} mb-6 p-4`}>
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <h2 className="font-bold text-white">Simulation Control</h2>
                  <p className="mt-1 text-xs text-[#71877A]">
                    Compile and execute your Verilog testbench.
                  </p>
                </div>
                <button
                  onClick={runSimulation}
                  disabled={busy}
                  className={buttonClass}
                >
                  {simulating ? "Running Simulation..." : "▶ Run Simulation"}
                </button>
              </div>
            </section>

            <section className={`${panelClass} mb-6 p-4`}>
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <h2 className="font-bold text-white">AI Debugger</h2>
                  <p className="mt-1 text-xs text-[#91A198]">
                    Analyze compilation errors and failed test cases with AI.
                  </p>
                </div>
                <button
                  onClick={debugVerilog}
                  disabled={busy}
                  className={buttonClass}
                >
                  {debugging ? "Analyzing Code..." : "✦ AI Debug & Fix"}
                </button>
              </div>
            </section>

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
                    {Array.isArray(debugResult.notes) && debugResult.notes.length > 0 && (
                      <p className="mb-4 whitespace-pre-wrap rounded-xl border border-yellow-900 bg-yellow-950/30 p-3 text-xs leading-5 text-yellow-300">
                        {debugResult.notes.join("\n")}
                      </p>
                    )}
                    <div className="flex flex-wrap gap-3">
                      {debugResult.module_changed === false &&
                      debugResult.testbench_changed === false ? (
                        <span className="rounded-xl border border-[#365B43] px-5 py-3 text-sm text-[#B7C9BD]">
                          No code changes suggested
                        </span>
                      ) : (
                        <button
                          onClick={applyDebugFix}
                          disabled={busy}
                          className={buttonClass}
                        >
                          ✓ Apply AI Fix
                          {debugResult.module_changed === false
                            ? " (testbench only)"
                            : debugResult.testbench_changed === false
                              ? " (design only)"
                              : ""}
                        </button>
                      )}
                      <button
                        onClick={() => setDebugResult(null)}
                        className="rounded-xl border border-[#365B43] px-5 py-3 text-sm font-semibold text-[#B7C9BD] transition hover:border-[#39E58C] hover:text-white"
                      >
                        Dismiss
                      </button>
                    </div>
                    <p className="mt-3 text-xs text-[#71877A]">
                      Review the suggested correction before applying it. Run
                      simulation again to verify the fix.
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
                      {simulationSuccess
                        ? simulationVerdict === "UNVERIFIED"
                          ? "✓ Ran, no summary"
                          : `✓ PASS${simulationTests != null ? ` (${simulationTests} tests)` : ""}`
                        : "✕ FAIL"}
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
          </div>
        </div>

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
