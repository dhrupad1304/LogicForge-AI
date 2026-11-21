# LogicForge AI

AI-powered Verilog generator + simulator.

## Features
- Natural language → Verilog module and self-checking testbench
- AI testbench generation and AI debugging of failed simulations
- Simulation using Icarus Verilog, with a built-in waveform viewer
- Circuit schematics: gate-level (AND/OR/XOR/MUX, flip-flops) or RTL blocks,
  drawn from the design with Yosys
- Workspace file explorer and Monaco editor UI

## Requirements
- Python 3.10+
- Node.js 20.12+
- [Icarus Verilog](https://steveicarus.github.io/iverilog/) (`iverilog` and `vvp` on your PATH),
  e.g. `brew install icarus-verilog`
- [Yosys](https://yosyshq.net/yosys/) for schematics: `brew install yosys`
- [Ollama](https://ollama.com) running locally with the model used by the backend:
  `ollama pull qwen2.5-coder:3b`

## Run

### Backend (http://localhost:8000)
```bash
cd backend
pip install -r requirements.txt
npm install          # netlistsvg, used to draw schematics
uvicorn main:app --reload
```

The API only accepts requests from the frontend at `http://localhost:3000`
(and `127.0.0.1:3000`). To allow other origins, set
`LOGICFORGE_ALLOWED_ORIGINS` to a comma-separated list.

### Frontend (http://localhost:3000)
```bash
cd frontend
npm install
npm run dev
```

## How generation is verified
1. The AI writes the **design** only. It is compile-checked with Icarus
   Verilog (Verilog-2005 rules), common mistakes are fixed in code, and the
   AI repairs whatever is left.
2. The **testbench is built in code** from the design's ports: declarations,
   stimulus (all input combinations, or random vectors for wide inputs),
   clock and reset, comparisons and the PASS/FAIL summary.
3. In a **separate request**, the AI writes only the expected-value logic
   (a reference model) from your description and the port list. It never
   sees the design's code, so a PASS means two independent versions agree.
4. On FAIL, it tries a fresh design, then a fresh reference model, and keeps
   the pair that agrees (or reports the closest one honestly).

## Choosing the AI model
Copy `backend/.env.example` to `backend/.env` and set:
- `LOGICFORGE_AI_PROVIDER=ollama` (default) with `LOGICFORGE_OLLAMA_MODEL`
  for a local model, or
- `LOGICFORGE_AI_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` to use Claude
  (much better on FSMs, ALUs and protocols; billed per use).

Restart the backend after changing it. The active model is shown in the
top-right corner of the app.

## Tests
```bash
cd backend
pip install -r requirements-dev.txt
python3 -m pytest
```
The suite (about 110 tests, a few seconds) covers simulation and verdicts,
the template and golden-model testbenches, the automatic code fixes, the
generation pipeline and Debug & Fix (with the AI replaced by a scripted
stub, so no model is called), the HTTP API and schematics. Tests that need
Icarus Verilog or Yosys are skipped when those tools are missing.

## Notes
- Workspace files are stored in `backend/workspace/` (not committed to git).
- Simulations reject Verilog that accesses host files (`$fopen`,
  `$readmemh`/`$readmemb`, `$writememh`/`$writememb`, `` `include ``, and
  `$dumpfile` with a path).
- `backend/server.js` is an older OpenAI-based server that is not used by the
  frontend. It runs on port 8001 and reads `OPENAI_API_KEY` from `backend/.env`.
