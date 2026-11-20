# LogicForge AI

AI-powered Verilog generator + simulator.

## Features
- Natural language → Verilog module and self-checking testbench
- AI testbench generation and AI debugging of failed simulations
- Simulation using Icarus Verilog, with a built-in waveform viewer
- Workspace file explorer and Monaco editor UI

## Requirements
- Python 3.10+
- Node.js 20.12+
- [Icarus Verilog](https://steveicarus.github.io/iverilog/) (`iverilog` and `vvp` on your PATH),
  e.g. `brew install icarus-verilog`
- [Ollama](https://ollama.com) running locally with the model used by the backend:
  `ollama pull qwen2.5-coder:3b`

## Run

### Backend (http://localhost:8000)
```bash
cd backend
pip install -r requirements.txt
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

## Notes
- Workspace files are stored in `backend/workspace/` (not committed to git).
- Simulations reject Verilog that accesses host files (`$fopen`,
  `$readmemh`/`$readmemb`, `$writememh`/`$writememb`, `` `include ``, and
  `$dumpfile` with a path).
- `backend/server.js` is an older OpenAI-based server that is not used by the
  frontend. It runs on port 8001 and reads `OPENAI_API_KEY` from `backend/.env`.
