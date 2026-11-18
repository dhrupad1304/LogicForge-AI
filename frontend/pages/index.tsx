import { useState } from "react";
import Editor from "@monaco-editor/react";
import { API } from "../utils/api";

export default function Home() {
  const [prompt, setPrompt] = useState("");
  const [code, setCode] = useState("");
  const [output, setOutput] = useState("");

  // ✅ Generate Verilog Code
  const generate = async () => {
    try {
      const res = await API.post("/generate", { prompt });
      setCode(res.data.code || "");
    } catch (error) {
      console.error("Generate Error:", error);
      alert("Failed to generate code");
    }
  };

  // ✅ Run Simulation
  const simulate = async () => {
    try {
      const res = await API.post("/simulate", { code });

      if (!res.data.success) {
        alert(res.data.error || "Simulation failed");
      } else {
        setOutput(res.data.output || "");
      }
    } catch (error) {
      console.error("Simulation Error:", error);
      alert("Simulation failed");
    }
  };

  return (
    <div style={{ padding: "20px" }}>
      <h1>LogicForge AI</h1>

      {/* ✅ Prompt Input */}
      <textarea
        placeholder="Describe your circuit..."
        value={prompt}
        style={{ width: "100%", height: "100px", marginBottom: "10px" }}
        onChange={(e) => setPrompt(e.target.value)}
      />

      {/* ✅ Buttons */}
      <div style={{ marginBottom: "10px" }}>
        <button onClick={generate} style={{ marginRight: "10px" }}>
          Generate
        </button>

        <button onClick={simulate}>
          Run Simulation
        </button>
      </div>

      {/* ✅ Monaco Editor */}
      <Editor
        height="400px"
        language="verilog"
        theme="vs-dark"
        value={code}
        onChange={(value) => setCode(value || "")}
      />

      {/* ✅ Output */}
      <h3>Output:</h3>
      <pre
        style={{
          background: "#111",
          color: "#0f0",
          padding: "10px",
          minHeight: "100px",
        }}
      >
        {output}
      </pre>
    </div>
  );
}