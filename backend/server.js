const express = require("express");
const cors = require("cors");
const OpenAI = require("openai");

// Load backend/.env with Node's built-in loader (Node 20.12+), so the
// separate "dotenv" package is not needed.
try {
  process.loadEnvFile(require("path").join(__dirname, ".env"));
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}

const app = express();
// Port 8000 belongs to the main Python backend (uvicorn), so this legacy
// OpenAI server uses 8001 unless PORT is set.
const PORT = Number(process.env.PORT) || 8001;

app.use(cors({ origin: ["http://localhost:3000", "http://127.0.0.1:3000"] }));
app.use(express.json());
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

app.post("/generate", async (req, res) => {
  try {
    const { prompt } = req.body || {};

    if (typeof prompt !== "string" || !prompt.trim()) {
      return res.status(400).json({ error: "A prompt is required." });
    }

    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: [
        {
          role: "system",
          content: `
You are a digital design expert.

Given a prompt, generate:
1. Clean Verilog module
2. Proper testbench

Rules:
- Use correct syntax
- Include comments
- Testbench must verify functionality
- Keep output clean and structured

Return format:
VERILOG:
<code>

TESTBENCH:
<code>
          `,
        },
        {
          role: "user",
          content: prompt,
        },
      ],
    });

    const output = response.choices[0].message.content;

    res.json({ code: output });

  } catch (error) {
    console.error(error);
    res.status(500).json({ error: "AI generation failed" });
  }
});

app.listen(PORT, () => {
  console.log(`Server running on http://localhost:${PORT}`);
});