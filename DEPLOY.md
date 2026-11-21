# Deploying LogicForge

LogicForge has two parts that deploy to different places:

| Part | Where | Why |
|---|---|---|
| **Frontend** (Next.js, `frontend/`) | **Vercel** | Built for Next.js; free; fast. |
| **Backend** (FastAPI, `backend/`) | **Render** (or any Docker host) | Needs native tools (Icarus Verilog, Yosys) and runs them as processes, which Vercel's serverless functions cannot do. |

The deployed backend uses the **Claude API** by default: cloud servers have no
GPU, so a local Ollama model would take minutes per request.

You need: a GitHub account, a [Render](https://render.com) account, a
[Vercel](https://vercel.com) account and an Anthropic API key from
[platform.claude.com](https://platform.claude.com) (billed per use; a
generation costs a few cents).

---

## 1. Put the code on GitHub

Create an empty repository on GitHub (no README), then from the project folder:

```bash
git remote add origin https://github.com/<you>/logicforge.git
git push -u origin main
```

`backend/.env` is git-ignored, so your keys are not uploaded.

## 2. Deploy the backend on Render

1. In Render: **New + → Blueprint**, connect GitHub and pick the repository.
   Render reads `render.yaml` and creates the `logicforge-backend` service.
2. Render asks for the secret values:
   - `ANTHROPIC_API_KEY`: your Anthropic API key.
   - `LOGICFORGE_ACCESS_PASSWORD`: a password you choose and give to the
     people who may use the site. Use something long; anyone with it can spend
     your API credit (within the hourly limits).
   - `LOGICFORGE_ALLOWED_ORIGINS`: leave as `http://localhost:3000` for now;
     you will set it to the Vercel URL in step 4.
3. Wait for the first build (several minutes: it installs Icarus Verilog,
   Yosys and Node). Open `https://logicforge-backend.onrender.com/`: it
   should answer `{"message": "LogicForge AI running", "status": "ok"}`.
   Your service URL may differ; copy it from the Render dashboard.

## 3. Deploy the frontend on Vercel

1. In Vercel: **Add New → Project**, import the same repository.
2. Set **Root Directory** to `frontend`. Vercel detects Next.js.
3. Under **Environment Variables** add
   `NEXT_PUBLIC_API_URL` = your Render URL from step 2 (no trailing slash).
4. **Deploy.** Copy the production URL, e.g. `https://logicforge.vercel.app`.

`NEXT_PUBLIC_API_URL` is built into the frontend, so after changing it,
redeploy the frontend.

## 4. Connect the two

In Render → `logicforge-backend` → **Environment**, set
`LOGICFORGE_ALLOWED_ORIGINS` to your Vercel URL (exact, no trailing slash)
and save; Render restarts the backend.

To also allow Vercel **preview** deployments, add
`LOGICFORGE_ALLOWED_ORIGIN_REGEX`, e.g. `https://logicforge-[a-z0-9-]+\.vercel\.app`.

Open the Vercel URL, enter the access password, and generate a circuit.

---

## Settings reference (backend)

| Variable | Default | Meaning |
|---|---|---|
| `LOGICFORGE_AI_PROVIDER` | `anthropic` in Docker, `ollama` locally | Which AI to use |
| `ANTHROPIC_API_KEY` | none | Claude API key |
| `LOGICFORGE_ANTHROPIC_MODEL` | `claude-opus-5-5` | Claude model |
| `LOGICFORGE_ANTHROPIC_EFFORT` | `high` | Thinking depth: low … max |
| `LOGICFORGE_ACCESS_PASSWORD` | empty (no password) | Shared site password |
| `LOGICFORGE_ALLOWED_ORIGINS` | localhost:3000 | Frontends allowed to call the API |
| `LOGICFORGE_ALLOWED_ORIGIN_REGEX` | none | Pattern for extra origins (Vercel previews) |
| `LOGICFORGE_AI_REQUESTS_PER_HOUR` | `30` | Per visitor; Generate / testbench / Debug |
| `LOGICFORGE_COMPUTE_REQUESTS_PER_HOUR` | `300` | Per visitor; Simulate / Schematic |
| `LOGICFORGE_MAX_ATTEMPTS` | `3` | Design / reference attempts per Generate |

## What is protected

- **Password:** every API route except the health check requires
  `Authorization: Bearer <password>` when a password is set.
- **Rate limits:** per visitor IP and hour, so a leaked password cannot run
  up unlimited API cost. Counts are kept in memory: run one backend instance.
- **Simulation sandbox:** Verilog that reads or writes files is rejected,
  and every Icarus / Yosys run has time, CPU, file-size and memory limits.
  The container runs as an unprivileged user.

## Know before you deploy

- **Render free plan sleeps** after 15 minutes without traffic; the first
  request afterwards takes about a minute. A paid instance avoids this.
- **The workspace is temporary on Render:** files saved in the Files panel
  are lost when the service restarts or redeploys. Download anything you
  want to keep. (Attach a Render disk at `/app/workspace` to keep them.)
- **Everyone with the password shares one workspace.**
- **Other hosts:** the backend is a standard Docker image
  (`docker build -t logicforge-backend backend`), so Railway, Fly.io or a VPS
  work too; set the same environment variables and expose port 8000.
