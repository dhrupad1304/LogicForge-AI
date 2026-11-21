# LogicForge backend: FastAPI + Icarus Verilog + Yosys + netlistsvg.
# Build:  docker build -t logicforge-backend ./backend
# Run:    docker run -p 8000:8000 --env-file backend/.env logicforge-backend

FROM python:3.12-slim

# Simulation (iverilog/vvp), synthesis for schematics (yosys) and Node.js
# for drawing schematics (netlistsvg).
RUN apt-get update \
    && apt-get install -y --no-install-recommends iverilog yosys nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY . .

# Run as an unprivileged user; the workspace is the only writable app folder.
RUN useradd --create-home --uid 10001 logicforge \
    && mkdir -p /app/workspace \
    && chown -R logicforge:logicforge /app/workspace
USER logicforge

# Servers have no GPU for a local model, so the image defaults to Claude.
ENV PORT=8000 \
    PYTHONUNBUFFERED=1 \
    LOGICFORGE_AI_PROVIDER=anthropic

EXPOSE 8000

# One process: the rate limiter keeps its counts in memory.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
