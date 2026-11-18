const express = require("express");
const cors = require("cors");
const OpenAI = require("openai");
require("dotenv").config();


const app = express();
const PORT = 8000;

app.use(cors());
app.use(express.json());
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

app.post("/generate", async (req, res) => {
  try {
    const { prompt } = req.body;

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